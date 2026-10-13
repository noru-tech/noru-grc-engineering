# `unpermitted_subject`

**Rule:** every `data_subjects` value on a privacy declaration must be a subject the baseline allows.

| | |
|---|---|
| Emitted by | `scripts/check_policy.py`, run as the policy step of `scripts/ci_check.py` — so the `noru-ci` and `noru-review` actions. The `enforce` action does not run the policy step |
| Gates by default | yes — exit `7` from `ci_check.py`, `1` from `check_policy.py` |
| Baseline it is checked against | `.noru/privacy-baseline.yml` (or `--baseline=`). No baseline: the step reports `skipped`, never a pass |

## Why it matters

The same data about a different group of people is a different processing activity: customer
email and employee email are not interchangeable in a record of processing.

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

With `data_subjects.allow: [employee]` instead of `[customer]`:

```text
  FAIL [unpermitted_subject] system[0].privacy_declarations[0]: 'customer' is not in the baseline's data subject allow list, and that list is closed — this names people the baseline does not cover (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/unpermitted_subject.md)

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

Correct the declaration's `data_subjects`, or record in the baseline that the organization covers
this group.

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
