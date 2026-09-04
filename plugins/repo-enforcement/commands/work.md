---
name: work
description: Inspect one exact baseline item and guide its owning piece through a reviewed remediation PR.
---

# /repo-enforcement:work

Require an exact `sha256:...` fingerprint. Run `enforce.py baseline inspect` with the repository and
an explicit date, then read `references/baseline-workflow.md`. Route to the returned piece review
command and prepare the smallest manifest, lock, generated-output, and baseline diff that resolves
the underlying issue.

Do not treat the agent proposal as approval. Before removing the baseline entry, verify that its
violation disappeared for the intended reason and obtain the accountable review required by the
piece. Do not merge or publish to Noru. After merge, offer the owning piece's read-only diff and keep
its push behind separate explicit confirmation.
