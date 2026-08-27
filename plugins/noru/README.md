# noru

> The hub. Connect, check, and share the repository context every last-mile piece reuses.

This plugin is not a piece — it has no `:scan` / `:diff` / `:push` and produces no manifest. It is
the thing you install alongside a piece so that "is my connection right, and is this machine ready"
is one command instead of a support thread.

## Commands

| Command | Writes anything? | What it does |
|---|---|---|
| `/noru:connect` | no | Confirms the Noru MCP connection, reports the organization and enabled frameworks, and explains least-privilege scopes for the piece you want to run |
| `/noru:doctor` | no | Checks node, python3, git, whether this is a git work tree, and whether `.noru/.cache/` is gitignored |
| `/noru:context` | no | Prints the provenance a push would carry, and every `.noru/*.yml` in the repository with its sha256 |

## Scopes

The hub itself needs almost nothing. `/noru:connect` calls two read tools to prove the connection
works; the rest is local.

| Capability | Scopes |
|---|---|
| `/noru:connect` | `read:organization`, `read:frameworks` |
| `/noru:doctor`, `/noru:context` | none — they make no Noru call |

Scopes for the pieces themselves are in each piece's own README.

## Why `context` exists

Two facts decide whether a push means anything, and both are easy to get wrong silently:

- **Is the working tree clean?** A push from a dirty tree records a `commit_sha` that does not
  describe what was actually scanned. The provenance is then worse than useless: it is confidently
  wrong.
- **What is the manifest's sha256?** A piece's plan is bound to the exact manifest bytes it was
  computed from. When `:push` refuses with a stale-plan error, `context` is how you see which
  manifest moved.

## For piece authors

`scripts/lib/plan.mjs` here is the **canonical** copy of the plan/diff helper every piece vendors —
the plan writer, the freshness check that makes `:diff`-before-`:push` real, the shared flag parsing,
and the credential redactor. Edit it here, then run:

```bash
python3 scripts/check_vendored_lib.py --fix
```

Never edit a vendored copy in a piece: CI fails on the drift, and the next `--fix` overwrites it.

An installed plugin cannot import across plugin boundaries, which is why the file is copied rather
than shared. The duplication is deliberate; the drift check is what makes it safe.
