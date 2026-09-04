---
name: setup
description: Inspect a repository and prepare a reviewable local GRC enforcement installation plan. Makes no GitHub administration change.
---

# /repo-enforcement:setup

Run `scripts/inspect.mjs --repo=<repo> --output=json`, inspect effective GitHub state read-only, and
read `references/rollout-modes.md`. Ask only for real review teams, strict versus ratchet, exception
lifetime, break-glass team, and repository versus organisation scope. Confirm teams exist. Produce
the exact `configure.mjs plan` result and show its file diff. Do not apply files or mutate GitHub.

For ratchet mode, run `enforce.py baseline propose` after the workflow files are accepted. The
candidate is not an accepted baseline; obtain a named human owner and rationale for each entry.
