# `expired_exception`

**Rule:** an accepted ratchet-baseline entry stops accepting its violation when its `expires_at` is
before the check date, or when its window is longer than the policy's `exceptions.maximum_days`.

| | |
|---|---|
| Emitted by | the `enforce` action (annotation `Noru GRC expired baseline`) and `enforce.py` (`expired_exceptions` in the report) |
| Gates by default | yes |
| Accepted in a ratchet baseline | no — it is a baseline entry that has run out |

## Why it matters

Temporary acceptance has to be temporary. An exception nobody renewed is debt nobody owns.

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Failing example

The examples run `plugins/repo-enforcement/scripts/enforce.py` against the throwaway repository
`scripts/test_repo_enforcement.py` sets up with `/repo-enforcement:setup` (ratchet mode; pieces
`ai-inventory`, `iac-scan` and `privacy-datamap` required), checked as of 2026-09-04. The `enforce`
action prints the same violations as GitHub annotations. The entry accepting `ai-inventory/drift` was decided 2026-08-01 and expired 2026-09-03:

```text
$ python3 plugins/repo-enforcement/scripts/enforce.py baseline check --repo=. --as-of=2026-09-04 --output=text
Repository enforcement: FAIL
...
expired exceptions: 1
```

The action's annotation:

```text
::error title=Noru GRC expired baseline::.noru/ai-inventory.yml (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/expired_exception.md)
```

## Passing example

The same entry with `decided_at: 2026-09-01` and `expires_at: 2026-09-20` (inside the 30-day
maximum) is counted under `baselined violations`.

## How to fix

Fix the underlying violation, then remove the entry (it becomes a
[`stale_baseline_entry`](./stale_baseline_entry.md) otherwise). `enforce.py baseline worklist`
lists entries by urgency, including those due within seven days.

## Recording a disposition

If the debt must stay, a named person makes a fresh, reviewed decision: a new `decided_at`, a new
`expires_at` within `exceptions.maximum_days`, and a rationale. That is a new acceptance, made in a
pull request, not an automatic renewal.
