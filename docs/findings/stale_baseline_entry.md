# `stale_baseline_entry`

**Rule:** a ratchet-baseline entry whose fingerprint matches no current violation must be removed —
resolved debt is cleaned up in the same pull request.

| | |
|---|---|
| Emitted by | the `enforce` action (annotation `Noru GRC stale baseline`) and `enforce.py` (`stale_baseline_entries` in the report) |
| Gates by default | yes |
| Accepted in a ratchet baseline | no |

## Why it matters

An entry that no longer matches anything is either fixed debt that nobody cleaned up, or a detector
that silently stopped reporting. Either way a reviewer should look: left in place, the entry would
quietly accept the violation if it came back.

A violation that *changed* — the same rule on the same subject with different details — produces
both a new violation and a stale entry, because the fingerprint covers the whole normalized
violation (line numbers excepted).

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
action prints the same violations as GitHub annotations. An accepted entry's fingerprint no longer matches the current `ai-inventory/drift`
violation:

```text
$ python3 plugins/repo-enforcement/scripts/enforce.py baseline check --repo=. --as-of=2026-09-04 --output=text
Repository enforcement: FAIL
new violations: 4
baselined violations: 2
expired exceptions: 0
stale baseline entries: 1
  FAIL [ai-inventory/drift] .noru/ai-inventory.yml: no committed manifest at .noru/ai-inventory.yml — run the piece's :scan and commit it
  ...
```

The action's annotations:

```text
::error title=Noru GRC ai-inventory/drift::.noru/ai-inventory.yml — no committed manifest at .noru/ai-inventory.yml — run the piece's :scan and commit it (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/drift.md)
::error title=Noru GRC stale baseline::.noru/ai-inventory.yml (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/stale_baseline_entry.md)
```

## Passing example

Every baseline entry's fingerprint matches a current violation: `stale baseline entries: 0`.

## How to fix

Confirm why the violation disappeared (fixed, or the detector changed), then delete the entry from
`.noru/enforcement-baseline.json` in the same reviewed pull request. If the violation mutated,
review the new one: accept it as a new entry or fix it.

## Recording a disposition

None: removing the entry is the disposition.
