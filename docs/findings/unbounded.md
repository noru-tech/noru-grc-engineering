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

`check_expiry.py` reads `interpretation.expires_at`, the record-level `expiry_date`, and — in place
of `expires_at` — a `next_review_due` review date, in the interpretation block (`ai-inventory`) or
on the claim itself (`governance-records`). A claim bounded by a review date is not `unbounded`: it
is aged against that date, as [`expired`](./expired.md) or [`expiring`](./expiring.md). Releases up
to 0.9.1 ignored `next_review_due` and reported such claims here.

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Example

The piece validators reject most open-ended claims (`ai-inventory`: "no `expires_at` and no
`next_review_due`"), so `unbounded` mostly shows up when `check_expiry.py` reads a manifest
directly. The valid `ai-inventory` fixture with the DPA claim's `next_review_due` removed:

```text
$ python3 scripts/check_expiry.py .noru/ai-inventory.yml --as-of=2026-08-27
expiry check of .noru/ai-inventory.yml as of 2026-08-27 (12 claim(s))
  warn  [unbounded] providers[0].claims[2] "DPA executed 2026-03-14, standard contractual clauses annexed": no expiry — acceptable only for a genuinely point-in-time procedural claim, and then the rationale has to say so (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/unbounded.md)

OK: 12 claim(s), 0 expired, 0 outside cadence, 0 expiring soon, 1 unbounded.
```

The unmodified fixture, where both procedural claims carry `next_review_due: 2027-02-01`, reports
nothing:

```text
OK: 12 claim(s), 0 expired, 0 outside cadence, 0 expiring soon, 0 unbounded.
```

## How to fix

If the claim can go stale, give it an `expires_at`. If it is reviewed on a cadence rather than
expiring, give it a `next_review_due` where the piece accepts one, as the fixture does
("Procedural obligation reviewed on the contract renewal cadence rather than on a technical expiry,
so no expires_at is set"). If it is genuinely point-in-time, say so in the `rationale`.

## Recording a disposition

None needed; it is advisory.
