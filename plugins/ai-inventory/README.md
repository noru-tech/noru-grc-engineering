# ai-inventory

> Inventory the AI systems a repository actually contains, review it as a committed manifest, and
> land it in Noru as assets, vendors and evidence.

This is the piece a server-side integration structurally cannot do. The truth about which model is
called from which line, what data reaches it, whether a person approves the output, and whether
anything fails when the evals fail lives in the repository — not in an API a scheduled job can poll.

**What it targets:** `iso_42001` and `eu_ai_act` — the frameworks that ask what AI systems you run,
who oversees them, and what the provider does with your data. It is not aimed at ISO 27001 or SOC 2,
and it should not be read as a route to either; for those, work your organization's own evidence
queue with [`evidence-push`](../evidence-push/README.md).

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/ai-inventory:scan` | no | Deterministic offline scan → `.noru/ai-inventory.yml` |
| `/ai-inventory:diff` | no | Reads current state, prints the exact plan |
| `/ai-inventory:push` | **yes** | Executes the confirmed plan |

## Scopes

Least privilege. Start read-only — `:scan` and `:diff` are useful before the piece is ever allowed
to write.

| Capability | Scopes |
|---|---|
| `:scan` (repository only) | none — it makes no Noru call |
| `:diff` | `read:organization`, `read:frameworks`, `read:controls`, `read:evidence`, `read:assets`, `read:vendors` |
| `:push` | the above plus `write:assets`, `write:vendors`, `write:evidence` |

## Artifact

`.noru/ai-inventory.yml`, schema at [`contract/ai-inventory.schema.json`](../../contract/ai-inventory.schema.json).

```yaml
version: 0.1.0
piece: ai-inventory
source: { slug, commit_sha, branch, generated_by, derived_digest }
providers:
  - key: …
    vendor_name: …
    claims: [{ kind, value, source: { type, ref|url, retrieved_on }, interpretation }]
    refs: ["path/to/file.ts:37"]
    interpretation: { owner, decided_at, expires_at, rationale }
ai_systems:
  - key: …          # stable: it becomes half the Noru asset upsert key
    name: …
    purpose: …
    deployment: hosted_api | self_hosted | on_device | embedded_library
    autonomy: assistive | supervised | autonomous
    human_oversight: [{ type, description, refs }]
    retrieval: [{ name, kind, refs }]
    evals: { suites: [{ name, path }], ci_gated: true|false }
    data_categories: [fideslang keys]
    refs: [...]
    interpretation: { … }
classifications:
  - system: …
    scheme: eu_ai_act_role | eu_ai_act_tier | iso_42001 | nist_ai_rmf
    value: …
    driver: "Article 50(1)"
    status: suggested        # always; a human accepts or rejects in Noru
    refs: [...]
    interpretation: { … }
```

Commit it. `.noru/.cache/` is machine state — keep it out of git.

## Idempotency

| Write | Kind | Key |
|---|---|---|
| `createAsset` | server upsert | `(source, externalId)` where source is `noru-ai-inventory` — documented upsert behaviour |
| `createVendor` | server dedupe | vendor name — the published tool description says an existing record is returned |
| `createEvidence` | **client probe** | a content marker in the description; no idempotency key is documented for evidence |
| `linkEvidenceToControl` | client probe | the piece reads the control context first; a duplicate link comes back as `ALREADY_LINKED`, which is benign |

The two client probes are fallbacks, not design. Each is written down in
[`piece.json`](./piece.json), with the public documentation it was checked against and what a
documented key would let the piece drop.

A second `:scan` + `:diff` on an unchanged repository must produce a plan of all `skip`. If it does
not, that is a bug.

## Verify

```bash
node    plugins/ai-inventory/scripts/collect.mjs --repo=. --output=json
python3 plugins/ai-inventory/scripts/validate_manifest.py .noru/ai-inventory.yml
node    plugins/ai-inventory/scripts/diff.mjs --repo=.
node    plugins/ai-inventory/scripts/push.mjs --repo=. --confirm
```

Exit codes: `0` success · `1` drift, validation failure, or a missing prerequisite · `2` usage,
including a push without `--confirm`.

## Detection coverage

`scripts/collect.mjs` recognises OpenAI, Anthropic, AWS Bedrock, Google Vertex, Azure OpenAI,
Mistral, Cohere, Ollama and Hugging Face; the Vercel AI SDK, LangChain, LlamaIndex, Semantic
Kernel and MCP; Pinecone, Weaviate, Qdrant, pgvector, Chroma, Milvus, LanceDB and FAISS. Model ids
are matched by shape, not by an enumerated list, so a model released after this collector was
written is still found.

It will miss a provider called through a hand-rolled HTTP client with no recognisable import. That
is what the reviewing human is for — the collector is a first pass, not an oracle.
