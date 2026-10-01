# `expired`

**Rule:** a claim whose `interpretation.expires_at` (or record-level `expiry_date`) is in the past
fails the build — nobody has stood behind it since it went stale.

| | |
|---|---|
| Emitted by | `scripts/check_expiry.py`, run as the expiry step of `scripts/ci_check.py`, so all three actions |
| Gates by default | yes — exit `4` from `ci_check.py`, `1` from `check_expiry.py` |
| Accepted in a ratchet baseline | yes |

## Why it matters

Contract requirement 8 puts an interpretation block on every claim: who decided it, when, until
when, and why ([contract](../../contract/README.md#the-nine-requirements)). The validators check the
block is well-formed and deliberately ignore the calendar; CI mode is where time is checked. An
evidence record that expires in Noru next week is not evidence of anything the week after, which is
why `expiry_date` on an upload is compared the same way. The privacy baseline carries an
interpretation block too: `check_expiry.py .noru/privacy-baseline.yml` ages it (the `ci_check.py`
expiry step ages the piece manifest, not the baseline). See
[ci-mode.md, "An expired interpretation"](../ci-mode.md#2-an-expired-interpretation).

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Failing example

The `ai-inventory` fixture manifest with every `expires_at` set to `2020-01-01`, checked as of
2026-08-27:

```text
$ python3 scripts/ci_check.py --piece=ai-inventory --repo=. --as-of=2026-08-27 --output=text --quiet
  FAIL [expired] ai_systems[0]: expired 2430 day(s) ago; dana.reed@example.com owned it — nobody has stood behind this claim since it went stale (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/expired.md)
  FAIL [expired] findings.prohibited_practices[0]: expired 2430 day(s) ago; sam.okafor@example.com owned it — nobody has stood behind this claim since it went stale (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/expired.md)
  ...

FAILED (4): see docs/ci-mode.md for what this exit code means.
```

## Passing example

An interpretation block with an expiry in the future:

```yaml
interpretation:
  owner: dana.reed@example.com
  decided_at: 2026-08-01
  expires_at: 2027-02-01
  rationale: >
    The client is constructed with store disabled and the account-level retention setting is
    asserted in the same module, so the claim is ours to make and ours to keep true.
```

```text
$ python3 scripts/check_expiry.py .noru/ai-inventory.yml --as-of=2026-08-27 --quiet
$ echo $?
0
```

## How to fix

Ask the named owner (or whoever now owns the decision) to look at the claim again. If it still
holds, update `decided_at` and `expires_at` and say why in `rationale`; if it does not, change or
remove the claim and re-run `:scan`.

## Recording a disposition

- **Re-own it.** The fix is always a person: update `owner`, `decided_at`, `expires_at` and
  `rationale` in the claim's interpretation block, in a reviewed pull request.
- **While adopting:** [warn-only mode](../ci-mode.md#warn-only-mode) reports the identical findings
  and exits `0`; `--fail-on` narrows which kinds gate.
- **Under [repository enforcement](../repository-enforcement.md#install-and-adoption):** a
  violation of this kind can be accepted in a ratchet baseline as an exact fingerprint with a named
  person, a rationale, a decision date and an expiry no longer than the policy's
  `exceptions.maximum_days`. An acceptance that has itself expired is an
  [`expired_exception`](./expired_exception.md).
