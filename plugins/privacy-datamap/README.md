# privacy-datamap

> Read the persistent structures a repository actually establishes, classify the personal data in them against
> the Fideslang taxonomy, and land the data map in Noru — with complete cited structural facts and
> a named owner for every judgement.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/privacy-datamap:scan` | no | Reads schemas and evidence-backed supplemental stores → `.noru/privacy-datamap.yml` — and, once that manifest validates, renders `.fides/datamap.yml` |
| `/privacy-datamap:diff` | no | Reads current state, prints the exact plan |
| `/privacy-datamap:push` | **yes** | Executes the confirmed plan |

`scan` now has two deterministic local phases. `collect.mjs` observes the repository; then
`reconcile.py` compares those observations with the last accepted lock. The reconciler emits the
small set of new or materially changed ambiguous fields that need agent analysis. It never calls a
model itself, and unchanged fields are never reclassified. The agent inspects repository context,
groups proposals by collection, and asks the user only about genuine ambiguities, amendments and
the accountable owner rather than handing over the raw structural inventory.

## What it reads

| Format | Files | What it takes |
|---|---|---|
| SQL DDL | `*.sql` (schemas, migrations) | declarative `CREATE TABLE`, or ordered migration replay for the supported operations below |
| Drizzle | `*.ts`, `*.tsx`, `*.js`, `*.jsx` | common multiline `pgTable`, `mysqlTable` and `sqliteTable` declarations, including nested builder calls |
| Prisma | `*.prisma` | `model` → collection, each field |
| Python ORM | `*.py` | Django `models.Model` and SQLAlchemy declarative classes; an attribute assigned from `Column(...)`, `mapped_column(...)` or a `*Field(...)` call |
| Protobuf | `*.proto` | `message` → collection, each numbered field |
| GraphQL SDL | `*.graphql`, `*.gql`, `*.graphqls` | `type` and `input` → collection, each field |
| Supplemental stores | `.noru/privacy-datamap-stores.json` | explicitly declared object stores, queues, search indexes and third-party stores; every field needs its own repository citation |

Drizzle parsing is deliberately static: literal table names and object-literal column maps are
supported, including comments between field declarations. Spreads, shorthand properties and
declarations assembled through runtime values are reported as `drizzle` coverage rather than
silently treated as a complete table or executed.

**Not read automatically yet**: OpenAPI and JSON Schema, TypeORM and Sequelize entities, Mongoose
schemas, ActiveRecord, Ecto, GORM structs, and TypeScript or Zod DTOs. A repository whose schema
lives only in one of those produces an empty data map, which is not the same as having no personal
data in it.

That used to be a sentence in this README that you had to remember to read. It is now a check. The
collector looks for the marker that says "a schema is defined here" in each of those formats and
records what it found under `coverage` in its derived facts:

- **parsed nothing, found one of these** → CI mode exits `6`, a broken gate rather than a pass, and
  `--mode=warn` does not suppress it. An empty map cannot be reported as a clean one.
- **parsed something, still found one of these** → a `coverage` finding, advisory by default because
  failing there would block a repository that has one such file beside its SQL. Gate it with
  `--fail-on=coverage` where the map is meant to be complete.

Two formats on the list above are deliberately **not** markers, which is a precision decision made
after running this against a real repository. `"$schema": ".../json-schema.org/..."` appears in every
JSON Schema document, including the ones that describe a manifest format rather than anything
stored — this repository's own `contract/` directory produced ten candidates holding no personal data
at all. `z.object(` is overwhelmingly request validation rather than persistence. A check that fires
on every repository with a schema directory is a check somebody turns off, and then it catches
nothing. The rule the line draws: **a marker earns its place when it means "a stored record is
defined here", not merely "a shape is described here"**. The parser gap for both formats is still
real, which is why they stay on the list.

The marker is a deterministic text match, never an attempt to read the schema — the honest output is
"there is one here and I cannot see inside it". A shape nobody has written a marker for is still
invisible, so this table is still the thing to read before trusting a small result.

## From observations to logical topology

A parsed file is a structural **observation**, not automatically a dataset. The collector groups
schema files under their nearest datastore boundary and merges tables contributed by multiple
declarative files. The complete current fields and file-shaped observations remain in
`.noru/.cache/privacy-datamap.derived.json` with their citations. New fields stay visible in the
review candidate. Once the collection decision is accepted, non-personal names move to a compact
collection-level `non_personal_fields` list without leaving the derived facts or accepted lock.

Tracked `drizzle.config.ts`, `.js`, `.mts`, `.cts`, `.mjs` and `.cjs` files provide an explicit
cross-directory topology edge when `schema` and `out` use static paths. Both paths are resolved
relative to the config. Schema observations keep
the canonical schema boundary and the generated SQL output joins it as migration history. A table
name shared by two stores is never enough to merge them; only the config link is.

Declarative schemas are the current-state authority when they share a boundary with migrations.
That includes Drizzle, Prisma, Django/SQLAlchemy and standalone SQL schemas. Historical migrations
remain as observations but do not produce duplicate datasets or fields. A migration-only datastore
is replayed in lexical path order. The supported structural subset is:

- `CREATE TABLE`
- `ALTER TABLE ... ADD COLUMN`, `DROP COLUMN`, and `RENAME COLUMN ... TO ...`
- `ALTER TABLE ... RENAME TO ...`
- `DROP TABLE`

Drizzle's `--> statement-breakpoint` marker is only a delimiter. Table-level foreign-key, check,
unique and primary-key constraints are inventory-neutral and do not create migration gaps. The SQL
parser reads column declarations only at the top level of `CREATE TABLE`, so multiline `CHECK`
expressions cannot become fields.

For a migration-only datastore, a structural statement outside that subset or one that references
missing state omits the datastore from the logical map and appears under
`coverage.migration_gaps`; the collector does not guess a partial current state. When a canonical
schema exists, it remains authoritative: migrations stay in the audit observations, but replay
limitations are not current coverage gaps. Conflicting canonical field shapes remain blocking
`coverage.schema_conflicts`. CI reports current coverage gaps alongside unsupported formats.

Dataset, collection and field identities come from the logical datastore boundary and schema
names, not migration filenames. Normalized-key collisions are checked before any manifest is
written and fail with both colliding boundaries. This also means a hidden directory such as
`.example` becomes `example`, never an empty key that silently falls back to `repository`.

Systems use a separate evidence pass. Package metadata alone is weak evidence and creates no
system. A runtime boundary needs a container or deployment definition, a Kubernetes workload, a
server/worker entrypoint, or an executable start/deploy script that points to an application
entrypoint. Libraries and tooling packages therefore stay out of the topology. If none of those
signals exists, the collector emits one conservative repository-level system. Runtime discovery
ignores markers inside conventional test, fixture and example directories. It
does not infer purpose, data use, subjects, or access to a datastore outside the runtime's own
directory; those remain human review decisions.

Runtime discovery ignores conventional test and fixture directories, including `__tests__` and
`__fixtures__`, plus `*.test.*` and `*.spec.*` files. A server-like call in test support code is not
evidence of a deployed system.

## Stores without a declarative schema

SDK initialization or a bucket, queue, index or third-party client call can show that a store
exists, but it cannot show which object fields the repository persists. The collector therefore
never invents a dataset or fields from a client call alone. For structures that cannot be derived
safely, commit `.noru/privacy-datamap-stores.json`, validated by
[`contract/privacy-datamap-stores.schema.json`](../../contract/privacy-datamap-stores.schema.json).

Each datastore and collection cites the integration or contract that establishes it. Every field
also carries an `evidence_kind` and its own repository `file:line` citation. Accepted evidence kinds
are `typed_contract`, `serializer`, `upload_payload` and `download_result`; a citation to the
supplement itself is rejected. Optional `system_references` attach the datastore to system keys
that runtime discovery actually found. Invalid citations, unknown system keys, duplicate identities
and untracked supplements fail the scan instead of creating a guessed map.

```json
{
  "$schema": "https://raw.githubusercontent.com/noru-tech/noru-grc-engineering/v0/contract/privacy-datamap-stores.schema.json",
  "version": "1.0.0",
  "datastores": [{
    "fides_key": "customer_objects",
    "name": "Customer object storage",
    "store_type": "object_storage",
    "provider": "gcs",
    "refs": ["src/storage.ts:18"],
    "system_references": ["src"],
    "collections": [{
      "name": "uploaded_files",
      "refs": ["src/storage.ts:5"],
      "fields": [{
        "name": "object_key",
        "shape": "string",
        "evidence_kind": "typed_contract",
        "refs": ["src/storage.ts:6"]
      }]
    }]
  }]
}
```

## What it scans

**Tracked files, wherever there is a git to ask** — `git ls-files`, which is the same set
`actions/checkout` gives CI, and which honours `.gitignore`, `.git/info/exclude` and your global
excludes file without this collector reimplementing any of them. That is what makes a scan on your
machine and a scan in CI the same question: a working tree usually holds more than the repository
does — worktrees, scratch checkouts, unpacked archives — and each of those is a full copy of the
schema as far as a directory walk can tell. Mapping them produces datasets keyed off paths that are
not in the repository, and drift no one can resolve, because the committed manifest can match one
environment or the other and never both.

Three consequences worth knowing. A **tracked** file that an ignore rule also matches is still in
scope — it is in the checkout, so it is in the map. An index entry that is not on disk (a sparse
checkout, a pending deletion) is not, because a file the collector cannot open is not one it can
describe. And a schema file you have written but not yet `git add`ed is not in the map either: it
is not in the checkout, so mapping it would put back the same disagreement in a smaller form. Stage
it and scan again. Vendored and build directories (`vendor/`, `dist/`, `node_modules/`, …) stay
excluded even when committed: those hold a dependency's schemas, not yours.

Scanning something that is **not** a work tree — an exported tarball, a directory with no `.git` —
is a legitimate thing to do, and there the collector reads what is on disk instead. That is a
different question, so it says which one it answered: `coverage.enumerated_by` in the derived facts
is `git` or `walk`, and the scan summary says so in words. Same files either way means the same
`derived_digest`, so an export and a checkout of one commit do not read as drift.

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

## Incremental reconciliation

A repeatable scan compares normalized observations with the accepted baseline. Citation locations,
file counts, discovery order and classification lookup changes do not change the structural digest.
The manifest can register `evidence_dependencies` on processing code and configuration; the lock
records their reviewed fingerprints. Changed evidence queues a scoped investigation, while baseline
and taxonomy maintenance remain separate from confirmed structural drift. See the scan command for
`dependencies.py`, Python and TypeScript AST selectors, JSON normalization and the conservative text
fallback. TypeScript fingerprints use a bundled supported-grammar parser; unsupported syntax is an
explicit evidence gap. They preserve types, literal values, calls and conditions while ignoring
comments, layout and optional trailing commas. Imports/helpers/configuration remain explicit dependencies.

Proposal caches now carry per-target `evidence_state` and an `evidence_dependencies` registry. An
unrelated README edit preserves analysis; changed serializer or connection evidence flags only its
dependent fields, activities and relationships. Unique source moves refresh citations; missing or
ambiguous dependencies remain unresolved. Superseded structural analysis is retained separately.

Broad code/configuration discovery continues outside those dependencies. New files and unregistered
declarations enter `discovery_required`; existing analysis stays intact. Evidence-backed discovery
answers are required before sealing a new baseline, and unreviewed new scope blocks export. See the
scan command's “Scoped analysis and broad discovery” for cache targets, supported syntax and answers.

The first completed review is sealed in `.noru/privacy-datamap.lock.json`. The lock contains stable
field identities, normalized structural fingerprints and citations. It contains no classifications
or model output: accepted meaning stays in `.noru/privacy-datamap.yml`.

On later scans `scripts/reconcile.py` compares every current field with that observation:

| Result | What happens |
|---|---|
| same semantic fingerprint and citation | carry the accepted decision forward |
| same fingerprint, different line | refresh evidence; no agent |
| new or structurally changed exact-table field | classify deterministically; re-sign the collection |
| new or structurally changed ambiguous field | add only that field to the agent proposal queue |
| removed field | remove it from the candidate; re-sign the collection |
| unique logical-identity migration | carry the decision forward under the new datastore key |

The reconciler writes the structural delta, proposal work queue and candidate manifest in
`.noru/.cache/`. After agent enrichment, `scripts/review.py --repo=<repo> --output=json` produces
three outputs: `privacy-datamap.map.md` summarizes stores and processing, `privacy-datamap.review.md`
groups human decisions, and `privacy-datamap.evidence.json` retains the full field inventory and
reasoning. Explicit decision IDs group related fields despite differences in reasoning; short decision
summaries replace detailed rationale in review. Technical fields remain in evidence. Evidence-backed
activities need no questions; a question must identify the privacy decision its answer changes. Each processing activity and actionable uncertainty has its own description. Structural
errors block review readiness until corrected. The reconciler's initial index is only navigation.
These are working files and must
not be committed. The candidate never overwrites the accepted manifest. In bootstrap mode an
invalid manifest contributes no descriptions, systems, declarations or references to the
candidate. After the candidate has been resolved and reviewed, `reconcile.py --seal` refuses to
write the lock unless the manifest is valid and matches the current observations.

A valid manifest from a release before locks existed enters migration mode. Its decisions are
carried forward and its first lock is seeded without sending every field back through an agent.
When a normalized identity replaces an older file-derived identity, maintenance mode matches only
one-to-one candidates with the same collection and field, a compatible normalized scalar type
family (for example SQL `TEXT` and a Drizzle `text(...)` builder), and supporting source scope.
Ambiguous candidates appear in `identity_ambiguities` and are left for review; the
reconciler never chooses one by similarity alone. The first normalized release can change dataset
keys in the downstream diff even when field decisions carry forward, so review the dataset-level
create/archive plan before pushing.

The claim unit is the **collection**, not the field. One person signs for "these are the categories
in this table"; per-field attribution would mean five hundred interpretation blocks on a
five-hundred-column schema, which is a form nobody fills in. Field-level uncertainty still shows,
as `needs_review` flags inside the collection that block the push. Proposed personal,
non-personal, ambiguous and special-category fields are reviewed as a collection group; accepting
the non-personal group moves those names to `non_personal_fields` without removing them from the
structural audit trail.

Special-category data — GDPR Article 9, plus Article 10 criminal-offence data — is collected into
its own list so a reviewer never has to go looking for the highest-risk thing in the map.

## When a signature stops counting

Two things anchor a claim, and the pair is the point.

**`structure_digest` pins what a signature was given for.** Every collection carries a digest of the
union of its verbose field names and compact `non_personal_fields` — not their categories — so
compacting a decision keeps the signature, and adding, removing or renaming a column breaks it:

```
ERROR dataset[0].collections[0].structure_digest: does not match this collection's fields
      (stamped 4abfeae8e1be, computed 9c2f0a71d33e) — a column was added, removed or renamed
      since this was signed, so the signature is no longer a statement about this table.
      Re-run :scan, review what changed, and sign again
```

The validator recomputes it rather than trusting the stamp and, when current derived facts are
present, compares that union with the complete observed collection. Compacting the review file
therefore cannot hide a field from drift detection.

**`expires_at` pins how long nobody has looked.** Required, and measured from `decided_at`:

| The collection holds | It may stand for |
|---|---|
| ordinary personal data | 365 days |
| GDPR Article 9, or Article 10 criminal-offence data | 183 days |

`decided_at` is an honest anchor **here** and is not in most pieces. Elsewhere it rewards signing
late — a claim about March, signed in August, gets its clock started in August. Here it cannot,
because what the claim is about is pinned by digest rather than by date: a signature cannot outlive
the structure it was given for. That is the whole reason the structural anchor is worth its
bookkeeping, and it is written up in
[`contract/README.md`](../../contract/README.md) under requirement 8.

Pass `--as-of=YYYY-MM-DD` to turn an already-expired claim into an error. Leave it off and the file
is judged on its own terms — nothing in the validator reads the clock by itself, so it stays
deterministic and the staleness check happens where it belongs, in CI or before a release.

## Accuracy

The validator guarantees every key it emits is a **real** Fideslang key. It cannot guarantee the
judgement is **right**. Exact-name classifications are deterministic table lookups; classifications
proposed for ambiguous names may use agent inference over the repository context. Review those
proposals and spot-check the deterministic matches before treating the output as authoritative.
This accelerates a data map. It does not replace accountable privacy sign-off.

## Scopes

Least privilege. Start read-only.

| Capability | Scopes |
|---|---|
| `:scan` | `read:datamaps` |
| `:diff` | adds `read:organization` to bind the plan to its target |
| `:push` | adds `write:datamaps` |

`read:datamaps` is what Noru's API documentation calls "Read the privacy data map"; it covers
`getPrivacyTaxonomy`, `getPrivacyDataMap` and `listPrivacyDatasets`, which are the only three
tools this piece reads. `write:datamaps` is documented as "Push fideslang privacy manifests
(`.fides/datamap.yml`) from CI" — which is this piece, stated by the API itself.

## Artifacts

`.noru/privacy-datamap-stores.json`, schema at
[`contract/privacy-datamap-stores.schema.json`](../../contract/privacy-datamap-stores.schema.json),
is an optional committed structural input for stores that no supported schema describes. It is
reviewed source, not generated output.

`.noru/privacy-datamap.yml`, schema at [`contract/privacy-datamap.schema.json`](../../contract/privacy-datamap.schema.json).

Commit it — it is the reviewable artifact. Keep `.noru/.cache/` out of git.

`.noru/privacy-datamap.lock.json`, schema at
[`contract/privacy-datamap-lock.schema.json`](../../contract/privacy-datamap-lock.schema.json), is
the machine-generated observation that was accepted with the manifest. Commit it too; never edit it
by hand.

## What it renders

`.fides/datamap.yml` — the same data map in Ethyca's own on-disk format, for the `fides` CLI and
anything else that reads a Fides manifest.

The two are not the same file and not interchangeable. `.noru/privacy-datamap.yml` is the
**manifest**: it carries citations for review-bearing fields, compact non-personal names, the
interpretation block behind every judgement, and the `needs_review` flags that block a push.
Complete citations and shapes remain in derived facts and the accepted lock. `.fides/datamap.yml`
contains only privacy-relevant fields: non-personal leaves and empty collections or datasets are
removed, and system references are restricted to retained datasets. It is only ever written from a
manifest that validated against the repository as it stands right now. Edit the manifest, never the
export: the next scan overwrites the export and will not warn you.

## Idempotency

One call, every time. `ingestDatamap` takes the whole data map for a source, so there is no fan-out
to keep idempotent — a repository with four hundred fields is still one write.

| Operation | Transport | Kind | Key |
|---|---|---|---|
| `ingestDatamap` | MCP | `server_upsert` | `slug` |

The documented behaviour, from the `Idempotency/Upsert Behavior` section of
`https://api.noru.tech/llms.txt`:

- **identical content is a no-op** — CI on an unchanged repository pushes the same manifest and
  nothing happens;
- **a changed manifest is upserted** — each system, dataset and processing activity it names is
  created or updated in place on its `fides_key`, and anything the manifest no longer names is
  **soft-archived** rather than deleted.

So a push *replaces* the data map for that slug rather than merging into it. Two consequences worth
knowing before you run it:

- **Dropping a table from your schema removes it from the map.** That is the intended behaviour —
  the map should describe the code — but it means a partial or mistaken scan pushed on top of a good
  one will archive what it failed to find. `:diff` shows you that before you confirm; read it.
- **`fides_key` is half the upsert key.** Renaming one archives the old record and creates a new
  one rather than renaming it in place, so keys are worth choosing once and leaving alone.

The content checksum covers the manifest only — `commitSha` and `branch` are excluded, so re-pushing
unchanged content from a new commit stays a no-op rather than re-materializing everything.

A second `:scan` + `:diff` on unchanged input must produce a plan of all `skip`. If it does
not, that is a bug — `scripts/test_idempotency.py` asserts it.

## Verify

```bash
node    plugins/privacy-datamap/scripts/collect.mjs --repo=. --output=json
python3 plugins/privacy-datamap/scripts/reconcile.py --repo=. --output=json
python3 plugins/privacy-datamap/scripts/validate_manifest.py .noru/privacy-datamap.yml
python3 plugins/privacy-datamap/scripts/reconcile.py --repo=. --seal
node    plugins/privacy-datamap/scripts/diff.mjs --repo=.
node    plugins/privacy-datamap/scripts/push.mjs --repo=. --confirm
```

## Connection relationship contract

The optional manifest `relationship_graph` stores framework-independent nodes and directed edges.
Explicit schema bindings override directory grouping. The same schema bound to two unresolved
connections produces two datasets, keyed `connection_<connection_id>`. Connections share a datastore
only through explicit edges to the same datastore node; schema similarity never supplies that edge.

Nodes have stable `id` and `kind` (`runtime`, `client`, `connection`, `datastore`, `schema`, `payload`).
Runtime/datastore nodes carry a manifest `key`. Schema/payload nodes carry repository-relative
`paths`. An unresolved connection carries `unresolved_question` and `resolution_needed`.
Edges have `id`, `source`, `target`, `dependencies`, `rationale`, and `confidence`. Allowed directions
are runtime→client, client→connection, connection→datastore, and client→schema/payload.
Each dependency ID must exist in `evidence_dependencies` and target `relationship:<edge_id>`;
its source fingerprint supplies the citation and freshness check. Changing that evidence queues
investigation of the relationship through the existing reconciler and blocks stale export/sealing.

The proposal cache uses `relationship_proposal: {graph, evidence_dependencies, decision_summary}`.
Use `collect.mjs --candidate`, `reconcile.py --candidate`, then `review.py --candidate` with the
same `--repo` to preview these proposals. Collection validates the graph and live evidence before
applying its bindings. The complete proposed graph replaces the mapping only within the preview.
Corrected observations, candidate manifest, enrichment queue and three review outputs live under
`.noru/.cache/privacy-datamap-preview/`. Normal scan/review artifacts, the accepted manifest, lock,
and Fides export stay unchanged. Candidate mode also works when no accepted manifest exists.

Complete the preview's enrichment queue before requesting acceptance. The rendered map uses the
corrected candidate stores and fields; missing processing context remains an explicit question.
Changed source proposals, evidence or baseline invalidate the preview and require recollection.
Invalid evidence fails before overwriting existing preview artifacts. Candidate mode cannot seal,
run `--check`, or export. Acceptance records the reviewed graph and decisions in the manifest;
normal collection and validation then establish the accepted observation baseline.

Extraction and identity are separate: framework adapters discover evidence, while the common graph
records relationships. Automatic client/import tracing is not implemented yet. Existing parsers
and static migration configuration provide discovery; the agent traces client wrappers and records
missing bindings. Without an explicit graph, the legacy directory grouping remains provisional.
A graph does not create payload fields: unsupported structures still require supplemental evidence.
