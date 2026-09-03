# Codex

## Install

```bash
codex plugin marketplace add noru-tech/noru-grc-engineering
codex plugin add noru@noru-grc-engineering
codex plugin add ai-inventory@noru-grc-engineering
codex plugin add evidence-push@noru-grc-engineering
codex plugin add governance-records@noru-grc-engineering
codex plugin add review-signoff@noru-grc-engineering
codex plugin add audit-pack@noru-grc-engineering
codex plugin add iac-scan@noru-grc-engineering
codex plugin add privacy-datamap@noru-grc-engineering
codex plugin add change-control@noru-grc-engineering
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
| `governance-records:push` | adds `write:evidence` |
| `review-signoff:push` | adds `write:evidence` |
| `audit-pack:push` | adds `write:evidence` |
| `iac-scan:scan` | `read:risks`, `read:assets` |
| `iac-scan:diff` | adds `read:organization` to bind the plan |
| `iac-scan:push` | adds `write:risks` |
| `privacy-datamap:scan` | `read:datamaps` |
| `privacy-datamap:diff` | adds `read:organization` to bind the plan |
| `privacy-datamap:push` | adds `write:datamaps` |

## Headless use

Every entrypoint takes `--output=json --quiet` and has documented exit codes, so a piece runs in a
pipeline without a TTY:

```bash
node    plugins/ai-inventory/scripts/collect.mjs --repo=. --check --output=json --quiet
python3 plugins/ai-inventory/scripts/validate_manifest.py .noru/ai-inventory.yml --output=json --quiet
```

Exit codes: `0` success · `1` the thing being checked is wrong (drift, invalid manifest, missing
input) · `2` you called it wrong, including a push without `--confirm`.

`scripts/ci_check.py` orchestrates the whole sequence, and
[`.github/actions/noru-ci`](../../.github/actions/noru-ci/) wraps it for GitHub:

```bash
python3 scripts/ci_check.py --piece=ai-inventory --mode=warn
```

Its exit codes are more specific than the per-tool ones above, so a pipeline can react to each gate
without parsing text: `3` drift · `4` an expired interpretation or one outside the declared cadence
· `5` the manifest failed validation · `6` a check could not run at all. That last one is not
suppressed by `--mode=warn`, on purpose — a broken gate should be loud while its findings are still
advisory.

How far headless actually goes, step by step:

| Step | Headless today | What it needs |
|---|---|---|
| `scan`, `validate`, `expiry` | **yes, fully** | nothing — no network, no credential, so it runs on a fork pull request |
| `diff` | yes, *given* a state snapshot | `.noru/.cache/noru-state.json`. `ci_check.py` does not fetch it; something has to call the piece's read tools over MCP first. Without it the step reports `skipped` and the build stays green |
| `push` | depends on the piece | `NORU_API_KEY` present in the environment. `evidence-push` completes its own write over REST. The MCP pieces emit an ordered call list to `.noru/.cache/<piece>.calls.json`, which still needs an MCP client to execute |

So the honest summary is narrower than "not there yet": the offline half is done and already gates
builds. What is missing is state acquisition and MCP call execution without an agent session — two
specific gaps, not the whole pipeline.

`ci_check.py` never reads the value of `NORU_API_KEY`. Its *presence* decides whether the push step
can run; the value is passed through the environment to the piece's own push entrypoint, and every
step that does not need it runs with the variable removed from its child environment.

Exit codes, warn-only adoption, the GitHub Action's inputs and the GitLab and plain-shell recipes:
[docs/ci-mode.md](../ci-mode.md).
