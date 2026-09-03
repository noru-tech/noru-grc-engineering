# Cursor

Cursor does not read this repository's plugin marketplaces, so use it in two parts: connect the MCP
server through Cursor's own configuration, and run the piece scripts directly.

## 1. Connect Noru MCP

Add the server in Cursor's MCP settings, or in `~/.cursor/mcp.json`:

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

Authenticate with OAuth where Cursor supports it for remote MCP servers, otherwise with a bearer
key from **Noru → Settings → Developer → API Keys** configured the way Cursor expects.

**Do not commit a config that inlines the key**, and do not paste it into a chat. MCP connections
are local to the host: authorizing Cursor does not authorize Claude Code or Codex.

Grant least privilege. `read:organization`, `read:frameworks`, `read:controls`, `read:evidence` is
enough to look around and to run `:scan` and `:diff` for most pieces; add write scopes only when you
intend to push. Two pieces sit outside that set entirely:

| Doing this | Scopes |
|---|---|
| `ai-inventory:diff` also | `read:assets`, `read:vendors` |
| `ai-inventory:push` | adds `write:assets`, `write:vendors`, `write:evidence` |
| `evidence-push:push`, `governance-records:push`, `review-signoff:push`, `audit-pack:push` | adds `write:evidence` |
| `iac-scan:scan` | `read:risks`, `read:assets` |
| `iac-scan:diff` | adds `read:organization` to bind the plan |
| `iac-scan:push` | adds `write:risks` |
| `privacy-datamap:scan` | `read:datamaps` |
| `privacy-datamap:diff` | adds `read:organization` to bind the plan |
| `privacy-datamap:push` | adds `write:datamaps` |

## 2. Run a piece

Clone this repository somewhere, then drive the scripts from the repository you want to scan.
`ai-inventory` below is only the example — every piece ships the same three scripts, so substitute
`evidence-push`, `governance-records`, `review-signoff`, `audit-pack`, `iac-scan` or
`privacy-datamap` and the sequence is identical:

```bash
GRC=/path/to/noru-grc-engineering

# scan — deterministic, offline
node "$GRC/plugins/ai-inventory/scripts/collect.mjs" --repo=.

# edit .noru/ai-inventory.yml: purpose, autonomy, oversight, owners

python3 "$GRC/plugins/ai-inventory/scripts/validate_manifest.py" .noru/ai-inventory.yml \
  --emit-parsed=.noru/.cache/ai-inventory.parsed.json
```

Then ask Cursor's agent to gather the state snapshot over MCP into
`.noru/.cache/noru-state.json` — the shape is documented in
[`plugins/ai-inventory/commands/diff.md`](../../plugins/ai-inventory/commands/diff.md) — and:

```bash
node "$GRC/plugins/ai-inventory/scripts/diff.mjs" --repo=.
node "$GRC/plugins/ai-inventory/scripts/push.mjs" --repo=. --confirm
```

`push.mjs` writes the confirmed call list to `.noru/.cache/ai-inventory.calls.json`. Ask the agent
to execute exactly those calls over the `noru` MCP connection, in order, and nothing else.

Three pieces depart from that shape, and it is worth knowing which before you drive them:

- **`evidence-push:push`** performs the upload itself over REST rather than emitting MCP calls, so
  it needs `NORU_API_KEY` in your shell. File upload is a deliberate omission from Noru's MCP
  surface — tool arguments are JSON and cannot carry a multipart body.
- **`iac-scan:push`** emits `createSecurityFinding` calls, which are documented server-side upserts
  on `source + externalId`. Filing a finding and closing one are the same call, and a repeat lands
  the same record.
- **`privacy-datamap:push`** emits a single `ingestDatamap` call carrying the whole map for a
  source, however many fields the repository has.

## Why the scripts and the agent are split

The deterministic parts — collection, validation, the diff, the confirmation gate — are scripts, so
they behave identically no matter which agent runs them. The judgement parts — what a system is for,
whether an artifact really satisfies an expectation, who owns the claim — are the agent's and yours.
The split is deliberate: it is what makes the manifest reviewable rather than a transcript.
