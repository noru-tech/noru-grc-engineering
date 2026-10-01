# `expiring`

**Rule:** a claim whose expiry falls inside the warning window (default 30 days) is reported, so it
can be re-owned before it becomes [`expired`](./expired.md).

| | |
|---|---|
| Emitted by | `scripts/check_expiry.py` / the expiry step of `scripts/ci_check.py` |
| Gates by default | no — advisory. `--fail-on=expiring` makes it exit `4` |
| Accepted in a ratchet baseline | not gated by repository enforcement by default |

## Why it matters

A heads-up, not a gate: the claim is still current, and the message names the owner to ask. The
window is `--warn-within-days` (action input `warn-within-days`).

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Example

The `ai-inventory` fixture manifest with every `expires_at` set to 2026-09-10, checked as of
2026-08-27:

```text
$ python3 scripts/ci_check.py --piece=ai-inventory --repo=. --as-of=2026-08-27 --output=text
...
  warn [expiring] ai_systems[0]: expires in 14 day(s); ask dana.reed@example.com to re-own it before then (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/expiring.md)
...
OK: every requested check passed.
```

It does not fail the build: `OK`, exit `0`. With `--warn-within-days=7` the same manifest reports
nothing for these claims.

## How to fix

Re-own the claim before the date: update `decided_at`, `expires_at` and `rationale`.

## Recording a disposition

None needed; it is advisory. To silence the noise for a window, lower `--warn-within-days`.
