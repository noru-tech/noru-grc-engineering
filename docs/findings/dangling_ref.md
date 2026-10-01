# `dangling_ref`

**Rule:** every `file:line` citation in a manifest must still resolve — the file exists and has at
least that many lines.

| | |
|---|---|
| Emitted by | `scripts/ci_check.py` expiry step |
| Gates by default | no — advisory; with `--fail-on=dangling_ref` it exits `3`, alongside drift |
| Accepted in a ratchet baseline | not applicable by default: repository enforcement does not gate on it unless a piece's `fail_on` lists it |

## Why it matters

`refs[]` is how a reviewer checks a claim against the code that produced it (contract requirement 8).
A citation that points at a deleted file or past the end of one means the record has come loose from
the code, which is the same class of failure as [`drift`](./drift.md) — hence the shared exit code.

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Failing example

The `ai-inventory` fixture manifest with one citation re-pointed at a file that does not exist:

```text
$ python3 scripts/ci_check.py --piece=ai-inventory --repo=. --output=text
...
  warn [dangling_ref] providers[0].claims[0].source.ref: citation no longer resolves: src/gone.ts no longer exists in the repository (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/dangling_ref.md)

OK: every requested check passed.
```

## Passing example

No `dangling_ref` line: every cited file exists and is long enough. Files over 4 MB are not opened
and are treated as resolving.

## How to fix

Re-run the piece's `:scan` so the collector re-derives the citations, or correct the `refs[]` entry
by hand to the line that now holds the evidence.

## Recording a disposition

It is advisory unless you opt in with `--fail-on`, so no disposition is needed. If you gate on it,
the same options as [`drift`](./drift.md#recording-a-disposition) apply.
