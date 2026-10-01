# `cadence`

**Rule:** when a pipeline declares a review cadence with `--max-age-days=N`, a claim last decided
more than N days ago, or declaring a review window longer than N days, fails the build.

| | |
|---|---|
| Emitted by | `scripts/check_expiry.py` / the expiry step of `scripts/ci_check.py`; only when `--max-age-days` (action input `max-age-days`) is above `0` |
| Gates by default | yes when a cadence is declared — exit `4`. With the default `0` it never fires |
| Accepted in a ratchet baseline | yes |

## Why it matters

Nothing offline can tell whether an `expires_at` was chosen thoughtfully or set two years out to stop
the build complaining. `--max-age-days` is the ceiling a team puts on that for every piece, including
pieces with no cadence field of their own. It is off by default: the tool does not invent a
compliance opinion you did not state. A cadence a manifest declares itself (`review-signoff`'s
`cadence: quarterly`) is enforced by that piece's validator and surfaces as
[`invalid`](./invalid.md) instead. See
[ci-mode.md, "Two kinds of cadence"](../ci-mode.md#two-kinds-of-cadence-and-who-checks-which).

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Failing example

The valid `ai-inventory` fixture manifest (claims decided 2026-08-01, expiring 2027-02-01) under a
90-day cadence:

```text
$ python3 scripts/ci_check.py --piece=ai-inventory --repo=. --as-of=2026-08-27 --max-age-days=90 --output=text --quiet
  FAIL [cadence] ai_systems[0]: declares a 184-day review window, longer than the 90-day cadence declared for this path (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/cadence.md)
  ...
  FAIL [cadence] providers[0].claims[2]: last decided 160 day(s) ago, past the 90-day review cadence declared for this path (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/cadence.md)

FAILED (4): see docs/ci-mode.md for what this exit code means.
```

## Passing example

The same manifest under `--max-age-days=365`, or with no cadence declared, reports no `cadence`
finding.

## How to fix

Shorten the declared window (`expires_at` no more than N days after `decided_at`), or re-decide a
claim whose `decided_at` is older than the cadence. If the cadence is wrong for this path, change
`--max-age-days` in the workflow — a reviewed change to the gate.

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
