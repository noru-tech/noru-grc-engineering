---
name: connect
description: Confirm the Noru MCP connection works, show which organization and frameworks it sees, and explain least-privilege scopes for the pieces you want to run.
---

# /noru:connect

Establish that this client can talk to Noru, and that it has exactly the scopes the work needs and
no more.

## 1. Confirm the connection

The plugin ships `.mcp.json` pointing at Noru's hosted MCP endpoint, `https://api.noru.tech/v1/mcp`.
**The plugin never handles authentication itself.** Depending on the client, either:

- **OAuth** — for clients that support OAuth against remote MCP servers. Preferred: nothing to store.
- **`NORU_API_KEY`** — a bearer key for manual or headless setup, created in
  **Noru → Settings → Developer → API Keys** and exported in the user's own shell.

MCP connections are local to the host. A connection authorized in one client does not authorize
another.

Never ask the user to paste a key into this conversation. If they do anyway, tell them to rotate it.

## 2. Prove it

Call `findOrganization`, then `getOrganizationFrameworks`. Report the organization and the frameworks
that are actually enabled. If a tool is missing from the session, the key is missing that scope —
which tools a session can see follows from the scopes on the key, so an absent tool is a permissions
answer, not an outage.

## 3. Grant the least privilege that does the job

| Doing this | Needs |
|---|---|
| Looking around, `:scan`, `:diff` for any piece | `read:organization`, `read:frameworks`, `read:controls`, `read:evidence` |
| `ai-inventory:diff` as well | `read:assets`, `read:vendors` |
| `ai-inventory:push` | adds `write:assets`, `write:vendors`, `write:evidence` |
| `evidence-push:push` | adds `write:evidence` (and a REST key, because upload is REST-only) |

Wildcards (`read:*`, `write:*`) work but are the wrong default. Start read-only: every piece is
useful before it is ever allowed to write, and `:diff` is where most of the value is.

## 4. Check the machine

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.mjs" --repo=<repo>
```

Then report what the user can run next: `/ai-inventory:scan` for an AI-heavy repository,
`/evidence-push:scan` to work the evidence queue for a control or framework they name.
