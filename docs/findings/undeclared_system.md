# `undeclared_system`

**Rule:** when the baseline declares its system list closed (`systems.closed: true`), every system processing personal data must be named in `systems.allow`.

| | |
|---|---|
| Emitted by | `scripts/check_policy.py`, run as the policy step of `scripts/ci_check.py` — so the `noru-ci` and `noru-review` actions. The `enforce` action does not run the policy step |
| Gates by default | yes — exit `7` from `ci_check.py`, `1` from `check_policy.py` |
| Baseline it is checked against | `.noru/privacy-baseline.yml` (or `--baseline=`). No baseline: the step reports `skipped`, never a pass |

## Why it matters

A service that started processing personal data without anyone agreeing to it is exactly the drift
this gate exists to catch. The rule applies only when the baseline opts in by closing the list.

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

With `systems: { closed: true, allow: [some_other_system] }`:

```text
  FAIL [undeclared_system] system[0]: 'deploy' processes personal data and the baseline's system list is closed — a service that started processing without anyone agreeing to it is exactly the drift this gate exists to catch (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/undeclared_system.md)

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

If the system should not process personal data, remove that processing. If it should, add its
`fides_key` to `systems.allow` in the baseline.

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
