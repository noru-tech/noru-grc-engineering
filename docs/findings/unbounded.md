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

`check_expiry.py` counts a claim as bounded when any of these holds a date it can read:
`interpretation.expires_at`, `interpretation.next_review_due` (`ai-inventory`'s procedural claims),
or the record-level `expiry_date` or `next_review_due` (`governance-records`). A `next_review_due`
is compared like an expiry: in the past it is [`expired`](./expired.md), inside the warning window
it is [`expiring`](./expiring.md). See
[ci-mode.md, "An expired interpretation"](../ci-mode.md#2-an-expired-interpretation).

A date that cannot be read bounds nothing: the claim is reported as `unbounded` and the date as
[`unparsable`](./unparsable.md).

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Example

The `evidence-push` fixture manifest with every `expires_at` and `expiry_date` line removed, checked
directly:

```text
$ python3 scripts/check_expiry.py .noru/evidence-push.yml --as-of=2026-08-27
expiry check of .noru/evidence-push.yml as of 2026-08-27 (2 claim(s))
  warn  [unbounded] uploads[0] "Q2 2026 quarterly access review": no expiry — acceptable only for a genuinely point-in-time procedural claim, and then the rationale has to say so (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/unbounded.md)
  warn  [unbounded] uploads[1] "2026 external penetration test report": no expiry — acceptable only for a genuinely point-in-time procedural claim, and then the rationale has to say so (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/unbounded.md)

OK: 2 claim(s), 0 expired, 0 outside cadence, 0 expiring soon, 2 unbounded.
```

A claim with an `expires_at` or a `next_review_due` produces no `unbounded` line: the valid
`ai-inventory` fixture's two procedural claims carry only `next_review_due`, and report nothing as
of 2026-08-27.

## How to fix

If the claim can go stale, give it an `expires_at`. If it is procedural and runs on a review cadence,
give it a `next_review_due` where the piece accepts one, and say so in the `rationale`, as the
`ai-inventory` fixture does ("Procedural obligation reviewed on the contract renewal cadence rather
than on a technical expiry, so no expires_at is set"). Either date is then checked like any expiry.

## Recording a disposition

None needed; it is advisory.
