# Baseline burn-down workflow

The baseline records temporary acceptance of exact legacy violations. It is not evidence that the
underlying work is complete and must not become a manually maintained task tracker. Work state is
derived by rerunning the released checks against the repository.

## Worklist states

- `accepted`: the exact violation still exists and its acceptance is live. Resolve it through the
  owning piece before expiry.
- `expired`: the violation still exists but its acceptance has ended. It blocks immediately.
- `stale`: the accepted fingerprint no longer exists. Verify that the cause was genuine remediation
  rather than weakened detection, then remove the unused entry in the same reviewed PR.
- `unbaselined`: a new or mutated violation. It blocks and cannot inherit an old acceptance.

`due_soon` means seven days or fewer remain. `blocking` sorts first, then stale cleanup, due-soon
debt, and the remaining accepted debt. Grouping by owner and piece is computed from the current
baseline; no second status field is written.

## Resolving one item

1. Inspect the exact fingerprint and follow its `review_command` into the owning piece.
2. Re-read the cited repository evidence. Agent output may propose a classification or fix but does
   not approve it.
3. Update the owning manifest and any lock or generated output required by that piece.
4. Rerun baseline check. The original violation must disappear without a new or mutated violation.
5. Review why the entry is now stale, then remove that exact baseline entry in the same PR.
6. Merge through the enforced workflow and required reviewers.
7. Run the owning piece's read-only diff against Noru. A separate explicit confirmation is still
   required for push.

Do not remove a baseline entry merely because a collector stopped reporting it. A rule, workflow,
collector, registry, or policy change needs independent review because it may have weakened the
detector instead of resolving the issue.
