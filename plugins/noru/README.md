# noru

> The hub. Review branch impact, summarize live work requiring attention, connect, check, and share
> repository context.

This plugin is not a piece — it has no `:scan` / `:diff` / `:push` and produces no manifest. It is
the thing you install alongside a piece so that "is my connection right, and is this machine ready"
is one command instead of a support thread. It also handles the loose first question — "what GRC
work is relevant here?" — before a user knows which piece to install or run.

## Start with a question

You do not need to know a command or piece name. From a repository, ask:

```text
Review this repository for local GRC work.
What has changed here that might affect our compliance records?
Do we need an AI inventory, infrastructure scan or privacy data map?
Run the relevant local checks, but do not write anything to Noru.
```

The hub inspects the tracked repository, identifies relevant pieces, cites the files behind each
selection, and runs the installed local scans independently. A branch review may create or update a
local `.noru/<piece>.yml`, but it never writes to Noru or supplies human decisions on the user's
behalf. Every Noru write still requires a separately reviewed `:diff` and confirmation. Ask only
"what is relevant?" when you want selection without running scans.

## Commands

| Command | Writes anything? | What it does |
|---|---|---|
| `/noru:connect` | no | Confirms the Noru MCP connection, reports the organization and enabled frameworks, and explains least-privilege scopes for the piece you want to run |
| `/noru:doctor` | no | Checks node, python3, git, cache hygiene, and warns about possible competing privacy data-map publishers |
| `/noru:context` | no | Prints the provenance a push would carry, and every `.noru/*.yml` in the repository with its sha256 |
| `/noru:review` | local manifests and cache only | Compares the branch with a base ref, discovers independently installed pieces, runs and validates selected local checks, and optionally prepares read-only diffs; it never pushes |
| `/noru:status` | no | Uses the live read-tool surface to report blockers, expiring evidence, findings, risks, privacy work and due sign-offs; missing scopes degrade only their section |

## Scopes

The hub itself needs almost nothing. `/noru:connect` calls two read tools to prove the connection
works; the rest is local.

| Capability | Scopes |
|---|---|
| `/noru:connect` | `read:organization`, `read:frameworks` |
| `/noru:doctor`, `/noru:context`, review selection | none — they make no Noru call |
| checks selected by `/noru:review` | `read:organization` plus the selected pieces' read scopes; never write scopes |
| `/noru:status` | `read:organization`, then only the section scopes visible through `getMcpCapabilities` |

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

The full installation, pull-request and protected-publication path is in
[developer onboarding](../../docs/developer-onboarding.md).
