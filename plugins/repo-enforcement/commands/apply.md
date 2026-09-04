---
name: apply
description: Apply the exact fresh GitHub ruleset plan after explicit confirmation, then verify effective enforcement.
---

# /repo-enforcement:apply

Read the cached plan and summarize create/update/skip counts and target repository. Obtain explicit
confirmation, then run `scripts/github-apply.mjs --repo=<repo> --confirm --output=json`. The script
refetches live state and refuses expired, rebound, or changed plans. Never add `--confirm` based on
an earlier file review or merge approval.
