# Noru GRC repository enforcement action

Runs every piece required by `.noru/enforcement.yml` against the whole checkout. It uses the
collectors, validators, and generated registry shipped in the pinned release, not executable code
from the target repository. It has no network step, reads no Noru credential, and requires an
explicit `as-of` date.

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

The job name and check name matter: `.noru/enforcement.yml` names the required check, and the
ruleset `/repo-enforcement:plan` writes requires that exact check. The date is passed in rather than
read by the action so that a run is reproducible from its log.

| Input | Default | Meaning |
|---|---|---|
| `as-of` | required | UTC date (`YYYY-MM-DD`) used for expiry and exception checks |
| `repo` | `.` | Repository to validate |
| `policy` | `.noru/enforcement.yml` | Committed enforcement policy |
| `report-path` | `$RUNNER_TEMP/noru-grc-enforcement.json` | Where the JSON report is written |

Outputs: `report` (path), `new-violations` and `baselined-violations` (counts). The job fails on
any new violation, expired exception or stale baseline entry, and every one is annotated in the
log and the job summary.

The supported path is the workflow that `/repo-enforcement:setup` generates. It is this example
with the action pinned to a full commit SHA of `noru-tech/noru-grc-engineering/actions/enforce`,
recorded in the policy, because `/repo-enforcement:verify` recognises only that form and reports a
workflow whose pin moved. `@v0` follows the newest 0.x release and suits a hand-written workflow
that is not under that verification; pin a release tag from
[the releases page](https://github.com/noru-tech/noru-grc-engineering/releases) or a commit SHA to
take changes only when you choose to. On the GitHub Marketplace this action is
`noru-tech/noru-enforce-action`, generated from the same tag.
