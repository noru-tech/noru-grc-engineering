# Codex

## Install

```bash
codex plugin marketplace add noru-tech/noru-grc-engineering
codex plugin add noru@noru-grc-engineering
codex plugin add ai-inventory@noru-grc-engineering
codex plugin add evidence-push@noru-grc-engineering
```

Codex reads the marketplace from `.agents/plugins/marketplace.json` and each plugin from
`plugins/<name>/.codex-plugin/plugin.json`.

To try it against a local checkout without touching your real configuration:

```bash
tmpdir="$(mktemp -d)"
CODEX_HOME="$tmpdir" codex plugin marketplace add <path-to-this-repo>
CODEX_HOME="$tmpdir" codex plugin list --marketplace noru-grc-engineering
CODEX_HOME="$tmpdir" codex plugin add ai-inventory@noru-grc-engineering
```

## Connect to Noru

Each plugin ships `.mcp.json` pointing at `https://api.noru.tech/v1/mcp`. **The plugin never
authenticates.** Configure it in your Codex MCP settings, using OAuth if your Codex build supports
it for remote MCP servers, otherwise a bearer key:

```bash
export NORU_API_KEY="<your_noru_api_key>"
```

Create it in **Noru → Settings → Developer → API Keys**. Do not commit a generated local config that
inlines the value, and do not paste the key into a chat.

## Scopes

| Doing this | Scopes |
|---|---|
| Reading, `:scan`, `:diff` | `read:organization`, `read:frameworks`, `read:controls`, `read:evidence` |
| `ai-inventory:diff` also | `read:assets`, `read:vendors` |
| `ai-inventory:push` | adds `write:assets`, `write:vendors`, `write:evidence` |
| `evidence-push:push` | adds `write:evidence` |

## Headless use

Every entrypoint takes `--output=json --quiet` and has documented exit codes, so a piece runs in a
pipeline without a TTY:

```bash
node    plugins/ai-inventory/scripts/collect.mjs --repo=. --check --output=json --quiet
python3 plugins/ai-inventory/scripts/validate_manifest.py .noru/ai-inventory.yml --output=json --quiet
```

Exit codes: `0` success · `1` the thing being checked is wrong (drift, invalid manifest, missing
input) · `2` you called it wrong, including a push without `--confirm`.

The `:diff` and `:push` steps still need the MCP state snapshot, which an agent session produces.
A fully headless run — scan, validate, diff, fail-or-push, with a scoped machine key — is not there
yet; today `collect.mjs --check` is the part that already works and is enough to fail a build on
drift.
