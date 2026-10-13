# `unpermitted_pair`

**Rule:** a category and a use that are each permitted on their own must not appear together where the baseline's `forbidden_pairs` forbids the combination.

| | |
|---|---|
| Emitted by | `scripts/check_policy.py`, run as the policy step of `scripts/ci_check.py` — so the `noru-ci` and `noru-review` actions. The `enforce` action does not run the policy step |
| Gates by default | yes — exit `7` from `ci_check.py`, `1` from `check_policy.py` |
| Baseline it is checked against | `.noru/privacy-baseline.yml` (or `--baseline=`). No baseline: the step reports `skipped`, never a pass |

## Why it matters

Health data used for advertising is the canonical case: each half is permitted, the combination is
not, and no single-axis allow list can express that. The baseline's `reason` is printed with the
finding.

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Failing example

The examples below run the `privacy-datamap` fixture map that `scripts/test_ci_mode.py` builds
from `tests/fixture-repo` against a baseline that permits it — `data_categories.allow:
[user.contact, user.device, user.authorization]`, `data_uses.allow: [essential]`,
`data_subjects.allow: [customer]` — with the one change named in each example.

With a fixture rule forbidding `user.authorization` for `essential.service`:

```yaml
forbidden_pairs:
  - categories: [user.authorization]
    uses: [essential.service]
    reason: Fixture rule, so the gate is observed failing.
```

```text
  FAIL [unpermitted_pair] system[0].privacy_declarations[0]: 'user.authorization.password' may not be processed for 'essential.service': Fixture rule, so the gate is observed failing. (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/unpermitted_pair.md)

FAILED (7): see docs/ci-mode.md for what this exit code means.
```

## Passing example

The fixture map against the permissive baseline above:

```text
$ python3 scripts/ci_check.py --piece=privacy-datamap --repo=. --output=text
privacy-datamap in /path/to/repo (gate mode)
  ok      scan
  ok      validate
  ok      expiry
  ok      policy: against /path/to/repo/.noru/privacy-baseline.yml

OK: every requested check passed.
```

## How to fix

Split the processing so the category is not used for that purpose, or — if the rule is wrong —
change it in the baseline with a rationale.

## Recording a disposition

- **Either the code changes or the baseline does.** Widening `.noru/privacy-baseline.yml` is the
  disposition, and it is a reviewed diff with an owner and a date: the baseline carries its own
  interpretation block and expires like any other claim. Noru holds the agreed taxonomy; the file is
  a pinned floor, so reconcile a change with Noru in a credentialed job
  ([ci-mode.md](../ci-mode.md#noru-is-the-truth-this-file-is-the-floor)).
- **A backlog on day one:** `--base-ref=<base> --gate-on-new` (action inputs `base-ref`,
  `gate-on-new`) stamps each finding `this_pr` or `pre_existing` and gates only on the first; the
  backlog is still reported. A base that cannot be resolved gates everything
  ([ci-mode.md](../ci-mode.md#which-of-these-did-this-pull-request-introduce)).
- **While adopting:** [warn-only mode](../ci-mode.md#warn-only-mode), or `--fail-on` without this
  kind.
