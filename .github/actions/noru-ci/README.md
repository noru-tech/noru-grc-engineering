# `noru-ci` action

Run one Noru GRC piece headless in CI, so the compliance record a repository commits stays true
between audits instead of being rebuilt before one.

## About

Every Noru piece keeps a reviewable manifest in the repository — `.noru/<piece>.yml` — that
describes something the code contains: the AI systems it calls, the personal data in its schemas,
the misconfiguration in its infrastructure, who approved and deployed each change. This action
re-derives that record from the checkout and fails the build when the two have come apart.

Everything it gates on is computed **from the repository, a calendar and a committed file**. The
default mode needs no network and no credential, so it runs on a pull request from a fork.

| Finding | What it means | Exit |
|---|---|---|
| **drift** | the collector no longer agrees with the committed manifest: someone changed the code without updating the record | `3` |
| **expired interpretation** | a claim whose expiry has passed, or that is outside the review cadence the pipeline declares | `4` |
| **unpermitted personal data** | the data map processes a category, purpose or subject the committed privacy baseline does not permit | `7` |
| **nothing parsed** | a collector found no input in a repository that visibly has one: a tooling failure, never a pass | `6` |

Pieces you can run: `ai-inventory`, `privacy-datamap`, `iac-scan`, `change-control`,
`evidence-push`, `governance-records`, `review-signoff`, `audit-pack`. What each one collects is in
the [toolkit README](../../../README.md#pieces).

## Usage

```yaml
name: compliance
on: [pull_request]

permissions:
  contents: read

jobs:
  inventory:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-node@v5
        with:
          node-version: "20"
      - uses: noru-tech/noru-ci-action@v0
        with:
          piece: ai-inventory
          mode: warn      # switch to gate once the report is quiet
```

Start in `warn`. It runs the identical checks, labels each finding `would-fail`, and exits `0`;
turn it into a `gate` when the report is quiet. A gate that has never run is how a gate gets
reverted.

To route a whole pull-request diff to every affected piece in one job, use
[`noru-review-action`](https://github.com/noru-tech/noru-review-action) instead. This action is
the lower-level tool for one explicitly adopted piece.

## Examples

**Gate only on what this pull request introduced.** A repository with a backlog can gate on new
findings while it burns the rest down. Needs the base branch in the checkout.

```yaml
- uses: actions/checkout@v5
  with:
    fetch-depth: 0
- uses: noru-tech/noru-ci-action@v0
  with:
    piece: privacy-datamap
    mode: gate
    base-ref: ${{ github.event.pull_request.base.sha }}
    gate-on-new: true
```

**Declare a review cadence.** A claim last decided more than 90 days ago is reported as `cadence`,
and one expiring within 45 days is flagged early.

```yaml
- uses: noru-tech/noru-ci-action@v0
  with:
    piece: review-signoff
    max-age-days: 90
    warn-within-days: 45
```

**Pin the YAML loader.** Validators use PyYAML when the runner has it and a bundled parser
otherwise. This turns the loader the action reports into an assertion, so a runner image change
fails loudly instead of switching parser under the gate.

```yaml
- uses: noru-tech/noru-ci-action@v0
  with:
    piece: iac-scan
    require-yaml-loader: pyyaml
```

**Run the same piece twice in one job.** The default report path is derived from the piece name,
so give each call its own.

```yaml
- uses: noru-tech/noru-ci-action@v0
  with: { piece: ai-inventory, repo: services/api, report-path: ${{ runner.temp }}/api.json }
- uses: noru-tech/noru-ci-action@v0
  with: { piece: ai-inventory, repo: services/web, report-path: ${{ runner.temp }}/web.json }
```

The GitLab CI and plain-shell recipes, and the publication half that stays separate from the gate,
are in [`docs/ci-mode.md`](../../../docs/ci-mode.md).

## Inputs

| Input | Default | Description |
|---|---|---|
| `piece` | *(required)* | Which piece to run |
| `repo` | `.` | Path to the repository to check |
| `mode` | `gate` | `gate` fails on a finding; `warn` reports the same findings and exits `0` |
| `steps` | `scan,validate,expiry,policy` | Add `diff` and `push`, or use `all`. The default needs no credential |
| `fail-on` | *(tool default)* | Which finding kinds gate the build, or `none` |
| `baseline` | `.noru/privacy-baseline.yml` | The agreed privacy taxonomy for the policy step. Absent file: step skipped. Explicit path that does not exist: tooling failure |
| `base-ref` | *(none)* | Compare against the merge base with this ref, so a finding says whether this pull request introduced it. Needs `fetch-depth: 0` |
| `gate-on-new` | `false` | Gate only on findings this branch introduced. Requires `base-ref`; without it everything gates |
| `max-age-days` | `0` | The review cadence this pipeline declares, in days. `0` declares none |
| `warn-within-days` | `30` | How far ahead to report an expiry that has not passed yet |
| `as-of` | *(today)* | Evaluate expiry against a fixed date. For testing |
| `state` | *(none)* | A Noru state snapshot, so the `diff` step has something to compare against |
| `on-missing-prerequisite` | `skip` | `fail` turns a step with no input into a tooling failure |
| `plugins` | *(bundled)* | Where the pieces live, if not the ones shipped with the action |
| `require-yaml-loader` | *(report only)* | Assert the runner's YAML loader is `pyyaml` or `fallback` |
| `summary` | `true` | Write a job summary table |
| `report-path` | `$RUNNER_TEMP/noru-ci-<piece>.json` | Where to write the JSON report |

## Outputs

| Output | Description |
|---|---|
| `status` | `pass`, `pass-with-warnings`, `warn`, `skipped`, `fail` or `error` |
| `exit-code` | The orchestrator's exit code; the table is in [`docs/ci-mode.md`](../../../docs/ci-mode.md#exit-codes) |
| `report` | Path to the JSON report |
| `drift` | `true` when the committed manifest no longer matches the repository |
| `expired` | Number of interpretations whose expiry has passed |
| `unpermitted` | Number of policy findings that gate this run. Advisory kinds are excluded, so a green build never reports a number here |

### Reading results when the gate fails

A composite action that exits non-zero does not propagate its declared outputs. GitHub sets
`steps.<id>.outcome` to `failure`, but every output above comes back **empty**, including when the
caller sets `continue-on-error: true`. That is runner behaviour, not something this action can
work around while still failing the job.

So outputs are reliable on a passing or warning run and absent on the run you most want to inspect.
Two ways to read a failing run: use `mode: warn`, or read the report file, which is written before
the action exits and survives the failure:

```yaml
- id: gate
  continue-on-error: true
  uses: noru-tech/noru-ci-action@v0
  with: { piece: ai-inventory }
- if: steps.gate.outcome == 'failure'
  run: |
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["status"], d["counts"])' \
      "${{ runner.temp }}/noru-ci-ai-inventory.json"
```

## What the action assumes about the runner

It installs **nothing**. `node` and `python3` must already be on the runner; the action fails with
a clear message if either is missing. Collectors use Node built-ins and validators use the Python
standard library, on purpose, so a compliance gate never depends on a package index being up.

The one environment property worth naming: validators use PyYAML when it is importable and a
bundled fallback parser otherwise, so which loader runs is a property of the runner image. The
action prints which one it got on every run; `require-yaml-loader` makes that an assertion.

## Credentials

Never pass a key as a `with:` input. The default steps need none. For the opt-in `steps: all`
publication, put `NORU_API_KEY` in the job or step `env:` from a secret, only in a job that has
secrets and never on `pull_request` from a fork. The action never reads the value; it checks only
whether the variable is present, and the piece's own push entrypoint reads it at the point of use.
MCP-backed pieces cannot push from this headless runner at all; the action reports their plan and
never claims an emitted call list as an executed write.

## Versioning

`@v0` follows the newest 0.x release, so a copied example never goes stale. To take changes only
when you choose to, pin a release tag from
[the releases page](https://github.com/noru-tech/noru-grc-engineering/releases) or a full commit
SHA. `noru-tech/noru-grc-engineering/.github/actions/noru-ci@<tag>` — the path inside the source
repository — is the same code at the same tag; the Marketplace repository is generated from it on
every release. Every plugin and action in the toolkit shares one version number, listed in the
[changelog](../../../CHANGELOG.md).

## Support and contributing

The action is built and tested in
[`noru-tech/noru-grc-engineering`](https://github.com/noru-tech/noru-grc-engineering), which
also holds the [contribution guide](../../../CONTRIBUTING.md) and the
[security policy](../../../SECURITY.md). Open issues and pull requests there, not in the
Marketplace repository, whose tree is overwritten on every release.
