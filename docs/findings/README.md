# Findings

Every finding the hub's checks report has a page here: the rule in one sentence, why it matters,
the controls it maps to, a failing and a passing example taken from this repository's own
fixtures, and how to fix it or record a disposition. The file name is the kind the tool prints, so
`[drift]` in a log is `drift.md` here, and every finding line ends with a link to its page:

```text
  FAIL [drift] .noru/ai-inventory.yml: the committed manifest no longer matches the repository (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/drift.md)
```

The links are on human-readable output only: the text of `scripts/ci_check.py`,
`scripts/check_expiry.py` and `scripts/check_policy.py`, the `noru-ci` job summary, and the
`enforce` action's annotations. JSON reports carry no URL, so a dashboard or a ratchet baseline
built on them does not change.

**Controls.** No page maps a finding to a framework control. This repository ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules)); which controls a record supports
is decided in your Noru organization.

## CI mode

Reported by [`scripts/ci_check.py`](../ci-mode.md), which is what the
[`noru-ci`](../../.github/actions/noru-ci/README.md) and
[`noru-review`](../../.github/actions/noru-review/README.md) actions run. Exit codes are the
orchestrator's; see [ci-mode.md, "Exit codes"](../ci-mode.md#exit-codes).

| kind | rule | gates by default | exit |
|---|---|---|---|
| [`drift`](./drift.md) | the committed manifest no longer matches what the collector derives from the repository | yes | `3` |
| [`invalid`](./invalid.md) | the manifest fails its piece validator | yes | `5` |
| [`dangling_ref`](./dangling_ref.md) | a `file:line` citation no longer resolves | no | `3` if gated |
| [`coverage`](./coverage.md) | the collector parsed nothing in a repository that visibly has a schema (broken gate), or parsed part of it | nothing parsed: always; partial: no | `6` |
| [`expired`](./expired.md) | a claim's expiry is in the past | yes | `4` |
| [`cadence`](./cadence.md) | a claim is outside the review cadence the pipeline declared with `--max-age-days` | yes, when declared | `4` |
| [`unparsable`](./unparsable.md) | a date that cannot be compared | yes | `4` |
| [`expiring`](./expiring.md) | a claim expires inside the warning window | no | `4` if gated |
| [`unbounded`](./unbounded.md) | a claim has no expiry the check can read | no | `4` if gated |
| [`unpermitted_category`](./unpermitted_category.md) | a data category the privacy baseline does not allow, or denies | yes | `7` |
| [`unpermitted_use`](./unpermitted_use.md) | a purpose the baseline does not allow | yes | `7` |
| [`unpermitted_subject`](./unpermitted_subject.md) | a data subject the baseline does not cover | yes | `7` |
| [`unpermitted_pair`](./unpermitted_pair.md) | a category and a use the baseline forbids together | yes | `7` |
| [`confined_category`](./confined_category.md) | a category found outside the dataset or system it is confined to | yes | `7` |
| [`undeclared_system`](./undeclared_system.md) | a system processing personal data that a closed baseline does not name | yes | `7` |
| [`special_category`](./special_category.md) | GDPR Article 9 or Article 10 data in the map | no | `7` if gated |
| [`tooling`](./tooling.md) | a check could not run at all | always, even in warn mode | `6` |

More than one distinct gating condition exits `1`; a usage error exits `2`. The standalone tools
`check_expiry.py` and `check_policy.py` exit `1` on any gating finding.

## Repository enforcement

Reported by the [`enforce`](../../actions/enforce/README.md) action and
`plugins/repo-enforcement/scripts/enforce.py`, which run every required piece's scan, validate and
expiry steps (not the policy step) and apply the ratchet baseline. Each CI-mode kind above appears
as `<piece>/<kind>`; these are added. See [repository-enforcement.md](../repository-enforcement.md).

| rule | meaning | can be baselined |
|---|---|---|
| [`needs_review`](./needs_review.md) | an entry still flagged `needs_review: true` | yes |
| [`missing_interpretation`](./missing_interpretation.md) | a claim without an interpretation block | yes |
| [`tooling`](./tooling.md) | a required piece could not run | no |
| [`invalid_baseline`](./invalid_baseline.md) | the ratchet baseline is malformed, unowned, or bound to another policy | no |
| [`expired_exception`](./expired_exception.md) | an accepted baseline entry has expired | no |
| [`stale_baseline_entry`](./stale_baseline_entry.md) | a baseline entry matches no current violation | no |

`invalid` is never baselineable either; `drift`, `expired`, `cadence`, `coverage` and `unparsable`
are, as exact fingerprints with a named person and an expiry.

## GitHub ruleset verification

`/repo-enforcement:verify` checks the merge rules on GitHub itself. Its seventeen kinds share one
page with a section each: [github-ruleset.md](./github-ruleset.md).

## What is not here

- Piece validator errors have no stable codes: each is a manifest path and a message saying what to
  do next, and CI mode reports every one as [`invalid`](./invalid.md).
- The records pieces land in Noru — an `iac-scan` security finding, a `change-control` separation
  that did not hold — are compliance content, documented in each piece's README, not check
  findings.
