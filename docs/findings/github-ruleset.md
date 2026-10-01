# GitHub ruleset verification

**Rule:** the merge rules repository enforcement depends on must still be in effect — the managed
ruleset active and unbypassed, reviews and the required check as the policy says, the workflow
present and pinned, CODEOWNERS protecting itself.

| | |
|---|---|
| Emitted by | `/repo-enforcement:verify` (`plugins/repo-enforcement/scripts/github-verify.mjs`), and as a post-condition of `/repo-enforcement:apply`. Not by any GitHub Action |
| Gates by default | verify exits `1` on any finding; it is a read-only check, not a merge gate |
| Accepted in a ratchet baseline | no — GitHub and workflow drift cannot be baselined |

These findings come from the GitHub administration half of
[repository enforcement](../repository-enforcement.md#github-administration). Each kind has a
section below; link to it as `github-ruleset.md#<kind>`. The text output is one line (`OK: verify`
or `FAIL: verify`); the kinds are in the JSON output's `findings`.

## Why it matters

The offline check only protects `main` if GitHub actually requires it. A ruleset someone disabled,
a bypass actor added "temporarily", or a required check rebound to another app quietly turns a gate
into a suggestion. A local monitor is defence in depth, not the authority: somebody able to weaken
the repository can remove it.

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Failing example

The ruleset fixture from `scripts/test_repo_enforcement.py`, applied and then weakened (enforcement
disabled, a team added as a bypass actor):

```text
$ node plugins/repo-enforcement/scripts/github-verify.mjs --repo=. --state=github-weakened.json --output=text
FAIL: verify
$ node plugins/repo-enforcement/scripts/github-verify.mjs --repo=. --state=github-weakened.json --output=json
{
  "ok": false,
  "enforcement": "disabled",
  ...
  "findings": [
    { "kind": "ruleset_disabled", "message": "managed ruleset enforcement is disabled" },
    { "kind": "bypass_added", "message": "managed ruleset has bypass actors" }
  ]
}
```

## Passing example

The same fixture straight after a confirmed apply:

```text
$ node plugins/repo-enforcement/scripts/github-verify.mjs --repo=. --state=github-applied.json --output=text
OK: verify
```

## How to fix

`/repo-enforcement:plan` writes a one-hour plan bound to the repository, policy digest and current
ruleset; `/repo-enforcement:apply` performs only that create or update after explicit confirmation,
then verifies. Organization rulesets are read and verified but not created by this utility.

## Recording a disposition

None. A weakening is either reverted or made deliberately by changing `.noru/enforcement.yml` in a
reviewed pull request, which changes the policy digest.

## The kinds

### `ruleset_missing`

**Rule:** the managed ruleset (`github.ruleset_name`, at `github.scope`) is absent. Message: `managed repository ruleset is absent`. **Fix:** Run `/repo-enforcement:plan`, then `/repo-enforcement:apply` with confirmation.

### `ruleset_disabled`

**Rule:** the managed ruleset's enforcement is not `active`. Message: `managed ruleset enforcement is disabled`. **Fix:** Set the ruleset's enforcement back to active (re-plan and apply).

### `bypass_added`

**Rule:** the managed ruleset lists bypass actors. Message: `managed ruleset has bypass actors`. **Fix:** Remove the bypass actors; break-glass access belongs to the team named in `ownership.break_glass`, used deliberately.

### `deletion_allowed`

**Rule:** branch deletion protection is missing from the managed ruleset. Message: `branch deletion protection is absent`. **Fix:** Re-plan and apply the ruleset.

### `force_push_allowed`

**Rule:** non-fast-forward (force-push) protection is missing. Message: `non-fast-forward protection is absent`. **Fix:** Re-plan and apply the ruleset.

### `approval_reduced`

**Rule:** the required approving review count is below `reviews.minimum_approvals`. Message: `approval count is below policy`. **Fix:** Restore the approval count, or change the policy in a reviewed pull request.

### `stale_review_dismissal_disabled`

**Rule:** stale approvals are not dismissed on push. Message: `dismiss_stale_reviews_on_push is not effective`. **Fix:** Re-plan and apply the ruleset.

### `codeowner_review_disabled`

**Rule:** code-owner review is not required. Message: `require_code_owner_review is not effective`. **Fix:** Re-plan and apply the ruleset.

### `last_push_approval_disabled`

**Rule:** approval of the most recent push is not required. Message: `require_last_push_approval is not effective`. **Fix:** Re-plan and apply the ruleset.

### `thread_resolution_disabled`

**Rule:** review-thread resolution is not required. Message: `required_review_thread_resolution is not effective`. **Fix:** Re-plan and apply the ruleset.

### `required_check_removed_or_rebound`

**Rule:** the required GRC check is missing, or bound to a different source integration than the one that runs it. Message: `required GRC check or its source integration does not match`. **Fix:** Re-plan after the workflow has run successfully, so the plan binds the integration that actually produces the check.

### `workflow_missing`

**Rule:** the Noru GRC workflow is absent. Message: `Noru GRC workflow is absent`. **Fix:** Re-run `/repo-enforcement:setup` and merge the workflow it plans.

### `workflow_unpinned`

**Rule:** the workflow does not pin the action to a full commit SHA. Message: `Noru GRC action is not pinned to a full commit SHA`. **Fix:** Pin `uses:` to the full SHA recorded in `github.action_sha`.

### `workflow_drift`

**Rule:** the workflow's action SHA differs from `github.action_sha` in the committed policy. Message: `Noru GRC action SHA differs from committed policy`. **Fix:** Change both in one reviewed pull request, or restore the workflow to the policy's SHA.

### `codeowners_missing`

**Rule:** there is no CODEOWNERS file. Message: `CODEOWNERS is absent`. **Fix:** Re-run `/repo-enforcement:setup`; it preserves unrelated CODEOWNERS entries.

### `codeowners_unprotected`

**Rule:** CODEOWNERS does not protect itself. Message: `CODEOWNERS does not protect itself`. **Fix:** Add an owner line for `/.github/CODEOWNERS`, as setup plans it.

### `check_not_successful`

**Rule:** the required GRC check has no successful run on the protected branch. Message: `the required GRC check has no successful run on the protected branch`. **Fix:** Get the workflow green on the default branch before planning or verifying.
