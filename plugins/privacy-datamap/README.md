# privacy-datamap

> Read the persistent structures a repository actually establishes, classify the personal data in them against
> the Fideslang taxonomy, and land the data map in Noru — with complete cited structural facts and
> a named owner for every judgement.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/privacy-datamap:scan` | no | Reads schemas and evidence-backed supplemental stores → `.noru/privacy-datamap.yml` ; an explicit export renders `.fides/datamap.yml` after acceptance |
| `/privacy-datamap:diff` | no | Reads current state, prints the exact plan |
| `/privacy-datamap:push` | **yes** | Executes the confirmed plan |

`scan` collects current structure, reconciles it with the accepted baseline, builds compiler-backed
evidence, and directs the agent through contextual classification. One canonical review contract
inside the existing analysis cache records the evidence and decisions. Names supply investigation
hints; they never classify a field automatically. The agent resolves code questions before asking
for business context or accountable acceptance.

## What it reads

| Format | Files | What it takes |
|---|---|---|
| SQL DDL | current `*.sql` schemas | declarative `CREATE TABLE`; recognized migrations are excluded |
| Drizzle | `*.ts`, `*.tsx`, `*.js`, `*.jsx` | common multiline `pgTable`, `mysqlTable` and `sqliteTable` declarations, including nested builder calls |
| Prisma | `*.prisma` | `model` → collection, each field |
| C#/.NET | `*.cs` and project metadata | bounded persisted model/property extraction and modeled EF, MongoDB, DynamoDB and literal typed SQL bindings; [support and limits](references/dotnet-support.md) |
| Python ORM | `*.py` | Django `models.Model` and SQLAlchemy declarative classes; an attribute assigned from `Column(...)`, `mapped_column(...)` or a `*Field(...)` call |
| Protobuf | `*.proto` | `message` → collection, each numbered field |
| GraphQL SDL | `*.graphql`, `*.gql`, `*.graphqls` | `type` and `input` → collection, each field |
| Supplemental datasets | `.noru/privacy-datamap.yml` | ordinary datasets with `meta.noru.origin: supplemental`; every field cites its structural evidence |

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
the canonical schema boundary and the generated output is excluded from collection. A table
name shared by two stores is never enough to merge them; only the config link is.

Current ORM and declarative schemas supply the field inventory. Migration files are excluded
before parsing, runtime discovery and code-change discovery; the collector never replays them.
Recognized paths include migration directories/filenames, Alembic versions, Rails db/migrate,
Liquibase db/changelog, Drizzle SQL output, Flyway SQL names and timestamp-prefixed migration
filenames. Static Drizzle `out` paths exclude generated files even when the directory has a custom
name or its current schema is missing. Custom layouts that match none of these conventions remain
a discovery limitation; do not treat historical declarations as current schemas during agent review.

`coverage.excluded_migrations` lists paths. A separate bounded adapter reads identity metadata and
fingerprints migration evidence; historical declarations never supply current fields. Explicit context
or configuration evidence links migrations to current schemas, with identity and completeness checked
separately. Incomplete or ambiguous coverage is reported in `coverage.migration_gaps`;
it contributes no guessed fields. Migrations cannot override current schemas through explicit
connection bindings. An accepted map that previously used migrations needs normal removal/identity
review against current evidence; do not silently carry obsolete fields forward.

For source helpers located under migration-named directories, the manifest may declare
a `migration_support` list with `path`, `sha256` (64 lowercase hex characters), and `rationale`
for each reviewed file.
These are exact source-role declarations, not ignore patterns: the files re-enter structure and
runtime/change discovery. Each file must be tracked/scanned and match its reviewed byte hash;
changed, missing, duplicate or escaping paths fail collection. Configured migration outputs,
SQL/data history, explicit migration filenames and known history layouts cannot be overridden.
The declarations carry into the candidate and structural digest. Review helper code before adding
one; never use this mechanism to treat migration history as a current schema.

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

Supplemental structures use ordinary `dataset` entries in `.noru/privacy-datamap.yml`.
Set `meta.noru.origin: supplemental` and `meta.noru.refs` on the dataset. Its collections and
fields retain source `refs`; each field adds `meta.noru.shape` and `meta.noru.evidence_kind`
(`typed_contract`, `serializer`, `upload_payload` or `download_result`). There is one definition
of the collections and fields, shared by collection, review and export. Nested payload properties
remain nested fields. Opaque file bytes remain a content field with explicit classification gaps.

External services use ordinary `system` entries with `meta.noru.origin: external`, source refs in
`meta.noru.refs`, and a suitable `system_type`. Use `dataset_references` to associate their data,
and native `ingress`/`egress` to represent evidenced transfers. A provider name alone does not
establish a connection identity, a flow direction, a processing purpose or field contents.

Propose additions in `structural_proposal.dataset` and `structural_proposal.system` in the normal
proposal cache, then run candidate collection, reconciliation and review. Include their monitoring
specs in `structural_proposal.evidence_dependencies`. Candidate previews do not modify accepted
files. After human acceptance, copy the reviewed definitions and dependencies to the main manifest,
collect normally, validate and seal. Supplemental field citations and external-system citations
must have registered evidence dependencies before acceptance. Unchanged evidence retains decisions.

The local `meta.noru` namespace records provenance, not a new Fides resource. Export strips it while
retaining other metadata. Supplemental datasets export like schema-extracted datasets; external
services and transfers export as systems and ingress/egress.

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

**The collector stands behind** structural observations. A declaration receives a source citation;
[`references/classification.json`](./references/classification.json) supplies optional category hints.
Every new or materially changed field remains `needs_review` until contextual investigation supports
a decision and a person accepts it.

**The plugin investigates** value meaning, natural-person association, security sensitivity and the
most specific supported taxonomy category. It retains typed facts, source spans, fingerprints,
counterevidence and unresolved work in the existing analysis cache. Nested members are reviewed
recursively. A parent cannot hide an unresolved child or dynamic branch.

**A person accepts** the grouped privacy conclusions, processing purposes, subjects and interpretation
blocks. Machine work left by an analysis budget remains agent work, not a request to classify code.

A manifest carrying any `needs_review: true` **cannot be pushed**. That is the mechanism, not a
lint: a confidently wrong data category is worse than a gap, because the gap gets reviewed and the
wrong answer gets signed.

## Accepting uncertainty

An investigated field or coverage finding can be explicitly accepted with uncertainty.
The agent presents the remaining uncertainty and map impact; a named human reviewer decides.
`investigate.py` supplies scoped `uncertainty_candidates` and accepts approved records through
`--uncertainties=<decisions.json>`. See [the workflow](references/accepted-uncertainty.md).

Accepted uncertainty clears the affected pending review, stays visible in the exported map,
and reopens when its evidence changes. Zero active gaps means zero unaccounted-for findings,
including those explicitly accepted with uncertainty. Unknown meaning is not relabeled non-personal.
Normal map acceptance and publication confirmation still apply.

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

Proposal caches carry per-target `evidence_state` and an `evidence_dependencies` registry for structural
and processing reconciliation. The canonical classification contract currently binds the complete
current source inventory conservatively: a code/configuration change requires re-extraction and
investigation, even if the structural delta is small. Unrelated Markdown changes do not invalidate
it. Do not claim selective semantic dependency closure beyond what the extracted facts establish.
Superseded structural analysis is retained separately.

Broad code/configuration discovery continues outside those dependencies. New files and unregistered
declarations enter `discovery_required`; existing analysis stays intact. Evidence-backed discovery
answers are required before sealing a new baseline, and unreviewed new scope blocks export. See the
scan command's “Scoped analysis and broad discovery” for cache targets, supported syntax and answers.

The first completed review is sealed in `.noru/privacy-datamap.lock.json`. The lock contains stable
field identities, normalized structural fingerprints and citations. It includes a compact accepted-meaning snapshot for semantic comparison; the authoritative
accepted map stays in `.noru/privacy-datamap.yml`.

On later scans `scripts/reconcile.py` compares every current field with that observation:

| Result | What happens |
|---|---|
| same semantic fingerprint and citation | carry the accepted decision forward |
| same fingerprint, different line | refresh evidence; no agent |
| new or structurally changed field | investigate source meaning, linkage and category fit; re-sign the collection |
| removed field | remove it from the candidate; re-sign the collection |
| unique logical-identity migration | carry the decision forward under the new datastore key |

The reconciler records pending investigations and proposals in
`.noru/.cache/privacy-datamap.analysis.json`. Candidate models, reconciliation results and review
views are reconstructed from current observations and the baseline rather than saved again.
After enrichment, `scripts/review.py --repo=<repo> --output=json` generates
`privacy-datamap.review.md`: the concise map followed by decisions. `--output=evidence` emits the
full evidence view as JSON on stdout without persisting a second inventory or copy of the
reasoning. Explicit decision IDs group related fields despite differences in reasoning; short decision
summaries replace detailed rationale in review. Technical fields remain in evidence. Evidence-backed
activities need no questions; a question must identify the privacy decision its answer changes. Each processing activity and actionable uncertainty has its own description. Structural
errors block review readiness until corrected. The reconciler emits machine-readable work items; the review renderer presents privacy decisions.
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

## When an agent may renew a signature

The structural anchor is what makes one more thing safe: an agent may **renew** a collection whose
structure and classification are exactly what a person signed. Nothing else. A new collection, a
changed one, a privacy declaration, or a collection an agent renewed last time is a person's to sign.

The repository opts in with a committed `.noru/interpretation-policy.yml`
([schema](../../contract/interpretation-policy.schema.json)). `repo-enforcement` already places
everything under `.noru/` behind the GRC reviewers in CODEOWNERS.

```yaml
kind: interpretation-policy
version: 1
grants:
  - id: datamap-renewals
    piece: privacy-datamap
    claims:
      - renewal          # the only value accepted; the validator enforces it, not the file
    agents:
      - datamap-renewal-agent
    operators:
      - Dana Okafor      # a named person who answers for the agent
    max_days: 60         # at most the piece's agent ceiling below
