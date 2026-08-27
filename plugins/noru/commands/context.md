---
name: context
description: Show the repository provenance a push would carry and the last-mile manifests this repository already has.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /noru:context

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/context.mjs" --repo=<repo> --output=json
```

Reports:

- **Provenance** — `slug`, `commit_sha`, `branch`, and whether the working tree is clean. Every push
  from every piece carries these three fields, so this is literally what would be written into Noru
  as the origin of the claim. A dirty tree means the recorded commit does not describe what was
  scanned; say so before anyone pushes.
- **Manifests** — every `.noru/*.yml` in the repository with its sha256. That digest is what a
  piece's plan is bound to: edit the manifest and the plan you reviewed is no longer valid, and
  `:push` will refuse until you re-run `:diff`.

Use this when a piece's `:push` refuses with a stale-plan error, to see which manifest moved, and
when you want to know what a repository already has under `.noru/` before starting.

This command reads only. It makes no MCP call and needs no scopes.
