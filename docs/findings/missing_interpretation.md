# `missing_interpretation`

**Rule:** every claim carries an interpretation block — `owner`, `decided_at`, `expires_at`,
`rationale` — and a claim without one cannot pass repository enforcement.

| | |
|---|---|
| Emitted by | the `enforce` action and `enforce.py`. CI mode reports the same validator error as [`invalid`](./invalid.md); enforcement names it from the message |
| Gates by default | yes — always, under repository enforcement |
| Accepted in a ratchet baseline | yes |

## Why it matters

This is contract requirement 8: an unattributed claim is a validator error, not a warning
([contract](../../contract/README.md#the-nine-requirements)). So much compliance evidence is, in
substance, "a named person decided X on date Y" that a claim without one is not a claim.

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
action prints the same violations as GitHub annotations.

```text
  FAIL [privacy-datamap/missing_interpretation] system[0].privacy_declarations[0]: missing required `interpretation` — name the person who decided this, when, until when, and why
```

The same error from a piece validator, on a shipped fixture:

```text
$ python3 plugins/ai-inventory/scripts/validate_manifest.py plugins/ai-inventory/fixtures/invalid-missing-interpretation.ai-inventory.yml --quiet
  ...
  ERROR ai_systems[0]: missing required `interpretation` — name the person who decided this, when, until when, and why (owner, decided_at, expires_at, rationale)
```

## Passing example

```yaml
interpretation:
  owner: dana.reed@example.com
  decided_at: 2026-08-01
  expires_at: 2027-02-01
  rationale: >
    The client is constructed with store disabled and the account-level retention setting is
    asserted in the same module, so the claim is ours to make and ours to keep true.
```

## How to fix

Add the block. `owner` names a person, not a team alias; `expires_at` says when it must be looked at
again.

## Recording a disposition

As for [`needs_review`](./needs_review.md#recording-a-disposition): an exact violation can be
accepted in the ratchet baseline with a named person and an expiry.