```

The renewal names the agent, the person who answers for it, the grant, and the decision it renews:

```yaml
interpretation:
  owner: datamap-renewal-agent
  owner_kind: agent
  operator: Dana Okafor
  policy_ref: datamap-renewals
  renews:
    owner: Dana Okafor
    decided_at: "2026-08-20"
  decided_at: "2027-02-15"
  expires_at: "2027-04-15"
  rationale: >-
    Every column is one Dana Okafor signed for, with the classification given then.
```

| An agent's renewal of a collection holding | It may stand for |
|---|---|
| ordinary personal data | 90 days, or the grant's `max_days` if shorter |
| GDPR Article 9, or Article 10 criminal-offence data | 45 days, or the grant's `max_days` if shorter |

Whether the collection is unchanged is a comparison with the manifest at the base revision, and the
grant is read from the policy as it stood there too. CI mode passes both to the validator
(`--base`, `--interpretation-policy`): a pull request that widens the policy is judged by the
policy it would replace, and a base CI cannot read refuses every agent-owned collection. Run without
`--base`, the validator warns that the renewal's lineage is unchecked. The
[fixtures](./fixtures/) named `*agent*` show each refusal.

## Accuracy

The validator checks taxonomy membership, recursive evidence completeness and freshness. It cannot
prove arbitrary business meaning from prose. Compiler facts and constrained decisions make the
review reproducible, while unresolved identities, transformations, dynamic content and unsupported
languages stay visible. Static extraction, agent-assisted classification and human acceptance are
separate steps. Synthetic application fixtures report classification accuracy, false exclusions,
coverage and unresolved work separately; a small resolved subset is not overall accuracy.

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
export: explicitly regenerate it with `collect.mjs --repo=<repo> --export`. Normal scans do not
write an export. Invalid or unresolved manifests fail explicit export without replacing an older file.

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
Corrected observations live in the analysis file’s `preview` section. Candidate reconciliation and
review update the same analysis file and review report. Normal scan observations, the accepted
manifest, lock and Fides export stay unchanged. Candidate mode also works when no accepted manifest exists.

Complete the preview's enrichment queue before requesting acceptance. The rendered map uses the
corrected candidate stores and fields; missing processing context remains an explicit question.
Changed source proposals, evidence or baseline invalidate the preview and require recollection.
Invalid evidence fails before overwriting existing preview artifacts. Candidate mode cannot seal,
run `--check`, or export. Acceptance records the reviewed graph and decisions in the manifest;
normal collection and validation then establish the accepted observation baseline.

Extraction and identity are separate: framework adapters discover evidence, while the common graph
records relationships. A bounded Python reader recognizes top-level literal instances of a local
frozen dataclass with exactly two string fields, `destination` and `schema_path`, and no custom
constructor. It records separate datastore keys and binding citations without importing target code.
Dynamic arguments, wrappers and imported constructors still need agent investigation. Explicit
relationship graphs take precedence over these discovered bindings. This models declarations,
not deployed persistence or transfers. General client/import tracing is not implemented. Existing
parsers and static migration configuration also provide discovery; the agent traces wrappers and records
missing bindings. Without an explicit graph, the legacy directory grouping remains provisional.
A graph does not create payload fields: unsupported structures still require supplemental evidence.

### Privacy review

The review reports collection, agent investigation, missing source facts, human approval and
publication separately. Known JSON members remain visible alongside an open-content assessment
and extraction limitations. User corrections can be recorded with `investigate.py --feedback`;
they queue evidence-based reconsideration and do not grant acceptance or publication.

The report leads with personal-data categories, data subjects, purposes, systems and sharing,
then meaningful changes and actionable decisions. Raw field inventories and collector lookup labels
stay in the evidence view. Missing field analysis is summarized as agent work; it cannot establish
review readiness. Technical fields use aggregate coverage counts, and possible special-category
data stays visible by collection. Full field citations, diagnostics and fingerprints are retained
for explicit evidence inspection.

### Working artifacts

The shared manifest/derived/parsed pattern is retained. Scan provenance and fingerprints live in
`privacy-datamap.derived.json` under `_scan`, with no separate scan metadata file. Privacy adds an
accepted lock and a reusable analysis cache for proposals, investigation coverage and dependencies.
The analysis cache retains candidate observations under `preview` when needed; it does not persist
candidate manifests, reconciliation results or generated review/evidence views.

Identical long investigation text is stored once in `shared_reasoning`; proposal `analysis_ref` and
`rationale_ref` fields identify those exact blocks. `analysis_storage.load` resolves them for the
validator and renderer; `analysis_storage.save` deduplicates and atomically writes compact JSON.
Agents should load and save through these helpers, editing inline reasoning in the loaded document.
Never read an entire large analysis cache into conversational context: select the relevant entity,
decision or reasoning group. Group shared reasoning before writing, keeping field-specific evidence.

Generate views only when needed:

```bash
python3 <plugin>/scripts/reconcile.py --repo=. --render-candidate
python3 <plugin>/scripts/review.py --repo=. --output=json
python3 <plugin>/scripts/review.py --repo=. --output=evidence
node <plugin>/scripts/collect.mjs --repo=. --export
```

Candidate YAML and full evidence JSON go to stdout. The review command writes the readable report;
the export command requires an accepted valid manifest and writes `.fides/datamap.yml`. The accepted
manifest and lock are committed; caches remain local. Deleting the analysis cache loses unaccepted
investigations and proposals; accepted decisions remain in the committed manifest and lock.

Readiness also checks purpose-specific activity categories, stored versus transient processing,
recipient assessment, and system investigation covering runtime processing, sharing and storage.
Disappeared field scope and systems require evidence-backed retirement, replacement or gap proposals.
The validator enforces recorded coverage; it cannot prove that an agent's explanation is correct or
that all integrations have been discovered. First scans establish the map and investigate material
ambiguities. Repeat scans use structural, dependency, discovery and semantic change queues plus
unresolved questions; reuse unaffected decisions and current cached reasoning. Source discovery
still runs across the repository so changed purposes or sharing can trigger review without ORM
changes. A broader manual audit requires an explicit user request. Stop at a supported conclusion
or a specific missing fact and report the question instead of extending the search indefinitely.
See the investigation scope in `commands/scan.md` for the agent stopping and reuse rules.

Semantic review compares proposals and manifest edits with the compact accepted meaning in the
existing lock. Personal-to-non-personal changes, collections lost through export filtering, and
narrowed activity categories, subjects, purpose keys or recipients require evidence-backed
explanations in the existing `removal_proposals` queue. Use `amended` for a justified reclassification
or scope correction, or an actionable `gap` while unresolved. Field changes are grouped by collection;
full differences remain in evidence. Rerun reconciliation after editing proposals to refresh the queue.
An activity rename is conservatively treated as removal of the prior activity until explained.
Ordering and citation changes do not count as narrowing. Seal reviewed amendments before export.
The manifest remains authoritative; the lock retains only compact comparison data, not another map.
A baseline without a semantic snapshot cannot establish past meaning; the next reviewed seal starts
semantic comparison. Do not claim historical semantic coverage where that baseline is absent.

Review readiness requires recorded infrastructure investigation for queued systems and structural
boundary reasoning for covered stores. The core workflow also requires checking classification use,
transient flows, identity continuity and separation of approval provenance from descriptions.
These are agent review obligations; the validator checks missing records, not whether an explanation
proves deployed behavior. Missing deployed evidence must remain visible.


### Canonical classification and fresh reassessment

After collection and reconciliation, run:

```bash
python3 scripts/field_review.py --repo=<repo>
python3 scripts/investigate.py --repo=<repo> --limit=24
```

The first command builds `classification_review` inside
`.noru/.cache/privacy-datamap.analysis.json`. The second prints bounded agent work and decision
templates on stdout. Investigate the cited facts and alternative writers, then submit a JSON list
with `investigate.py --repo=<repo> --decisions=<file>` (a temporary file outside the scanned repo,
or `--decisions=-` for stdin). Continue until the agent queue is complete.
These decisions remain proposals; they do not accept a manifest. Extraction gaps remain in the same
contract; evidence-backed resolutions use `investigate.py --repo=<repo> --gaps=<file>`. Each records
the gap identity, investigation, source evidence and rationale. A resolved coverage gap records the
supported scope or exclusion; it never claims that unsupported syntax was parsed.

Use `field_review.py --repo=<repo> --fresh` to ignore prior field classifications during inference.
Structural topology and evidenced supplemental definitions remain in scope. It uses the same
canonical model and gate, with no second report that can be deleted to bypass review. Preserve any
prior draft needed for comparison before explicitly requesting fresh mode.

The existing artifact layout is unchanged: manifest and lock in `.noru/`, analysis and review in
`.noru/.cache/`, accepted Fides export in `.fides/`. The analysis cache gains versioned evidence,
facts, recursive field decisions and engine/taxonomy/source bindings. No repository instruction,
classification script or special correction file is required. Existing accepted artifacts are not
rewritten by evidence extraction. Readiness, validation, sealing and export use the same mandatory
gate. The accepted lock gains a compact classification certificate. A clean checkout without the
ignored cache can validate its accepted map only when source, policy, structure and manifest bindings
all match that sealed certificate. A stale certificate requires investigation; a present fresh or
unfinished contract cannot be bypassed by the old certificate.

The TypeScript adapter uses a pinned bundled compiler with an offline, constrained host. It reads
configuration as data, resolves supported in-repository imports/types and bounded source lineage,
and never runs target code, configuration, builds or compiler plugins. Unsupported constructs,
open content and exhausted traversal budgets are coverage gaps. Other schema formats retain their
structural parsers and require agent investigation of unsupported semantic flows.

When compiler extraction leaves an opaque container, agent investigation can supply source-bound
shape evidence with `investigate.py --repo=<repo> --shapes=<file>`. Each entry identifies the current
field and observation digest, explains the inspected contract, cites current source lines, and
describes its type and nested members. This adds structural facts, never personal-data verdicts.
Existing observed members are retained; changed shapes require new field decisions. The normal
cache and accepted lock retain this evidence. Fresh reassessment discards prior shape judgments.

### Separate semantic evidence from acceptance

Field decisions submitted through the investigation API require six cited `semantic_review`
findings covering producers/consumers, identity and correlation, taxonomy uncertainty, security
and transformations, behavior and execution scope, and counterevidence or missing facts.
Maintenance timestamps enter investigation without an automatic non-personal classification.
A known natural-person association cannot be submitted as non-personal or unknown simply because
its category is unresolved. The review still preserves separately assessed nested members.
These gates check evidence-record completeness and consistency; they do not prove semantic truth.

## Portable CI checks and publication

Run `scripts/check_privacy.py --repo=<repository>` from this plugin to check the reviewed
baseline and export offline. `scripts/publish_datamap.py` provides opt-in REST publication
with provider-neutral source metadata. See [portable privacy CI](../../docs/privacy-ci.md)
for inputs, exit codes and the supported document format.

### Extraction diagnostic resolutions

Investigate raw findings through the canonical gap-decision API. Valid evidence-bound resolutions
are honored by review, validation, sealing, export and publication preflight; unresolved, partial,
conflicting or stale findings still block. Raw diagnostics and prior rationale remain auditable.
See [the investigation workflow](references/extraction-resolutions.md) for commands, migration
coverage adapters, supplemental definitions and cache compatibility.
