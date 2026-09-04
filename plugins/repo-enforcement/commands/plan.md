---
name: plan
description: Bind an exact, expiring GitHub ruleset plan to current repository, policy, check source, and live rules.
---

# /repo-enforcement:plan

Confirm `Noru GRC / validate` has run, resolve its integration ID and all configured teams, inspect
inherited and repository rules, then run `scripts/github-plan.mjs --repo=<repo> --output=json`.
Explain the single create/update/skip operation. This command is read-only and writes only the
ignored plan cache.
