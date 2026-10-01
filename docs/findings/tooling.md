# `tooling`

**Rule:** a check that could not run is not a check that passed — a missing runtime, an unreadable
declaration, a crashed child process, a baseline path that does not exist, or a required piece the
release does not ship fails the build in every mode.

| | |
|---|---|
| Emitted by | `scripts/ci_check.py` as exit `6` and status `error` (no finding kind in its report), so `noru-ci` and `noru-review`; the `enforce` action and `enforce.py` as rule `tooling` |
| Gates by default | yes — exit `6`, and `--mode=warn` does not suppress it |
| Accepted in a ratchet baseline | no — a tooling failure cannot be baselined |

## Why it matters

A gate that silently stops running looks exactly like a gate that passes. Exit `6` is the code
[ci-mode.md](../ci-mode.md#exit-codes) calls the one that matters most and the one people forget. A
collector that parsed no schema in a repository that visibly has one is reported the same way, with
a [`coverage`](./coverage.md) finding attached.

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Failing example — CI mode

A `--baseline=` path that does not exist:

```text
$ python3 scripts/ci_check.py --piece=ai-inventory --repo=. --baseline=.noru/missing-baseline.yml --output=text --quiet
ERROR: a check could not run — policy: --baseline=.noru/missing-baseline.yml does not exist
This is a tooling failure, not a compliance finding.
$ echo $?
6
```

## Failing example — repository enforcement

A policy requiring a piece the released registry does not contain:

```text
  FAIL [ai-inventory/tooling] pieces.ai-inventory: required piece is absent from the released registry
```

## Passing example

Every step either ran (`ok`, `FAIL`) or reported `skipped`/`blocked` for want of input. Without
`--on-missing-prerequisite=fail`, a step with no input is a skip, not a tooling failure.

## How to fix

Read the step and detail on the `ERROR` line. The usual causes: `node` or `python3` missing from the
runner (the actions install nothing), a wrong `--baseline`/`--state` path, a `plugins` input
pointing somewhere without the piece, or a policy naming a piece the pinned release does not ship.

## Recording a disposition

None. Exit `6` is loud in warn mode on purpose, and repository enforcement never baselines it.
