# dev-agents

> The coding agents that wrote your changes, and the MCP servers and plugins your repository tells
> them to load — recorded as suppliers, decided by a person, and landed in Noru as vendors plus one
> evidence record for the window. Nothing per person, ever by default.

A development process that uses coding agents has suppliers it did not have before: the vendor
whose model wrote the change, and whoever publishes each MCP server and plugin those agents are
configured to use. None of them shows up in a procurement system, because nobody bought them the
usual way, and none of them is visible to a server-side integration, because the facts are in the
repository's history and its committed configuration.

## Not ai-inventory

[`ai-inventory`](../ai-inventory/) inventories the AI **the product calls**: the model behind a
feature, what data reaches it, who oversees its output. `dev-agents` inventories the AI **the
development process depends on**: the agent that authored a pull request, the MCP server it read
tickets from, the plugin that drafted the release notes. The same organization can be both — and
then it is both, in two different records, because the obligations that follow differ. Use either
or both; neither reads the other's manifest.

## Two inputs, both local

### 1. An agent-change-control manifest

[agent-change-control](https://github.com/noru-tech/agent-change-control) (`acc`) establishes who
the effective author of each change was — a person, or an agent — and keeps an actor registry in
which every agent carries its vendor. The collector reads the manifest `acc` writes, AI Change
Provenance 0.3 or 0.4, from `.noru/.cache/acc-manifest.json`:

```bash
acc scan --format json -o .noru/.cache/acc-manifest.json
```

From `events.actors` (kind `agent`, with `vendor`) and `events.changes` (an effective author
`agent:*`), it derives each agent, its vendor, how many changes it authored and how many it
reviewed in the window, and the totals. It records the window and the manifest's SHA-256, so drift
detection covers the evidence underneath. Any other version is refused rather than misread.

Without an acc manifest the piece still inventories the configuration below, and the validator
says that the agents are missing. Once a committed manifest was built from an acc manifest, a
checkout without one is a missing input (exit `1`), not drift.

### 2. The repository's committed agent configuration

| File | Tool | What is read | Format, as documented |
|---|---|---|---|
| `.mcp.json` | Claude Code | `mcpServers` | <https://code.claude.com/docs/en/mcp> |
| `.claude/settings.json` | Claude Code | `enabledPlugins`, `extraKnownMarketplaces` | <https://code.claude.com/docs/en/settings-reference> |
| `.cursor/mcp.json` | Cursor | `mcpServers` | <https://cursor.com/docs/context/mcp> |
| `.vscode/mcp.json` | VS Code | `servers` | <https://code.visualstudio.com/docs/agents/reference/mcp-configuration> |
| `.gemini/settings.json` | Gemini CLI | `mcpServers` | <https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md> |

Only files git tracks are read: a developer's own untracked configuration is theirs, not the
repository's, and CI would never see it. Comments and trailing commas are tolerated rather than
treated as a broken file. A file that does not parse, or an entry the documented format does not
allow, is listed under `config_problems` and is **not** in the inventory; the validator says so.

For each MCP server the manifest records the file, the server's name, the transport, and:

- for a **stdio** server, the **name of the program** it runs (`npx`, `uvx`, `docker`) and nothing
  else — no directory, no argument;
- for a **remote** server, the **host** and nothing else — no scheme, port, path, query string or
  user-info.

`env`, `envFile`, `headers`, `args`, `cwd`, `inputs` and full URLs are **never** recorded. Any of
them can hold a credential, and this manifest is committed and its content is pushed. A command or
a URL built from a variable is recorded as `(variable)`, not expanded. The validator refuses a
manifest that carries any of those keys, or a host that is not a bare host name.

A server whose transport the file does not state (a Cursor or VS Code `url` with no `type`) is
recorded as `remote` rather than guessed. A Claude Code entry with a `url` and no `type` is
documented as a configuration error, so it is reported, not inventoried.

For each plugin, `.claude/settings.json` gives the plugin and marketplace names; only plugins set to
`true` are recorded. Where `extraKnownMarketplaces` declares the marketplace, its source kind and,
for a `github` source, its `owner/repo` (for any other URL, its host) come with it.

## Who ran which agent is never aggregated by default

How many changes each person produced with an agent is employee activity data. The collector does
not compute it. With `--operator-counts`, and only then, it writes per-operator counts to
`.noru/.cache/dev-agents.operators.json` — under the cache directory, which is never committed —
and it never puts them into the derived facts, the manifest, the plan or anything pushed. The
summary it prints names the file and the number of rows, never the contents, because that summary
is what a CI log keeps. The validator refuses a manifest that carries per-person counts under any
of their usual names.

Think about whether you need the file before asking for it, and who may read it once it exists.

## A person decides who each supplier is

Every supplier in the skeleton is a proposal, flagged `needs_review: true`:

- **an agent's vendor** is proposed from acc's built-in vendor registry (AI Change Provenance §5:
  `claude-code` → `anthropic`, `copilot` → `github`, `codex` → `openai`, `gemini-cli` → `google`,
  `cursor` → `cursor`), named from this plugin's vocabulary, with category `ai_model_provider`. The
  validator holds that category: the organization that builds and operates the model behind a
  coding agent is an AI model provider. An agent acc does not know has a null vendor, and its
  supplier is `TODO`.
- **an MCP server's or a plugin's publisher** is `TODO`, with category `TODO`. The collector never
  guesses one: a hostname is not a publisher, a package scope is not a publisher, and a marketplace
  repository's owner is not necessarily the plugin's author.

A person replaces each `TODO`, picks the category from the `createVendor` vocabulary, merges
suppliers that are the same organization (two servers, one vendor), and signs each one. Two
suppliers with the same vendor name are refused, because Noru would land them as one vendor; a
supplier nothing depends on is refused, because it would land a vendor nobody uses.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/dev-agents:scan` | no | Reads the acc manifest and the agent configuration, asks Noru for its controls, writes `.noru/dev-agents.yml` with every supplier flagged `needs_review: true` |
| `/dev-agents:diff` | no | Probes existing vendors and evidence, prints the exact plan |
| `/dev-agents:push` | **yes** | Creates each supplier as a vendor, lands the window as one evidence record, and links it to the controls the manifest chose |

A manifest carrying any `needs_review: true` cannot be pushed. The collector proposes; a person
decides.

## What lands in Noru

- one `createVendor` per supplier: its name, its category, and a description naming the agents,
  servers and plugins it stands for;
- one `createEvidence` for the window: the agents and their vendors, the changes each authored and
  reviewed, the MCP servers and plugins, the suppliers and who decided them, and who reviewed the
  inventory. Totals and names of tools only — nothing per person;
- one `linkEvidenceToControl` per control the manifest chose from the queue snapshot. The validator
  refuses a control id the snapshot did not offer; this plugin ships no control ids.

## Expiry

`expires_at` is required on every claim, at most **400 days** out.

| Claim | Anchored on | Why |
|---|---|---|
| a supplier | `decided_at` | Who stands behind a tool is a claim about now, not about a period. The derived digest catches the configuration changing; the date catches nobody having looked |
| the attestation | the window's end | An account of a quarter, signed late, does not cover more of the quarter. It also cannot be signed before the window closes |

Nothing here reads the clock; staleness is CI mode's job, in the expiry step.

## Scopes

Least privilege. Start read-only.

| Capability | Scopes |
|---|---|
| `:scan` | `read:controls` |
| `:diff` | adds `read:organization` to bind the plan, `read:vendors`, `read:evidence` |
| `:push` | adds `write:vendors`, `write:evidence` |

Authentication is the MCP client's job. This piece never reads, writes or logs a credential — and
reads configuration files in a way built so that it cannot copy one.

## Artifact

`.noru/dev-agents.yml`, schema at
[`contract/dev-agents.schema.json`](../../contract/dev-agents.schema.json).

Commit it. Keep `.noru/.cache/` out of git: the acc manifest in it names the people who wrote and
reviewed each change, and `.noru/.cache/dev-agents.operators.json`, if you asked for it, counts
their agent use.

Because the acc manifest lives in `.noru/.cache/`, the `refs[]` that cite it point at a file that
is not committed. CI mode reports that as `dangling_ref`, which is advisory by default and the
correct outcome.

## Idempotency

| Operation | Transport | Kind | Key |
|---|---|---|---|
| `createVendor` | MCP | `server_dedupe` | `name` (the published tool description: an existing vendor with the same name is returned) |
| `createEvidence` | MCP | `client_probe` | marker in the description |
| `linkEvidenceToControl` | MCP | `client_probe` | `evidenceId` + `controlId` |

The evidence marker is built from the repository slug, the window and a digest of the rendered
inventory, so an unchanged inventory is found and skipped, and a changed one is a new record beside
the old one. A second `:scan` + `:diff` on unchanged input is a plan of all `skip`;
`scripts/test_idempotency.py` asserts it. What a documented key would buy is in
[`piece.json`](./piece.json).

## Verify

```bash
node    plugins/dev-agents/scripts/collect.mjs --repo=. --output=json
python3 plugins/dev-agents/scripts/validate_manifest.py .noru/dev-agents.yml
node    plugins/dev-agents/scripts/diff.mjs --repo=.
node    plugins/dev-agents/scripts/push.mjs --repo=. --confirm
```

Exit codes: `0` success · `1` drift, validation failure, an unusable acc manifest, a missing acc
manifest the committed manifest was built from, or a stale plan · `2` usage, including a push
without `--confirm`.
