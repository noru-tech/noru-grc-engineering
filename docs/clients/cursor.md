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

Grant least privilege: `read:organization`, `read:frameworks`, `read:controls`, `read:evidence` to
look around; add `read:assets` and `read:vendors` for `ai-inventory:diff`; add write scopes only
when you intend to push.

## 2. Run a piece

Clone this repository somewhere, then drive the scripts from the repository you want to scan:

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

`evidence-push:push` is different: it performs the upload itself over REST, so it needs
`NORU_API_KEY` in your shell.

## Why the scripts and the agent are split

The deterministic parts — collection, validation, the diff, the confirmation gate — are scripts, so
they behave identically no matter which agent runs them. The judgement parts — what a system is for,
whether an artifact really satisfies an expectation, who owns the claim — are the agent's and yours.
The split is deliberate: it is what makes the manifest reviewable rather than a transcript.
