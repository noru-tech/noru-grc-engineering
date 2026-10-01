# `unbounded`

**Rule:** a claim with no expiry the check can read is reported — permitted by the contract only for
a genuinely point-in-time procedural claim, and then the rationale has to say so.

| | |
|---|---|
| Emitted by | `scripts/check_expiry.py` / the expiry step of `scripts/ci_check.py` |
| Gates by default | no — advisory. `--fail-on=unbounded` makes it exit `4` |
| Accepted in a ratchet baseline | not gated by repository enforcement by default |

## Why it matters

A claim that never expires is never looked at again. Some claims are legitimately point-in-time ("the
DPA was executed on 2026-03-14"), so the contract allows them; this finding makes each one visible.

`check_expiry.py` reads `interpretation.expires_at` and the record-level `expiry_date`. A claim
bounded by another field — `ai-inventory`'s `next_review_due` on a procedural claim — is reported as
`unbounded` too, and a `next_review_due` in the past is not reported as `expired`.

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Example

The valid `ai-inventory` fixture manifest has two procedural claims that use `next_review_due`:

```text
$ python3 scripts/check_expiry.py .noru/ai-inventory.yml --as-of=2026-08-27
expiry check of .noru/ai-inventory.yml as of 2026-08-27 (12 claim(s))
  warn  [unbounded] providers[0] "example-llm": no expiry — acceptable only for a genuinely point-in-time procedural claim, and then the rationale has to say so (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/unbounded.md)
  warn  [unbounded] providers[0].claims[2] "DPA executed 2026-03-14, standard contractual clauses annexed": no expiry — acceptable only for a genuinely point-in-time procedural claim, and then the rationale has to say so (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/unbounded.md)

OK: 12 claim(s), 0 expired, 0 outside cadence, 0 expiring soon, 2 unbounded.
```

A claim with an `expires_at` produces no `unbounded` line.

## How to fix

If the claim can go stale, give it an `expires_at`. If it is genuinely point-in-time, say so in the
`rationale`, as the fixture does ("Procedural obligation reviewed on the contract renewal cadence
rather than on a technical expiry, so no expires_at is set").

## Recording a disposition

None needed; it is advisory.
