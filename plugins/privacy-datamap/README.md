# privacy-datamap

> Read the schemas a repository actually contains, classify the personal data in them against
> the Fideslang taxonomy, and land the data map in Noru — with a citation for every field and a
> named owner for every judgement.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/privacy-datamap:scan` | no | Deterministic offline collection → `.noru/privacy-datamap.yml` |
| `/privacy-datamap:diff` | no | Reads current state, prints the exact plan |
| `/privacy-datamap:push` | **yes** | Executes the confirmed plan |

## What it reads

| Format | Files | What it takes |
|---|---|---|
| SQL DDL | `*.sql` (schemas, migrations) | `CREATE TABLE` → collection, each column → field |
| Prisma | `*.prisma` | `model` → collection, each field |
| Python ORM | `*.py` | Django `models.Model` and SQLAlchemy declarative classes; an attribute assigned from `Column(...)`, `mapped_column(...)` or a `*Field(...)` call |
| Protobuf | `*.proto` | `message` → collection, each numbered field |
| GraphQL SDL | `*.graphql`, `*.gql`, `*.graphqls` | `type` and `input` → collection, each field |

**Not read yet**, and worth saying so plainly rather than letting an empty result imply a clean
repository: OpenAPI and JSON Schema, TypeORM and Sequelize entities, Mongoose schemas, ActiveRecord,
Ecto, GORM structs, and TypeScript or Zod DTOs. A repository whose schema lives only in one of those
will produce an empty data map, which is not the same as having no personal data in it. If the piece
finds nothing, check this table before concluding anything.

## Structure is derived, meaning is judged

This split is the whole design, and it is what lets a collector be deterministic (contract
requirement 2) while the interesting part of the work is a judgement.

**The collector stands behind** the structure: that a column named `email` exists at
`db/schema.sql:12` is a parse, not an opinion, and it carries the `file:line` to prove it. It also
classifies the field names it can resolve by **exact lookup** against
[`references/classification.json`](./references/classification.json) — a table, not an inference. A
name only belongs in that table when it means the same thing in every schema it appears in.

**A person stands behind** everything else, and the collector marks it rather than guessing:

- a field name the table does not know → `needs_review: true`
- what each system uses the data *for* — `data_use`, `data_subjects`, the purpose → `needs_review: true`
- the `interpretation` block on each collection and each declaration: who decided, when, until when, why

A manifest carrying any `needs_review: true` **cannot be pushed**. That is the mechanism, not a
lint: a confidently wrong data category is worse than a gap, because the gap gets reviewed and the
wrong answer gets signed.

The claim unit is the **collection**, not the field. One person signs for "these are the categories
in this table"; per-field attribution would mean five hundred interpretation blocks on a
five-hundred-column schema, which is a form nobody fills in. Field-level uncertainty still shows,
as `needs_review` flags inside the collection that block the push.

Special-category data — GDPR Article 9, plus Article 10 criminal-offence data — is collected into
its own list so a reviewer never has to go looking for the highest-risk thing in the map.

## Accuracy

The validator guarantees every key it emits is a **real** Fideslang key. It cannot guarantee the
judgement is **right**. Classification is a model inference over a lookup table; review the
`needs_review` items and spot-check the rest before treating the output as authoritative. This
accelerates a data map. It does not replace privacy review.

## Scopes

Least privilege. Start read-only.

| Capability | Scopes |
|---|---|
| `:scan` and `:diff` | `read:datamaps` — and nothing else |
| `:push` | adds `write:datamaps` |

`read:datamaps` is what Noru's API documentation calls "Read the privacy data map"; it covers
`getPrivacyTaxonomy`, `getPrivacyDataMap` and `listPrivacyDatasets`, which are the only three
tools this piece reads. `write:datamaps` is documented as "Push fideslang privacy manifests
(`.fides/datamap.yml`) from CI" — which is this piece, stated by the API itself.

## Artifact

`.noru/privacy-datamap.yml`, schema at [`contract/privacy-datamap.schema.json`](../../contract/privacy-datamap.schema.json).

Commit it — it is the reviewable artifact. Keep `.noru/.cache/` out of git.

## What it renders

`.fides/datamap.yml` — the same data map in Ethyca's own on-disk format, for the `fides` CLI and
anything else that reads a Fides manifest.

The two are not the same file and not interchangeable. `.noru/privacy-datamap.yml` is the
**manifest**: it carries the `file:line` citation behind every field, the interpretation block
behind every judgement, and the `needs_review` flags that block a push. `.fides/datamap.yml` is
that content projected down to plain Fideslang with the piece's own bookkeeping stripped out, and
it is only ever written from a manifest that validated against the repository as it stands right
now. Edit the manifest, never the export: the next scan overwrites the export and will not warn
you, because it has no way to tell your edit from its own output.

## Idempotency

One call, every time. `ingestDatamap` takes the whole data map for a source, so there is no fan-out
to keep idempotent — a repository with four hundred fields is still one write.

| Operation | Transport | Kind | Key |
|---|---|---|---|
| `ingestDatamap` | MCP | `server_dedupe` | `slug` + manifest content |

The tool description published at `https://api.noru.tech/v1/mcp` says: *"Idempotent: identical
content is a no-op."* That covers the case this piece actually re-runs in — CI on an unchanged
repository pushes the same manifest and nothing happens.

**What is not documented**, and what this piece therefore does not assume: what happens when the same
slug is pushed with *changed* content. Replaced, merged, or filed alongside as a second record —
nothing public says, and that is the ordinary case, because the manifest changes every time the
schema does. `https://api.noru.tech/llms.txt` documents the `write:datamaps` scope but its
"Idempotency/Upsert Behavior" section names only `POST /v1/assets` and `POST /v1/security-findings`.

So `:diff` reads `getPrivacyDataMap` and `listPrivacyDatasets` first and shows you the change set
rather than asserting what the server will do with it. If that behaviour gets documented as an
upsert on `slug`, this becomes `server_upsert`, the pre-read goes, and the piece is the shape
requirement 4 actually asks for.

A second `:scan` + `:diff` on unchanged input must produce a plan of all `skip`. If it does
not, that is a bug — `scripts/test_idempotency.py` asserts it.

## Verify

```bash
node    plugins/privacy-datamap/scripts/collect.mjs --repo=. --output=json
python3 plugins/privacy-datamap/scripts/validate_manifest.py .noru/privacy-datamap.yml
node    plugins/privacy-datamap/scripts/diff.mjs --repo=.
node    plugins/privacy-datamap/scripts/push.mjs --repo=. --confirm
```
