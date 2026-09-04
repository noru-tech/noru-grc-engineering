---
name: verify
description: Verify the offline GRC baseline and effective GitHub merge protections without changing them.
---

# /repo-enforcement:verify

Run `enforce.py validate --repo=<repo> --as-of=<today> --output=json`, then
`scripts/github-verify.mjs --repo=<repo> --output=json`. Confirm the workflow exists and is pinned,
the check source is bound, review controls are effective, bypass actors have not appeared, inherited
organisation rules are visible, and the ratchet baseline is valid. Read-only.
