---
name: status
description: Report current repository enforcement, baseline debt, and GitHub ruleset drift without changing anything.
---

# /repo-enforcement:status

Run offline validation with an explicit date, then `scripts/github-status.mjs --repo=<repo>`. Report
enforcement, protected branch, required check, CODEOWNER review, bypass actors, policy drift,
baseline counts, expiring exceptions, and installed action version. Read-only.
