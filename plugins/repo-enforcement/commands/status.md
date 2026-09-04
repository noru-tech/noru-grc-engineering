---
name: status
description: Report current repository enforcement, baseline debt, and GitHub ruleset drift without changing anything.
---

# /repo-enforcement:status

Run `scripts/enforce.py baseline worklist` with an explicit date, then
`scripts/github-status.mjs --repo=<repo>`. Report enforcement, protected branch, required check,
CODEOWNER review, bypass actors, policy drift, backlog grouped by owner and piece, due-soon and
expired acceptances, stale cleanup, unbaselined blockers, and installed action version. Read-only.
