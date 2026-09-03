# Claude Code and Claude Desktop

## Install the marketplace

```text
/plugin marketplace add noru-tech/noru-grc-engineering
/plugin install noru@noru-grc-engineering
/plugin install ai-inventory@noru-grc-engineering
/plugin install evidence-push@noru-grc-engineering
/plugin install governance-records@noru-grc-engineering
/plugin install review-signoff@noru-grc-engineering
/plugin install audit-pack@noru-grc-engineering
/plugin install iac-scan@noru-grc-engineering
/plugin install privacy-datamap@noru-grc-engineering
/plugin install change-control@noru-grc-engineering
```

Install `noru` (the hub) alongside whichever pieces you want. Each piece works on its own, but the
hub is where `connect`, `doctor` and `context` live.

## Connect to Noru

Each plugin ships a `.mcp.json` pointing at Noru's hosted endpoint:

```json
{
  "mcpServers": {
    "noru": {
      "type": "http",
      "url": "https://api.noru.tech/v1/mcp"
    }
  }
}
```

**The plugin does not authenticate.** Claude Code handles that. Two paths:

### OAuth (preferred)

Claude Code supports OAuth against remote MCP servers. On first use it opens the Noru authorization
flow in your browser and stores the result in its own credential store. Nothing lands in your
repository, and there is no key to rotate by hand.

Run `/mcp` to check the connection state.

### API key

For headless use, or a client without OAuth:

```bash
export NORU_API_KEY="<your_noru_api_key>"
```

Create the key in **Noru → Settings → Developer → API Keys** and grant it only the scopes below.
MCP connections are local to the host: a connection authorized in Claude Desktop does not authorize
Claude Code, or Codex, or Cursor.

**Never paste a key into a chat.** If you do, rotate it.

## Scopes

| Doing this | Scopes |
|---|---|
| Reading, `:scan`, `:diff` | `read:organization`, `read:frameworks`, `read:controls`, `read:evidence` |
| `ai-inventory:diff` also | `read:assets`, `read:vendors` |
| `ai-inventory:push` | adds `write:assets`, `write:vendors`, `write:evidence` |
| `evidence-push:push` | adds `write:evidence` |
| `governance-records:push` | adds `write:evidence` |
| `review-signoff:push` | adds `write:evidence` |
| `audit-pack:push` | adds `write:evidence` |
| `iac-scan:scan` | `read:risks`, `read:assets` |
| `iac-scan:diff` | adds `read:organization` to bind the plan |
| `iac-scan:push` | adds `write:risks` |
| `privacy-datamap:scan` | `read:datamaps` |
| `privacy-datamap:diff` | adds `read:organization` to bind the plan |
| `privacy-datamap:push` | adds `write:datamaps` |

Wildcards (`read:*`, `write:*`) work but are the wrong default. Start read-only: tool visibility is
filtered by scope at registration time, so a missing tool means a missing scope rather than an
outage.

## Verify

```text
/noru:connect
/noru:doctor
```

`connect` calls `findOrganization` and `getOrganizationFrameworks` and reports what it sees.
`doctor` checks node, python3, git, and that `.noru/.cache/` is gitignored.

## Then

```text
/ai-inventory:scan
/ai-inventory:diff
/ai-inventory:push
```

Review `.noru/ai-inventory.yml` between `:scan` and `:diff`, and read the plan before you confirm
the push. Those two review points are the product, not friction.

## Troubleshooting

**A command is not offered.** The plugin is not installed, or the marketplace was added but the
plugin was not. `/plugin` lists what is installed.

**A tool is missing from the session.** Your key lacks that scope. Tool visibility is scope-filtered
at registration.

**`push` says the manifest changed after the plan was written.** You edited the manifest after
running `:diff`. Re-run `:diff`, read the new plan, then push. This refusal is a security control:
the plan you approved no longer describes what would happen.

**`evidence-push:push` says `NORU_API_KEY` is not set.** File upload is REST-only — MCP tool
arguments are JSON and cannot carry a multipart body — so that one step needs a bearer key with
`write:evidence` exported in your shell.
