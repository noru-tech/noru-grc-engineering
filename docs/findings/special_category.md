# `special_category`

**Rule:** GDPR Article 9 special-category data, or Article 10 criminal-offence data, anywhere in
the data map is reported so a reviewer never has to go looking for it.

| | |
|---|---|
| Emitted by | `scripts/check_policy.py`, run as the policy step of `scripts/ci_check.py` — so the `noru-ci` and `noru-review` actions. The `enforce` action does not run the policy step |
| Gates by default | no — advisory. `--fail-on=special_category` makes it exit `7` |
| Baseline it is checked against | `.noru/privacy-baseline.yml` (or `--baseline=`). No baseline: the step reports `skipped`, never a pass |

## Why it matters

It is the highest-risk thing in a data map. Whether the data is *permitted* is what
[`unpermitted_category`](./unpermitted_category.md) answers; this finding is advisory so one
condition does not fail twice. The list of Fideslang keys treated as special is
`contract/lib/taxonomy/special_categories.json`, which states its basis: Regulation (EU) 2016/679
Article 9(1) and Article 10. It deliberately reads broadly — the whole `user.biometric` subtree is
listed even though Article 9 covers biometric data only when processed for unique identification.

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

The legal basis the repository does state is the one above: GDPR Article 9(1) and Article 10, as
recorded in `contract/lib/taxonomy/special_categories.json`.

## Example

The examples below run the `privacy-datamap` fixture map that `scripts/test_ci_mode.py` builds
from `tests/fixture-repo` against a baseline that permits it — `data_categories.allow:
[user.contact, user.device, user.authorization]`, `data_uses.allow: [essential]`,
`data_subjects.allow: [customer]` — with the one change named in each example. Here one field is classified `user.health_and_medical`, and the baseline allows it:

```text
$ python3 scripts/ci_check.py --piece=privacy-datamap --repo=. --output=text
privacy-datamap in /path/to/repo (gate mode)
  ok      scan
  ok      validate
  ok      expiry
  ok      policy: against /path/to/repo/.noru/privacy-baseline.yml

  warn [special_category] dataset[1].collections[0].fields[2]: 'user.health_and_medical' is GDPR Article 9 or Article 10 data — the highest-risk thing in this map, surfaced so nobody has to go looking for it (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/special_category.md)

OK: every requested check passed.
```

A map with no special-category key produces no `special_category` line.

## How to fix

Nothing to fix if the processing is agreed: the finding is a pointer for review. Confirm the
classification is right (a stored photograph is biometric data without necessarily being Article 9
data) and that the baseline's category rules reflect the decision.

## Recording a disposition

Advisory by default. To require a second pair of eyes on every one, gate on it with
`--fail-on=special_category`. Narrow what counts per system in the baseline's `scopes`, with a
rationale — never by editing the vocabulary.
