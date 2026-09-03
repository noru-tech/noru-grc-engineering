# Marketplace capabilities and compatibility

Every piece is independently installable. Each declares the same logical hosted Noru MCP server at
`https://api.noru.tech/v1/mcp`; none depends on the `noru` hub to own or proxy that connection. The
repository gate compares the parsed server declarations structurally so configuration
drift cannot silently create a different endpoint or authentication expectation.

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
- idempotency behaviour, provenance and confirmation requirements.

Collectors require Node.js 18 or newer. Validators require Python 3 and use only the standard
library, with optional PyYAML parity coverage in the repository matrix. Seven pieces publish over
MCP. `evidence-push` uses REST for multipart file upload because MCP tool arguments cannot carry the
file body; it is the only piece whose push reads `NORU_API_KEY` directly.

The repository checks this metadata, plugin names and versions across both marketplace formats.
Platform-only presentation such as suite collections, upgrade warnings and shared connection UI is
not represented as a plugin runtime dependency.
