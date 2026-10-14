# `invalid`

**Rule:** the committed manifest must pass its piece's validator — every claim attributed, every
vocabulary value real, every required field present.

| | |
|---|---|
| Emitted by | `scripts/ci_check.py` validate step (one finding per validator error), so all three actions |
| Gates by default | yes — exit `5` from `ci_check.py`; the piece validator itself exits `1` |
| Accepted in a ratchet baseline | no — an invalid record cannot be baselined |

## Why it matters

A manifest that does not validate is not a record anyone can rely on: an unattributed claim, a
data category that is not a Fideslang key, an Article 50 trigger with no disclosure verdict. Contract
requirement 8 makes an unattributed claim a validator **error**, not a warning
([contract](../../contract/README.md#the-nine-requirements)). While validation fails, the expiry and
policy steps report `blocked`: there is nothing trustworthy to age or check.

Piece validators have no stable error codes. Each error is a manifest path and a message that says
what to do next; CI mode turns each one into an `invalid` finding. Two of them have their own names
under repository enforcement: [`needs_review`](./needs_review.md) and
[`missing_interpretation`](./missing_interpretation.md).

`privacy-datamap` also reports `invalid` when its reconciler finds decision evidence or baseline
metadata that needs review ("this is not a confirmed privacy change").

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Failing example

The `ai-inventory` fixture manifest with one claim's `owner` removed:

```text
$ python3 scripts/ci_check.py --piece=ai-inventory --repo=. --output=text --quiet
  FAIL [invalid] providers[0].claims[0].interpretation.owner: missing or too short — must name a person, not a team alias (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/invalid.md)

FAILED (5): see docs/ci-mode.md for what this exit code means.
```

The piece validator on its own, against a shipped invalid fixture:

```text
$ python3 plugins/ai-inventory/scripts/validate_manifest.py plugins/ai-inventory/fixtures/invalid-unattributed-claim.ai-inventory.yml --quiet
  ERROR providers[0].interpretation.expires_at: no `expires_at` and no `next_review_due` — a procedural claim runs on a review cadence, so say when someone must look at this again. An open-ended claim is one nobody will ever revisit
  ERROR ai_systems[0]: missing required `refs` — every claim must cite the repository lines (file:line) that produced it

FAILED: 2 error(s), 0 warning(s).
```

## Passing example

```text
$ python3 plugins/ai-inventory/scripts/validate_manifest.py plugins/ai-inventory/fixtures/valid.ai-inventory.yml --quiet
$ echo $?
0
```

## How to fix

Read the path and message: each one names the field and what it needs. Fix the manifest (or re-run
`:scan` if the structure is wrong), then re-run the piece validator:

```bash
python3 plugins/<piece>/scripts/validate_manifest.py .noru/<piece>.yml
```

An agent's renewal in `privacy-datamap` is invalid in the same way when it breaks a rule of
[requirement 8](../../contract/README.md#on-requirement-8-and-why-it-is-worth-the-friction): no
`operator`, an operator that is a team alias, an agent or operator outside the grant it names, an
expiry past the agent ceiling, or a collection that is new or has changed since a person signed it.
The message names which:

```text
  FAIL [invalid] dataset[0].collections[0].interpretation.operator: missing required `operator` — an agent owner needs a named person who can be asked what it was thinking
```

The fix for a changed or new collection is a person's signature, not a corrected renewal. To check
a renewal locally the way CI does, pass the base revision's manifest and policy:

```bash
git show origin/main:.noru/privacy-datamap.yml > /tmp/base.yml
git show origin/main:.noru/interpretation-policy.yml > /tmp/policy.yml
python3 plugins/privacy-datamap/scripts/validate_manifest.py .noru/privacy-datamap.yml \
  --base=/tmp/base.yml --interpretation-policy=/tmp/policy.yml
```

## Recording a disposition

None. `invalid` gates in gate mode, and `--mode=warn` or `--fail-on` without `invalid` only make it
advisory in CI mode. [Repository enforcement](../repository-enforcement.md#install-and-adoption)
never lets an invalid record into a baseline, and a per-piece `fail_on` list cannot remove it.
