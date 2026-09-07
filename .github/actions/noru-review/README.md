# `noru-review` action

Review a pull request against every Noru GRC piece it affects, in one job, with no credential and
no way to write to Noru.

## About

A pull request that adds a model provider, a database migration, a Terraform module or a deploy
step has changed something a compliance record describes. This action reads the branch diff,
selects the pieces whose declared signals the diff matches, runs only their local checks — scan,
validate, expiry and policy — and writes one consolidated JSON report and job summary.

It is the deterministic counterpart of the `/noru:review` command an engineer runs in their editor,
and it is **structurally read-only**: there is no input that enables the `diff` or `push` steps, and
`NORU_API_KEY` is removed from every child process. Pull requests, including forks, need only
`contents: read`.

**How routing works.** Each piece declares path and content patterns in the toolkit, for example a
change under `agents/` or `prompts/`, a dependency manifest that may add an AI provider, a `.tf`
file, a workflow under `.github/workflows`. A piece is selected when the diff matches one of its
signals, and the report says which signal, with the file and line. A piece whose external input is
absent, such as a queue-driven piece on a fork, is reported as `skipped`, never `passed`.

## Usage

The [supported template](../../../templates/github/noru-grc-review.yml) is the recommended start.
Copy it to `.github/workflows/noru-grc-review.yml`:

```yaml
name: Noru GRC review

on:
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: noru-grc-review-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - uses: actions/setup-node@v5
        with:
          node-version: "20"
      - uses: noru-tech/noru-review-action@v0
        with:
          base-ref: ${{ github.event.pull_request.base.sha || github.event.repository.default_branch }}
          # Set the repository variable NORU_GRC_MODE=gate after the warning report is understood.
          mode: ${{ vars.NORU_GRC_MODE || 'warn' }}
          gate-on-new: true
```

`fetch-depth: 0` is required: routing and the policy comparison both need the merge base with
`base-ref` in the checkout. The template starts in `warn` and moves to `gate` through a repository
variable, so turning the gate on is a settings change rather than a workflow edit.

## Examples

**Review an explicitly adopted set** instead of routing from the diff. Useful when a repository
has committed to specific pieces and wants them checked on every pull request regardless of what
changed:

```yaml
- uses: noru-tech/noru-review-action@v0
  with:
    base-ref: ${{ github.event.pull_request.base.sha }}
    pieces: privacy-datamap,ai-inventory
    mode: gate
```

**Gate with a privacy baseline and a review cadence.** The policy step compares the data map
against the committed baseline; the cadence turns a stale sign-off into a finding:

```yaml
- uses: noru-tech/noru-review-action@v0
  with:
    base-ref: ${{ github.event.pull_request.base.sha }}
    baseline: .noru/privacy-baseline.yml
    max-age-days: 90
    mode: gate
    gate-on-new: true
```

**Act on the selection.** The `selected-pieces` output is a JSON array, so a later step can
branch on it:

```yaml
- id: review
  uses: noru-tech/noru-review-action@v0
  with:
    base-ref: ${{ github.event.pull_request.base.sha }}
- if: contains(fromJSON(steps.review.outputs.selected-pieces), 'privacy-datamap')
  run: echo "this pull request touches personal data; privacy review requested"
```

To run one piece with the full set of options, including the opt-in publication steps, use
[`noru-ci-action`](https://github.com/noru-tech/noru-ci-action).

## Inputs

| Input | Default | Description |
|---|---|---|
| `base-ref` | *(required)* | Base commit or branch for routing and policy comparison. The checkout must contain it |
| `repo` | `.` | Path to the repository to review |
| `pieces` | *(routed)* | Comma-separated override. Empty selects pieces from branch-change signals |
| `mode` | `warn` | `warn` reports findings; `gate` fails on them. Start with `warn` |
| `fail-on` | *(tool default)* | Comma-separated finding kinds that gate, or `none` |
| `baseline` | *(none)* | Path to the committed privacy baseline for the policy step |
| `max-age-days` | `0` | Review cadence in days. `0` does not invent one |
| `warn-within-days` | `30` | How far ahead to report an expiry |
| `gate-on-new` | `false` | Gate only on policy findings introduced by this branch |
| `report-path` | `$RUNNER_TEMP/noru-ci-review.json` | Where to write the consolidated JSON report |
| `summary` | `true` | Write the consolidated result to the job summary |

## Outputs

| Output | Description |
|---|---|
| `status` | `pass`, `warn`, `skipped`, `fail` or `error` |
| `report` | Path to the consolidated JSON report |
| `selected-pieces` | JSON array of the pieces that were selected and ran |

The report lists every changed file, every piece with its disposition and the reasons it was or
was not selected, each piece's own report, and a `write_boundary` field stating that diff and push
were unavailable. A tooling failure in any piece makes the overall status `error`, in `warn` mode
too: a check that could not run is not a check that passed.

As with any composite action, outputs are empty on a run that failed the job. Read the report file
in that case; it is written before the action exits.

## What the action assumes about the runner

It installs **nothing**. `node` and `python3` must already be on the runner, and the action fails
with a clear message if either is missing. Collectors use Node built-ins and validators use the
Python standard library, so the review never depends on a package index being reachable.

## Versioning

`@v0` follows the newest 0.x release, so a copied example never goes stale. To take changes only
when you choose to, pin a release tag from
[the releases page](https://github.com/noru-tech/noru-grc-engineering/releases) or a full commit
SHA. `noru-tech/noru-grc-engineering/.github/actions/noru-review@<tag>` — the path inside the
source repository — is the same code at the same tag; the Marketplace repository is generated from
it on every release. Every plugin and action in the toolkit shares one version number, listed in
the [changelog](../../../CHANGELOG.md).

## Support and contributing

The end-to-end rollout, from installing the pieces to the separate publication step, is in
[developer onboarding](../../../docs/developer-onboarding.md). The action is built and tested in
[`noru-tech/noru-grc-engineering`](https://github.com/noru-tech/noru-grc-engineering), which
also holds the [contribution guide](../../../CONTRIBUTING.md) and the
[security policy](../../../SECURITY.md). Open issues and pull requests there, not in the
Marketplace repository, whose tree is overwritten on every release.
