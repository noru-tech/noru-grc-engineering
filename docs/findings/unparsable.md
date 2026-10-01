# `unparsable`

**Rule:** a date in an interpretation block (or a record-level `expiry_date`) that cannot be read as
`YYYY-MM-DD` fails the build — an expiry that cannot be compared cannot be trusted.

| | |
|---|---|
| Emitted by | `scripts/check_expiry.py` / the expiry step of `scripts/ci_check.py` |
| Gates by default | yes — exit `4` from `ci_check.py`, `1` from `check_expiry.py` |
| Accepted in a ratchet baseline | yes, though repository enforcement always gates on it: a per-piece `fail_on` list cannot remove it |

## Why it matters

Every date in a manifest is an ISO string by contract. The check is deliberately strict about the
type: the two YAML loaders (PyYAML and the bundled fallback) have disagreed before about whether an
unquoted `2026-08-01` is a date object or a string, and accepting both would hide the next such
divergence. In practice the piece validators reject most malformed dates first, as
[`invalid`](./invalid.md); `unparsable` is what `check_expiry.py` reports when it reads a manifest
directly, or when a loader hands it something that is not a string.

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Failing example

`check_expiry.py` run directly on an `ai-inventory` manifest with one `expires_at: next-spring`:

```text
$ python3 scripts/check_expiry.py .noru/ai-inventory.yml --as-of=2026-08-27 --quiet
  ERROR [unparsable] providers[0].claims[0] "Zero data retention enabled for this account": 'next-spring' is not a date this check can compare (expected YYYY-MM-DD) (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/unparsable.md)

FAILED: 1 claim finding(s) that --fail-on covers.
```

Through `ci_check.py`, the validator catches the same value first:

```text
  FAIL [invalid] providers[0].claims[0].interpretation.expires_at: 'next-spring' is not an ISO date (YYYY-MM-DD) (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/invalid.md)
```

## Passing example

```yaml
interpretation:
  decided_at: 2026-08-01
  expires_at: 2027-02-01
```

## How to fix

Write the date as `YYYY-MM-DD` (an ISO timestamp is accepted; its date part is used). If the value
is correct but the runner's YAML loader resolved it to another type, the action input
`require-yaml-loader` pins the loader you tested against
([ci-mode.md](../ci-mode.md#what-the-action-assumes-about-the-runner-stated-out-loud)).

## Recording a disposition

Fix the date; there is rarely a reason to carry one. [Repository
enforcement](../repository-enforcement.md#install-and-adoption) always gates on `unparsable` — a
per-piece `fail_on` list cannot remove it — but an exact violation can be accepted in a ratchet
baseline with a named owner and an expiry, like other baselineable debt.
