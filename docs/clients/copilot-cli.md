# GitHub Copilot CLI

## Install

```bash
copilot plugin marketplace add noru-tech/noru-grc-engineering
copilot plugin install noru@noru-grc-engineering
copilot plugin install ai-inventory@noru-grc-engineering
copilot plugin install compliance-assistant@noru-grc-engineering
```

Any plugin in the marketplace installs the same way: `copilot plugin install <name>@noru-grc-engineering`.
`copilot plugin marketplace browse noru-grc-engineering` lists them all.

## Which file Copilot CLI reads

Copilot CLI looks for a marketplace at `marketplace.json`, `.plugin/marketplace.json`,
`.github/plugin/marketplace.json` and `.claude-plugin/marketplace.json`, in that order. This
repository ships `.github/plugin/marketplace.json`, a copy of the Claude marketplace with one
difference: Copilot CLI does not accept the `git-subdir` source that Claude Code needs for
`compliance-assistant`, so that entry uses Copilot's `github` source with the same `path`, `ref` and
`sha`. `scripts/check_repo.py` fails if the two files drift apart.

Each plugin then loads from its `.claude-plugin/plugin.json` and `.mcp.json`, which Copilot CLI
reads for plugins written in the Claude layout.

## What has been verified

- Copilot CLI 1.0.90 adds this marketplace from GitHub, and `copilot plugin install` installs both a
  plugin from this repository and `compliance-assistant` from its pinned commit.
- Not verified: running the pieces' slash commands under Copilot CLI. The commands run their scripts
  through `${CLAUDE_PLUGIN_ROOT}`. Copilot CLI documents that variable for hooks and for agent MCP
  servers, but not for the text of a command. Until that is confirmed, run the scripts directly as
  described in the [Cursor guide](./cursor.md#2-run-a-piece).

## Connect to Noru

Each plugin ships `.mcp.json` pointing at `https://api.noru.tech/v1/mcp`. **The plugin never
authenticates.** Authorize the server in Copilot CLI's MCP settings, with OAuth where it is
offered, otherwise with a bearer key from **Noru → Settings → Developer → API Keys**. Do not commit
a config that inlines the key, and do not paste it into a chat. The scopes are the same as in the
[Claude Code guide](./claude-code.md#scopes).
