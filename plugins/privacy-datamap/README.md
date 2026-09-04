# privacy-datamap

> Read the schemas a repository actually contains, classify the personal data in them against
> the Fideslang taxonomy, and land the data map in Noru — with a citation for every field and a
> named owner for every judgement.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/privacy-datamap:scan` | no | Reads the repository's schemas → `.noru/privacy-datamap.yml` — and, once that manifest validates, renders `.fides/datamap.yml` |
| `/privacy-datamap:diff` | no | Reads current state, prints the exact plan |
| `/privacy-datamap:push` | **yes** | Executes the confirmed plan |

`scan` now has two deterministic local phases. `collect.mjs` observes the repository; then
`reconcile.py` compares those observations with the last accepted lock. The reconciler emits the
small set of new or materially changed ambiguous fields that need agent analysis. It never calls a
model itself, and unchanged fields are never reclassified.

## What it reads

| Format | Files | What it takes |
|---|---|---|
| SQL DDL | `*.sql` (schemas, migrations) | `CREATE TABLE` → collection, each column → field |
| Prisma | `*.prisma` | `model` → collection, each field |
| Python ORM | `*.py` | Django `models.Model` and SQLAlchemy declarative classes; an attribute assigned from `Column(...)`, `mapped_column(...)` or a `*Field(...)` call |
| Protobuf | `*.proto` | `message` → collection, each numbered field |
| GraphQL SDL | `*.graphql`, `*.gql`, `*.graphqls` | `type` and `input` → collection, each field |

**Not read yet**: OpenAPI and JSON Schema, TypeORM and Sequelize entities, Drizzle tables, Mongoose
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

The reconciler writes `.noru/.cache/privacy-datamap.reconciliation.json`,
`.noru/.cache/privacy-datamap.proposals.json` and
`.noru/.cache/privacy-datamap.candidate.yml`. They are working files and must not be committed. The
candidate never overwrites the accepted manifest. After the candidate has been resolved and
reviewed, `reconcile.py --seal` refuses to write the lock unless the manifest is valid and matches
the current observations.

A valid manifest from a release before locks existed enters migration mode. Its decisions are
carried forward and its first lock is seeded without sending every field back through an agent.

The claim unit is the **collection**, not the field. One person signs for "these are the categories
in this table"; per-field attribution would mean five hundred interpretation blocks on a
five-hundred-column schema, which is a form nobody fills in. Field-level uncertainty still shows,
as `needs_review` flags inside the collection that block the push.

Special-category data — GDPR Article 9, plus Article 10 criminal-offence data — is collected into
its own list so a reviewer never has to go looking for the highest-risk thing in the map.

## When a signature stops counting

Two things anchor a claim, and the pair is the point.

**`structure_digest` pins what a signature was given for.** Every collection carries a digest of its
field *names* — not their categories — so resolving a classification keeps the signature, and adding,
removing or renaming a column breaks it:

```
ERROR dataset[0].collections[0].structure_digest: does not match this collection's fields
      (stamped 4abfeae8e1be, computed 9c2f0a71d33e) — a column was added, removed or renamed
      since this was signed, so the signature is no longer a statement about this table.
      Re-run :scan, review what changed, and sign again
```

The validator recomputes it rather than trusting the stamp, so editing the fields and editing the
digest by hand are caught by the same check.

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
