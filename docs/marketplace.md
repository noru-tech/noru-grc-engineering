# Marketplace capabilities and compatibility

Every piece is independently installable. Each declares the same logical hosted Noru MCP server at
`https://api.noru.tech/v1/mcp`; none depends on the `noru` hub to own or proxy that connection. The
repository gate compares the parsed server declarations structurally so configuration
drift cannot silently create a different endpoint or authentication expectation.

`repo-enforcement` is separately installable but is a utility, not a piece. It has no Noru MCP
server and cannot publish a compliance claim. Its local capability creates reviewable repository
files; its optional GitHub capability reads rules and, only after a fresh plan and explicit
confirmation, creates or updates one dedicated repository ruleset.

## What is visible before installation

Each Codex manifest's `interface.capabilities` names four boundaries explicitly:

- `Local read:` repository or artifact access
- `Local write:` generated review files and cache behaviour
- `Noru read:` organization data the piece may inspect
- `Noru write:` the records it may change, always after diff and confirmation

Default prompts explicitly prohibit Noru writes. The Claude marketplace supplies the plugin's
Compliance category, description and keywords; the Codex marketplace uses the platform's supported
Productivity category while the interface copy carries the more precise compliance, security and
privacy capability labels.

## Executable compatibility contract

The public `plugins/<piece>/piece.json` is the structured source of truth where marketplace schemas
do not support a field. It declares:

- validator runtime and entrypoint;
- generated artifacts and their purpose;
- exact read and write scopes;
- every write operation and whether its transport is MCP or REST;
- provenance and confirmation requirements.
- generic CI validator, drift-check, and watch-path declarations consumed by the released
  whole-repository enforcement registry.

Collectors require Node.js 18 or newer. Validators require Python 3 and use only the standard
library, with optional PyYAML parity coverage in the repository matrix. Seven pieces publish over
MCP. `evidence-push` uses REST for multipart file upload because MCP tool arguments cannot carry the
file body; it is the only piece whose push reads `NORU_API_KEY` directly.

The repository checks this metadata, plugin names and versions across both marketplace formats.
Platform-only presentation such as suite collections, upgrade warnings and shared connection UI is
not represented as a plugin runtime dependency.

## One marketplace for every Noru plugin

`/plugin marketplace add noru-tech/noru-grc-engineering` (or the Codex and Copilot CLI equivalents)
gives access to every Noru plugin, including `compliance-assistant`, which lives in
[`noru-tech/compliance-assistant`](https://github.com/noru-tech/compliance-assistant). The
marketplaces list it from that repository rather than copying it:

| File | Client | `compliance-assistant` source |
|---|---|---|
| `.claude-plugin/marketplace.json` | Claude Code | `git-subdir`: `url`, `path`, `ref`, `sha` |
| `.agents/plugins/marketplace.json` | Codex | the same `git-subdir` object |
| `.github/plugin/marketplace.json` | GitHub Copilot CLI | `github`: `repo`, with the same `path`, `ref`, `sha` |

Copilot CLI needs its own file because it rejects a whole marketplace that contains a `git-subdir`
source, while Claude Code ignores `path` on a `github` source. Copilot CLI reads
`.github/plugin/marketplace.json` before `.claude-plugin/marketplace.json`.

The external entry is pinned to the full commit of a `compliance-assistant` release, so what users
install changes only when this repository changes. It keeps its own version (it is not part of this
repository's release), so the shared-version rule does not apply to it. To move it to a new
`compliance-assistant` release, update `ref`, `sha`, `version` and `description` in all three files
together. `scripts/check_repo.py` checks that the source is a `noru-tech` repository over https, that
the `sha` is a full commit, that Codex and Copilot carry the same pointer, and that no plugin
directory here uses the same name.
