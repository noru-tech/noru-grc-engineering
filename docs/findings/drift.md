# `drift`

**Rule:** the committed `.noru/<piece>.yml` must describe the repository as it is now — when the
piece's collector re-runs and its derived digest no longer matches `source.derived_digest`, the
build fails.

| | |
|---|---|
| Emitted by | `scripts/ci_check.py` scan step, so the `noru-ci` and `noru-review` actions and the `enforce` action |
| Gates by default | yes — exit `3` from `ci_check.py` |
| Accepted in a ratchet baseline | yes, as an exact fingerprint with a named owner and an expiry |

## Why it matters

The manifest is the reviewed record of what the repository contains: model providers, schemas,
artifacts. When the code changes and the record does not, the record is wrong, and everything that
lands in Noru from it is wrong too. The check is a hash of local files compared against a field in
a committed file — no network, no credential — so it runs on a fork pull request.

Three situations produce the same finding and the message says which one you are in: no committed
manifest, a manifest with no recorded digest, or a manifest whose digest no longer matches. For
`privacy-datamap`, a changed accepted observation lock is reported as `drift` as well.

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

For `ai-inventory`, the piece states what it targets: `iso_42001` and `eu_ai_act`
([piece README](../../plugins/ai-inventory/README.md)).

## Failing example

`tests/fixture-repo` with a valid `ai-inventory` manifest, after a pull request adds a new model
call in `src/summarize.ts` (the case `scripts/test_ci_mode.py` runs):

```text
$ python3 scripts/ci_check.py --piece=ai-inventory --repo=. --output=text --quiet
  FAIL [drift] .noru/ai-inventory.yml: the committed manifest no longer matches the repository (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/drift.md)
         present in the repository, named nowhere in the manifest:
           + frameworks[0]: vercel-ai-sdk  (first seen at src/summarize.ts:2)
           + models[0]: claude-sonnet-4-5  (first seen at src/agent.py:4)
           + providers[0]: anthropic  (first seen at src/agent.py:2)
           ...

FAILED (3): see docs/ci-mode.md for what this exit code means.
```

The list under the finding is an explanation, not the gate: it compares the repository against the
manifest text, so it can include things that were already unnamed before this change (here, eight
further rows were trimmed). See [ci-mode.md, "Manifest drift"](../ci-mode.md#1-manifest-drift).

With no manifest committed at all:

```text
  FAIL [drift] .noru/ai-inventory.yml: no committed manifest at .noru/ai-inventory.yml — run the piece's :scan and commit it (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/drift.md)
```

## Passing example

The same repository after `/ai-inventory:scan` has been re-run and the manifest committed:

```text
$ python3 scripts/ci_check.py --piece=ai-inventory --repo=. --output=text
ai-inventory in /path/to/repo (gate mode)
  ok      scan
  ok      validate
  ok      expiry
  skipped policy: no privacy baseline at .noru/privacy-baseline.yml — agree one and commit it, or pass --baseline. This step has no default policy of its own
...
OK: every requested check passed.
```

## How to fix

1. Run the piece's `:scan` (`/ai-inventory:scan`, `/privacy-datamap:scan`, …). The collector
   re-derives the facts and stamps the new digest.
2. Review the manifest diff. New entries arrive with `needs_review` or without an interpretation
   block; fill those in — a person has to stand behind each claim.
3. Commit the manifest in the same pull request as the code change.

If the itemised list is empty, the difference is in line positions or counts; re-run `:scan` and
read the manifest diff.

## Recording a disposition

- **While adopting:** run in [warn-only mode](../ci-mode.md#warn-only-mode), or keep drift advisory
  with `--fail-on=expired` (or any list without `drift`) while the team catches the manifest up.
- **Under [repository enforcement](../repository-enforcement.md#install-and-adoption):** a drift
  violation can be accepted in a ratchet baseline as an exact fingerprint with a named owner, a
  rationale, a decision date and an expiry. When the drift is fixed, the entry becomes a
  [`stale_baseline_entry`](./stale_baseline_entry.md) and must be removed in the same pull request.
- There is no `--base-ref` delta for drift: the collector's previous derived facts are not
  committed, so the check cannot say what this pull request changed
  ([ci-mode.md](../ci-mode.md#where-this-is-weaker-than-it-looks)).
