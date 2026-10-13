# `invalid_baseline`

**Rule:** the repository-enforcement ratchet baseline (`.noru/enforcement-baseline.json`) must be
well-formed, bound to the current enforcement policy, and every entry must name a person, a
rationale, a decision date and a later expiry.

| | |
|---|---|
| Emitted by | the `enforce` action and `enforce.py`, as piece `repo-enforcement` |
| Gates by default | yes |
| Accepted in a ratchet baseline | no |

## Why it matters

The baseline is the list of debt a team has knowingly accepted. An entry with no named owner, no
reason, or no end date is not an acceptance — it is a way to switch the gate off. A baseline whose
`policy_digest` does not match the current `.noru/enforcement.yml` was agreed under different rules,
so none of it can be trusted.

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
action prints the same violations as GitHub annotations. One accepted entry's `owner` is a team handle:

```text
$ python3 plugins/repo-enforcement/scripts/enforce.py baseline check --repo=. --as-of=2026-09-04 --output=text
Repository enforcement: FAIL
...
  FAIL [repo-enforcement/invalid_baseline] /path/to/repo/.noru/enforcement-baseline.json: baseline.violations[0].owner must name a person
```

Other messages: `baseline file is missing at …`, `baseline.version must be 1`,
`… .fingerprint is invalid` / `is duplicated`, `… .rationale must explain the temporary
acceptance`, `… .expires_at must be after decided_at`, and `baseline.policy_digest does not match
the current enforcement policy`.

## Passing example

```json
{
  "version": 1,
  "policy_digest": "<digest of the current .noru/enforcement.yml>",
  "violations": [
    {
      "piece": "ai-inventory",
      "rule": "drift",
      "subject": ".noru/ai-inventory.yml",
      "fingerprint": "sha256:<64 hex>",
      "owner": "Dana Okafor",
      "rationale": "Manifest lands in the next sprint; tracked in the GRC backlog.",
      "decided_at": "2026-09-01",
      "expires_at": "2026-09-20"
    }
  ]
}
```

## How to fix

Correct the entry the message names. After a policy change, regenerate a candidate with
`enforce.py baseline propose`, review it, and fill in owner and rationale for each entry you accept.

## Recording a disposition

None: the baseline is where dispositions are recorded, so it cannot excuse itself.
