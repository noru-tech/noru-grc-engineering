# Supported GitHub workflow templates

Copy [`noru-grc-review.yml`](./noru-grc-review.yml) to
`.github/workflows/noru-grc-review.yml` in the adopting repository. It detects the pieces implicated
by a pull-request diff and runs local scan, validation, expiry, and privacy-policy checks. It cannot
run `diff` or `push`, requests only `contents: read`, receives no Noru credential, and handles forks
without pretending that missing external inputs passed.

The template starts in report-only mode. Create the repository variable `NORU_GRC_MODE=gate` after
the team has reviewed the initial backlog and committed the manifests and privacy baseline it wants
to enforce. The action and its dependencies are pinned to the suite's release tag.

## Protected publication

There is intentionally no copyable workflow that claims to publish every piece from GitHub Actions.
Eight pieces prepare reviewed changes, but seven publish over MCP; the current headless action can
verify their plans but cannot execute MCP calls. `evidence-push` uses REST, but its plan still needs
a live evidence-queue snapshot before an approved push. A generic workflow cannot obtain either
input without inventing an unsupported credential bridge.

Today, approval and publication for MCP pieces happen in the authenticated MCP host with
`/<piece>:diff` followed by explicit confirmation and `/<piece>:push`. If Noru exposes a supported
headless MCP execution contract, add a separate post-merge workflow using a protected GitHub
environment and least-privilege credentials. Until then, a workflow containing a nominal approval
gate but no executable writer would be security theatre, and a direct REST call would bypass the
organization- and repository-bound plan.
