# Noru GRC repository enforcement action

Make a repository's committed GRC records a merge condition: run every piece its enforcement policy
requires against the whole checkout, offline, and fail the pull request on any violation the
policy has not explicitly accepted.

## About

The other Noru actions check one piece or one pull-request diff. This one is the whole-repository
gate behind a required status check. It reads the committed policy at `.noru/enforcement.yml`,
runs each piece the policy names using the collectors, validators and registry shipped in the
pinned release — never executable code from the target repository — and produces one JSON report,
one annotation per violation, and a job summary.

Two adoption modes are recorded in the policy:

- **strict** accepts no existing failure.
- **ratchet** accepts only the exact violations recorded in a baseline, each with a named owner,
  a rationale, a decision date and an expiry. New, changed, expired or reintroduced debt fails;
  resolved debt whose baseline entry is still present fails too, so the acceptance has to be
  removed in the same reviewed change.

Invalid records, tooling failures, credential exposure and expired exceptions can never be
baselined. The action has no network step and reads no Noru credential. Merge approval is never
permission to publish to Noru; publication stays behind each piece's own `diff`, confirmation and
`push`.

## Usage

The supported path is the `repo-enforcement` plugin, whose `/repo-enforcement:setup` command
writes the policy, this workflow and the CODEOWNERS entries as a reviewable file plan, and whose
`/repo-enforcement:plan` and `:apply` commands create the ruleset that makes the check required.
The workflow it generates is this one, with the action pinned to a full commit SHA that is also
recorded in the policy:

```yaml
name: Noru GRC

on:
  pull_request:

permissions:
  contents: read

jobs:
  validate:
    name: validate
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - id: clock
        run: echo "date=$(date -u +%F)" >> "$GITHUB_OUTPUT"
      - uses: noru-tech/noru-enforce-action@v0
        with:
          as-of: ${{ steps.clock.outputs.date }}
```

The workflow and job names matter: `.noru/enforcement.yml` names the required check, `Noru GRC /
validate` by default, and the ruleset requires exactly that check. The date is passed in rather
than read by the action so that a run is reproducible from its log. The workflow runs on every pull
request with no path filter, because the policy decides what is in scope, not the trigger.

## Examples

**Validate a repository inside a monorepo checkout**, with the policy where that repository keeps
it:

```yaml
- uses: noru-tech/noru-enforce-action@v0
  with:
    repo: services/api
    policy: .noru/enforcement.yml
    as-of: ${{ steps.clock.outputs.date }}
    report-path: .noru/.cache/enforcement.json
```

**Keep the report as an artifact** for a failed run. The report is written before the action
exits, so `if: always()` can pick it up:

```yaml
- id: enforce
  uses: noru-tech/noru-enforce-action@v0
  with:
    as-of: ${{ steps.clock.outputs.date }}
- if: always()
  uses: actions/upload-artifact@v4
  with:
    name: noru-grc-enforcement
    path: ${{ steps.enforce.outputs.report }}
```

## Inputs

| Input | Default | Description |
|---|---|---|
| `as-of` | *(required)* | UTC date (`YYYY-MM-DD`) used for expiry and exception checks |
| `repo` | `.` | Repository to validate |
| `policy` | `.noru/enforcement.yml` | Committed enforcement policy |
| `report-path` | `$RUNNER_TEMP/noru-grc-enforcement.json` | Where the JSON report is written |

## Outputs

| Output | Description |
|---|---|
| `report` | Path to the normalized JSON report |
| `new-violations` | Number of violations not accepted by the exact ratchet baseline |
| `baselined-violations` | Number of current violations matched by a live baseline entry |

The job fails on any new violation, expired exception or stale baseline entry. Each one is
annotated in the log with the piece, rule and subject, and the job summary carries the counts and
the report path. A missing policy or an `as-of` that is not a date is a usage error, exit `2`.

## What the action assumes about the runner

It installs **nothing**. `node` and `python3` must already be on the runner. The pieces it runs use
Node built-ins and the Python standard library only, and every credential-like variable
(`TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, `AUTHORIZATION`) is removed from the environment before
any piece runs.

## Versioning

The managed workflow pins `noru-tech/noru-grc-engineering/actions/enforce` at a full commit SHA
because `/repo-enforcement:verify` recognises only that form and reports a workflow whose pin has
moved. `@v0` follows the newest 0.x release and suits a hand-written workflow that is not under
that verification; pin a release tag from
[the releases page](https://github.com/noru-tech/noru-grc-engineering/releases) or a commit SHA to
take changes only when you choose to. The Marketplace repository is generated from the source
repository on every release, and both forms at the same tag are the same code. Every plugin and
action in the toolkit shares one version number, listed in the [changelog](../../CHANGELOG.md).

## Support and contributing

How enforcement fits the rest of the workflow, including the ruleset, CODEOWNERS and the derived
worklist, is in [`docs/repository-enforcement.md`](../../docs/repository-enforcement.md). The
action is built and tested in
[`noru-tech/noru-grc-engineering`](https://github.com/noru-tech/noru-grc-engineering), which
also holds the [contribution guide](../../CONTRIBUTING.md) and the
[security policy](../../SECURITY.md). Open issues and pull requests there, not in the Marketplace
repository, whose tree is overwritten on every release.
