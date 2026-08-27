# Changelog

All notable changes to this project are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Every plugin in the marketplace shares
one version number; the release workflow fails if they disagree.

## Unreleased

### Fixed

- **The collector's own version is no longer part of the derived digest** (#17). `generated_by` was
  inside the object every `digestOf()` hashes, so bumping a plugin changed the digest of every
  repository that had already run `:scan` — a plugin upgrade was indistinguishable from a schema
  change, reporting drift where nothing drifted and failing CI mode with exit `3` on upgrade day.
  It stays in the derived facts and in the manifest as provenance; it is simply not hashed. The
  digest answers one question — has the repository changed? — and the version of the tool that read
  it is not a fact about the repository.

  **Breaking, once.** Every digest changes with this release, because the hashed input changed.
  On upgrading, each piece will report drift on a manifest that was accurate a moment earlier.
  Re-run `:scan`, confirm nothing else moved, and commit the new `derived_digest`. This is the last
  time a version bump will do that, which is the point of the change.

  Asserted for all seven collectors rather than the one it was found in, since they carry a
  byte-identical `digestOf()` and fixing one is not fixing the property. The test also checks that
  an absent `generated_by` hashes the same as a present one, so the exclusion cannot be
  inconsistent.

  This also removes the reason the `0.2.0` release pinned every collector's `VERSION` constant at
  `0.1.0` while the distribution moved to `0.2.0`. Bumping it is now free, and can happen whenever
  the next release does.

## 0.2.0 — 2026-08-27

The release that adds `audit-pack`, `iac-scan`, `governance-records`, `review-signoff` and
`privacy-datamap`, and CI mode. The first release with a tag: `0.1.0` was published to the
marketplace but never tagged, which is why `noru-ci@v0.2.0` in the README and in
[`docs/ci-mode.md`](./docs/ci-mode.md) resolves for the first time here.

### A note on versions

The plugin manifests, the marketplace entries and the skills move to `0.2.0`. The `VERSION`
constant inside each collector deliberately **stays at `0.1.0`**.

Those are two different things. The first is what you install. The second is stamped into every
manifest as `generated_by: <piece>@<version>` and is part of the derived digest, so bumping it would
change the digest of every repository that has already run `:scan` — making a plugin upgrade
indistinguishable from a schema change, reporting drift where nothing drifted, and failing CI mode
with exit `3` for every user on upgrade day. That the two are entangled at all is a design problem
rather than a policy, and it is tracked in #17.

### Added

- `privacy-datamap` carries over `references/classification-guide.md` from
  `noru-tech/privacy-taxonomy` — the last thing in that repository the new piece still needed. The
  skill and the `:scan` command had been pointing at it since it was written; the file was not
  there. Every one of the 68 taxonomy keys it recommends was checked against the vendored snapshot
  before it landed, since a guide that suggests a key the validator rejects is worse than no guide.

  Rewritten for where it now sits: `classification.json` holds the names that mean the same thing in
  every schema and the collector applies it mechanically, so the guide is only for the judgement
  half — the names that need context (`name`, `id`, `role`, `title`, `notes`), the `data_use` and
  `data_subjects` the collector cannot know, and where Article 9 data arrives without looking like
  it (a free-text `reason` on a leave request, a `tags` array on a membership row).
- `scripts/check_repo.py` fails when a piece's prose points at a `references/` file that does not
  exist. The skill and the commands are instructions an agent follows literally: a missing guide
  sends it looking, and the likeliest outcome is that it classifies without the guidance and never
  mentions it could not find the file. This is how the gap above went unnoticed through three
  changes.
- `privacy-datamap` renders `.fides/datamap.yml`, the deliverable declared in its `outputs[]`, and
  gets its skill and commands. The export is gated the way `audit-pack` gates its bundle: only ever
  written from a manifest whose `derived_digest` matches the repository as it stands right now. A
  Fides file that looks authoritative and describes a schema that has moved on is worse than no
  file, because nobody re-reads one that already exists.

  The projection to plain Fideslang now lives in one place, `scripts/lib/fides.mjs`, used by both
  the renderer and the plan builder — so the export and the payload `ingestDatamap` receives are the
  same content by construction rather than by discipline, and a test asserts it. Built separately
  they would drift, and the failure would be silent: a data map handed to an auditor saying
  something different from the one in the compliance record.

  **Fixed: `structure_digest` was reaching Noru.** Adding it to the manifest in the previous change
  put it on the wire too, because the leak check was a denylist of the three review fields that
  existed when it was written. It is now an allowlist taken from Fideslang's own resource model — an
  external vocabulary this piece cannot quietly grow — so the next review field is caught by what
  the wire looks like rather than by somebody remembering two places. Both directions are
  mutation-tested.
- `privacy-datamap` puts the **fourth expiry anchor** to work — the one added to
  `contract/README.md` in this same release and documented there as unused. Every collection now
  carries a `structure_digest`: a hash of its field *names*, not their categories, so resolving a
  classification keeps the signature and adding, removing or renaming a column breaks it. The
  validator recomputes it rather than trusting the stamp, so editing the fields and editing the
  digest by hand fail the same check.

  That anchor settles a question the other pieces each had to answer differently. `expires_at` is
  now required and measured from `decided_at` — the plain reading, "how long since a person last
  looked" — which is honest here and is not elsewhere: the objection to `decided_at` is that a late
  signature extends what a claim covers, and here it cannot, because the coverage is pinned by
  digest rather than by date. A signature cannot outlive the structure it was given for. Ordinary
  data may stand for 365 days, GDPR Article 9 and Article 10 data for 183.

  `--as-of=YYYY-MM-DD` turns an already-expired claim into an error, matching `review-signoff` and
  `iac-scan`. Nothing reads the clock by itself, so the validator stays deterministic.

  Five new invalid fixtures, and a cross-language test: the digest has a JavaScript implementation
  in the collector and a Python one in the validator, which is exactly the pair that silently
  diverges. If they ever disagree, every collection reads as "changed shape since it was signed"
  for ever and re-running `:scan` does not help — so the agreement is asserted directly, and
  mutation-tested by changing one side's separator.
- `privacy-datamap` — the collector, the manifest shape and the validator. It reads SQL DDL, Prisma,
  Django/SQLAlchemy models, protobuf and GraphQL SDL into datasets, collections and fields, each
  carrying the `file:line` that produced it. The README lists what it does *not* read yet
  (OpenAPI, TypeORM, Mongoose, ActiveRecord, Ecto, GORM, TS/Zod DTOs), because an empty data map
  from an unsupported format looks exactly like a repository with no personal data in it.

  **Structure is derived, meaning is judged.** That a column named `email` exists at
  `db/schema.sql:12` is a parse; what it means is a lookup or a judgement. The collector classifies
  only by exact match against a bundled table of 107 rules whose every value was checked against
  the vendored taxonomy, and raises everything else as `needs_review: true` rather than inferring.
  That is what lets a collector whose job is classification be deterministic, and a manifest
  carrying an unresolved flag cannot be pushed — a confidently wrong data category is worse than a
  gap, because the gap gets reviewed and the wrong answer gets signed.

  The claim unit is the **collection**: one interpretation block per table rather than per column,
  because five hundred blocks on a five-hundred-column schema is a form nobody fills in. Special
  category data (GDPR Article 9, and Article 10 criminal-offence data) is collected into its own
  list so the highest-risk thing in the map is never something a reviewer has to go looking for.

  The validator checks every `data_categories`, `data_use` and `data_subjects` value against the
  vendored Fideslang snapshot with did-you-mean hints, requires a citation and an interpretation on
  every claim, and rejects a `dataset_references` entry naming a dataset the manifest does not
  define — a dangling edge in the map, which would otherwise render as a system reading from a
  store nothing describes.

  Nineteen collector assertions, each mutation-tested: making the classifier guess, shifting a
  citation by one line, letting the collector overwrite a reviewed manifest, and dropping the
  special-category flag are all caught.
- `privacy-datamap` — the piece skeleton, its declaration and its push. Supersedes
  [`noru-tech/privacy-taxonomy`](https://github.com/noru-tech/privacy-taxonomy), whose skill name it
  keeps, so `/privacy-datamap` and "generate a Fides data map for this repo" go on working; the
  plugin name and the install path change. **It collects nothing yet** — the collector and the
  manifest schema land next, and the valid fixture still carries the scaffold's generic shape.

  What is real is the push, and it is the first one in this repository that requirement 4 describes
  literally: `mode: single_call`, one operation, `ingestDatamap`, carrying `slug` + `commitSha` +
  `branch`. Nothing to fan out over — the tool takes the whole data map for a source, so a
  repository with four hundred fields is one write.

  Its idempotency is declared `server_dedupe`, not `server_upsert`, and the gap is written down.
  The published tool description says "identical content is a no-op", which covers re-running CI on
  an unchanged repository. It does not say what happens when the same slug is pushed with *changed*
  content, which is the ordinary case, and `llms.txt` documents no datamap endpoint and no
  idempotency rule for one. So `:diff` reads `getPrivacyDataMap` and `listPrivacyDatasets` first and
  reports the change set rather than asserting what the server will do with it.

  The piece's own bookkeeping — `refs`, `interpretation`, `needs_review` — is stripped before
  anything is sent: it is how a human reviews the manifest in a pull request, and a Fides manifest
  carrying it is one no other Fides tool can read. The payload is hashed over a key-sorted
  canonical form, because a JSON object is not guaranteed to come back with its key order intact and
  hashing the unsorted form would make every second run look like a change.

  Its idempotency test asserts the single call, the argument names taken from the published tool
  schema, the provenance, and that a second run is a no-op. Two further assertions — that no
  bookkeeping reaches the wire, and that key order alone is not a change — **report themselves
  skipped**, because the fixture has no collections yet and they would otherwise pass whether or not
  the code did anything. They activate on their own once the schema and fixture carry real content.
- `audit-pack` — `:scan` / `:diff` / `:push`. Assembles what an auditor asks for, for one framework
  over one window: the controls in scope with what is expected of each and what is actually linked,
  the local artifacts an integration cannot reach, the other pieces' committed manifests, and a
  workpaper per control. Draws a **reproducible** sample seeded from the population file's own
  digest, so anyone holding that file can redraw it; enforces a floor on sample size for the
  population it came from. The rendered pack under `.noru/audit-pack/` is a local deliverable and is
  only ever built from a manifest that validated against the same repository state; what lands in
  Noru is the tested conclusion for each control, one workpaper to one record to one control.
- `iac-scan` — `:scan` / `:diff` / `:push`. Reads Terraform, CloudFormation, Kubernetes and
  pipeline configuration and proposes a security finding for each bundled rule that fires, keyed on
  the rule and the resource rather than the line so moving a block is not a new problem. Lands
  through the documented idempotent upsert on `(source, externalId)` — the first piece with no
  client-side probe anywhere — and **closes** the findings whose rules no longer fire with the same
  call, scoped to the repository's own slug so two repositories under one source cannot close each
  other's work. Records a citation and never a copy: one rule fires on lines that hold credentials,
  so no matched text reaches the manifest.
- `contract/audit-pack.schema.json`, `contract/iac-scan.schema.json`.
- `getOrganizationRisks` and `getSecurityFindings` added to the published-tool list in
  `contract/piece.schema.json`, checked against the tool list the MCP server publishes.
- `outputs[]` in `contract/piece.schema.json` — the optional declaration of what a piece renders
  for a human *besides* the manifest: the path, what it is for, why it is not the manifest, and the
  assertion that it is only ever rendered from a manifest that validated against the repository
  state it describes. `audit-pack` declares its bundle under `.noru/audit-pack/` there. This closes
  the gap `contract/README.md` had already written down under "what `:push` means for a piece that
  assembles" — a produced artifact was undeclared, so nothing could check it. `check_item_2` now
  requires every declared path to appear in the piece README and to not be the manifest itself; a
  deliverable that is written into someone else's repository at run time cannot be opened from
  here, but the declaration drifting away from the documentation can be caught, and that is the
  failure that actually happens.
- `getPrivacyDataMap`, `listPrivacyDatasets` and `ingestDatamap` added to the published-tool list in
  `contract/piece.schema.json`, each confirmed against the tool list the MCP server publishes rather
  than from memory. `getPrivacyTaxonomy` was already listed.
- A fourth expiry anchor in `contract/README.md`, documented and not yet used: a claim *about a
  structure* the collector already digests may carry that digest beside its dates and lapse when
  the structure changes, not only when the calendar says so. Stronger than a date, because signing
  again renews a date and cannot renew a digest.
- `contract/README.md` now says where the line between a bundled **vocabulary** (required by
  requirement 3) and a bundled **catalogue** (forbidden by requirement 9) falls, because
  `ai-inventory` ships 85 vendored Fideslang categories and the distinction was folklore. The test
  is whether the file says what someone must do. Where Noru publishes the same vocabulary, the
  bundled copy is the offline floor and Noru is the truth.
- `governance-records` — `:scan` / `:diff` / `:push`. Reads the governance documents a repository
  already holds (minutes, ISMS scope, statement of applicability, internal audit plan, report and
  checklist, finding records, corrective action plans), extracts who was present, what was decided
  and what was assigned to whom with a `file:line` citation for every fact, and files each record as
  attributed evidence over MCP against the expectations Noru says are unmet. Writes no policy text:
  Noru already owns the authoring half of governance.
- `review-signoff` — `:scan` / `:diff` / `:push`. The recurring "a human attests to machine output"
  pattern — access reviews, rule reviews, hardening baselines, asset reconciliation, physical access,
  vendor security reviews. Hashes the export that was reviewed, reconciles confirmed against
  exceptions, and lands a named, dated sign-off whose expiry is set on the evidence record itself.
  Its queue has two halves: expectations with nothing linked, and sign-offs Noru reports as expiring
  or expired.
- `contract/governance-records.schema.json`, `contract/review-signoff.schema.json`.
- **CI mode.** `scripts/ci_check.py` runs any piece headless — `scan → validate → expiry`, and
  optionally `diff → push` — driven entirely by that piece's `piece.json`, so a piece scaffolded
  tomorrow works with no change to the orchestrator. Two conditions fail a build and **both are
  computed from the repository and a calendar, with no network and no credential**, so the checks
  work on a pull request from a fork: the committed manifest no longer matching the repository
  (exit `3`, with a readable account of what the collector found that the manifest names nowhere),
  and an interpretation whose expiry has passed or that is outside the review cadence the pipeline
  declared (exit `4`). Documented exit codes distinguish each condition from a tooling failure, and
  `--mode=warn` reports the identical findings without failing, so a team can adopt it before
  gating on it. `--output=json --quiet` throughout; no TTY.
- `scripts/check_expiry.py` — the expiry half on its own, over a manifest or a validated document.
  Reports `expired`, `cadence`, `expiring`, `unbounded` and `unparsable`, and compares a
  record-level expiry (`expiry_date`) the same way it compares `interpretation.expires_at`.
- `.github/actions/noru-ci` — the published GitHub Action wrapping it, with a job summary, outputs
  for `status` / `exit-code` / `drift` / `expired`, and `require-yaml-loader` so a runner image that
  changes which YAML parser is importable fails the build instead of quietly switching parser under
  a compliance gate. It installs nothing. [`docs/ci-mode.md`](./docs/ci-mode.md) documents the exit
  codes, warn-only adoption, the opt-in push job, and the GitLab and plain-shell recipes for anyone
  not on GitHub.
- `scripts/test_ci_mode.py` — constructs both failure conditions in a throwaway repository and
  asserts each one really fails with its own exit code and message, that they are distinguishable
  when both fire, that warn-only reports the same findings and exits `0`, that a check which could
  not run is never reported as a pass, and that `NORU_API_KEY` never reaches a step that does not
  push. Runs in CI under both YAML loaders and against a freshly scaffolded piece.
- The `ci` workflow dogfoods the action on this repository: it gates a repository whose manifest is
  true, then asserts the drift gate fails with exit `3`, warn-only mode does not, and an expired
  interpretation fails with exit `4`.
- `review-signoff`'s validator takes `--as-of=YYYY-MM-DD`, which turns an already-expired sign-off
  into an error. Nothing in any validator reads the clock by itself, so this stays deterministic and
  the check is explicit where it belongs — in CI, or before a release.
- `ai-inventory` now detects **EU AI Act Article 50 triggers and whether the disclosure or content
  marking each one requires is actually present in the code**. Detecting that a system qualifies was
  never the useful half. The check reports `present` (the notice or mark is emitted from the same
  file as the model call), `unclear` (something disclosure-shaped exists but nothing ties it to this
  surface) or `absent`, and an `absent` finding must record `searched` — where the check looked —
  because a notice rendered by a design system, a CMS, a mobile client or another repository is
  invisible to a repository scan.
- `ai-inventory` screens for seven of the eight Article 5(1) prohibited practices and reports which
  practices it screened even when it finds nothing, so that "the screen ran and found nothing" is
  distinguishable from silence. The collector never writes `determination: indicated`: a pattern
  match proposes `needs_legal_review` with `needs_review: true`, and only a person decides.
- `scripts/test_collectors.py` — a new gate asserting what the collectors actually detect, rather
  than only that they are deterministic, offline and contract-clean. It runs the real collector over
  purpose-built repositories and pins each disclosure state, the marking-is-not-a-label rule, the
  emotion-recognition-is-biometric rule, and the collector's refusal to assert. Wired into `ci` and
  `release`.

### Changed

- The Fideslang snapshot is now **canonical in one place**, `contract/lib/taxonomy/`, alongside
  `yaml_mini.py` and for the same reason. `ai-inventory` keeps its verbatim copy of
  `data_categories.json` — an installed plugin cannot read a file from outside its own directory —
  and `scripts/check_vendored_lib.py` grew a third arm that fails the build if a copy drifts,
  is missing, or is declared with no canonical to copy from. `--fix` re-copies it.

  The arm is driven by each piece's own `validator.vocabulary` list rather than by a table kept in
  the checker, so declaring the file is what opts a piece into the check and a piece vendors only
  the vocabulary its validator actually loads. `ai-inventory` loads data categories and nothing
  else, so that is all it carries.

  `data_uses.json` (56 entries) and `data_subjects.json` (15) join the canonical directory although
  no piece loads them yet. The refresh recipe regenerates all three from one upstream revision in
  one pass, and a snapshot that holds only what today's pieces happen to need is a snapshot that
  needs a second procedure the moment one of them needs more. The `Used by` column in
  `contract/lib/taxonomy/SOURCE.md` says which are live.

  Two refresh recipes existed for this same pinned upstream commit, one here and one in
  `noru-tech/privacy-taxonomy`, and they had already diverged in wording. There is one now.
- `contract/README.md` now records what `:push` means for a piece that **assembles** rather than
  collects — the artifact stays local and the judgements inside it land — along with two gaps the
  first such piece exposed: a read-only piece cannot satisfy a mandatory `push`, and a piece that
  produces an output for a human has nowhere to declare it. It also records that expiry now has
  three different **anchors** in use (a declared cadence, the day the world was observed, and the end
  of the period a conclusion covers), and that a new piece should say which anchor it uses before it
  says how long the window is.
- `scripts/check_repo.py`, `scripts/test_collectors.py` and `scripts/test_idempotency.py` cover
  both new pieces. The collector tests assert the two claims each piece rests on: that `iac-scan`
  never writes a matched line anywhere, and that following `audit-pack`'s written redraw recipe
  reproduces its sample exactly.
- `tests/fixture-repo/` gained infrastructure and pipeline configuration, a change-ticket population
  to sample, and a queue snapshot for each new piece.
- `contract/README.md` — requirement 8 now spells out the two ways a claim may satisfy the expiry
  rule, after building a piece at each end of it.
- `scripts/test_idempotency.py` enumerates pieces from disk and **fails** when a piece has no
  idempotency test registered, instead of quietly covering a subset while reporting "every piece".
  Its summary line now names the pieces it actually exercised.
- `scripts/check_repo.py` — vocabulary/schema sync entries for both new pieces, and a `keys`
  comparison mode that pins the *declaration order* of the `ai-inventory` finding categories against
  the vocabulary, because that order is meaning rather than formatting.

### Changed — `ai-inventory` findings (breaking to the manifest format)

- **`classifications[]` is replaced by `findings`**, an ordered block of four distinct categories:
  `prohibited_practices`, `transparency_obligations`, `role_and_risk`, `standards_alignment`. A
  manifest that writes them in another order is a validator error. The order is the substance: the
  first two are already enforceable, the third serves a later date, and a reader acts on what they
  read first. A `.noru/ai-inventory.yml` written before this change will not validate; re-run
  `:scan` against the new schema.
- Article 5 findings carry the practice, its point of Article 5(1), a determination, and an `action`
  — required unless the determination is `no_indication`, because a prohibition that produces a row
  in a table and no instruction has failed at the only thing it was for. Article 5 findings and
  missing Article 50 disclosures are lifted above everything else in `:scan`, `:diff` and validator
  output, and carried in `--output=json` under `alerts` so CI can fail on them.
- `role_and_risk` requires `enforceable_from`, the date the obligations that follow from the tier
  start to apply, so a finding serving a future deadline cannot be presented as one due today. It
  also carries the article driving the role and the article driving the tier, an Annex III screen,
  and the Article 6(3) assessment where the conclusion is not-high-risk — including the profiling
  answer, which the validator gates on: Article 6(3) does not permit that conclusion for a system
  that performs profiling of natural persons.
- The tier vocabulary drops `limited_risk` and `minimal_risk`. They are commentary shorthand rather
  than terms of the Regulation, and the transparency duties they usually stand for now have their
  own category. `prohibited` is gone from the tier list for the same reason.

### Fixed

- **The bundled YAML loader no longer truncates prose containing a `#`.** It stripped comments line
  by line before assembling a block scalar, so a `#` preceded by a space ended the line even inside
  a `>` or `|` block: a rationale reading `tracked in ticket #4412 until the rollout completes`
  loaded as `tracked in ticket rollout completes` on any machine without PyYAML, and intact on one
  with it. Nothing downstream could detect it — the manifest still validated and the sentence still
  read as a complete one. Issue numbers, `C#` and a `#` in a URL fragment all hit it.

  Block scalars are now resolved while the document is still raw text, where their extent is
  knowable, so their content escapes comment stripping entirely. The rest of the construct came with
  it: indentation is detected from the first content line, folding respects blank and more-indented
  lines, and the chomping (`-`, `+`) and explicit-indentation indicators are honoured in either
  order. Checked against PyYAML 6.0.3 over 660 block-scalar documents and every manifest and fixture
  in the repository.

- **A piece's identity no longer depends on which YAML loader parsed its manifest.** The two
  loaders a validator may use — PyYAML where it is importable, the bundled fallback otherwise —
  disagreed on the whitespace inside a folded (`>`) block scalar, so the same manifest produced two
  different plans depending on the machine: push from a laptop without PyYAML, push again from CI
  with it, and the second push filed a duplicate instead of skipping.

  This was first fixed in each piece, by normalising every manifest-sourced free-text field before
  it reached a rendered body, a content digest or a planned argument. It is now fixed in the loader
  instead, and the normalisation is gone: the bundled fallback reads a block scalar exactly as
  PyYAML does, so there is nothing left for the pieces to defend themselves against. The pieces
  render manifest prose as written again — a `|` block keeps its line breaks in an evidence body
  rather than being flattened to one line.

  `scripts/test_idempotency.py::test_loader_independence` no longer simulates the difference by
  re-spacing prose. It parses the same manifest bytes with both loaders — in one interpreter, the
  fallback forced by hiding PyYAML behind a stub that raises on import — and compares the resulting
  plans for every declared piece, against an empty organization and against one the plan has already
  been pushed into. Having two loaders to compare means having PyYAML, so it reports itself skipped
  where there is none rather than passing while checking nothing; the CI matrix runs a leg with it.
  `scripts/test_validators.py` pins the fallback loader's output to PyYAML's on every machine, and
  re-checks that pin against PyYAML itself wherever it is importable.

  **Migration — nothing moves for anything pushed from a machine that had PyYAML**, which includes
  every push from CI. Each piece's whole plan is byte-identical to what the released code produced
  under PyYAML. What does move is the opposite case: for an organization only ever pushed from a
  machine *without* PyYAML, the markers of `ai-inventory`, `audit-pack`, `governance-records` and
  `review-signoff` records change, because the prose they digest is now the complete text rather
  than the truncated, re-spaced text that loader used to return. The next push files a **new**
  evidence record beside the old one, which then has to be retired by hand; `:diff` names it before
  it happens, with a reason reading "covers this system but the content changed". `evidence-push` is
  unaffected — its marker is the artifact's own digest, never prose. Of the four, only
  `ai-inventory` has been released.

- The `ai-inventory` documentation no longer implies that the EU AI Act requires an organization to
  keep an AI register. It does not. Articles 49 and 71 are registration into a public Commission
  database by providers of Annex III high-risk systems, and by deployers only where they are public
  authorities or EU bodies; a private-sector deployer has no registration duty. The piece README now
  states what the Regulation does ask for and why an inventory is still worth keeping.


## 0.1.0

First public release.

### The contract

- `contract/piece.schema.json` — the declaration every piece ships at `plugins/<piece>/piece.json`,
  covering all nine requirements.
- `contract/README.md` — the requirements, how each is enforced, and the non-goals.
- `contract/ai-inventory.schema.json`, `contract/evidence-push.schema.json` — the manifest schemas.
- The shared **interpretation block** (`owner` / `decided_at` / `expires_at` / `rationale` /
  `refs[]`). An unattributed claim is a validator error, not a warning.
- Three idempotency kinds — `server_upsert`, `server_dedupe`, `client_probe` — where the last is
  recorded as a gap with the server-side change that would close it, rather than presented as a
  design.

### Plugins

- `noru` — hub: `connect`, `doctor`, `context`, and the canonical plan/diff helpers every piece
  vendors.
- `ai-inventory` — `:scan` / `:diff` / `:push`. Deterministic offline discovery of model and
  provider SDK usage, model ids, retrieval sources, eval suites and human-oversight points; lands in
  Noru as assets, vendors and evidence over MCP. EU AI Act, ISO 42001 and NIST AI RMF classification
  is emitted as *suggestions with citations*, never as assertions.
- `evidence-push` — `:scan` / `:diff` / `:push`. Works Noru's own evidence queue
  (`getOrganizationControls` + `getControlContext`), matches local artifacts against it, and uploads
  over `POST /v1/evidence/upload` with `controlMappings`. REST rather than MCP because file upload
  is a deliberate omission from the MCP surface.

### Tooling

- `scripts/scaffold-piece.mjs` — stamps a piece that passes the contract test unmodified.
- `scripts/contract_test.py` — executes requirements 1–9 against every plugin.
- `scripts/test_idempotency.py` — drives every piece end to end and asserts a second push is a no-op.
- `scripts/test_validators.py`, `scripts/check_repo.py`, `scripts/check_vendored_lib.py`,
  `scripts/jsonschema_mini.py`.
- Two CI workflows: `ci` (syntax, hygiene, tests across Node 18/20/22 and Python 3.9/3.12/3.13, and
  a scaffolded-piece smoke test) and `release` (version consistency across every manifest).

### Known gaps

- Noru's published API documents no idempotency key for evidence creation or file upload, so both
  pieces fall back to a client-side content-marker probe rather than assuming one. Recorded in each
  `piece.json`.
- Both pieces are `mode: keyed_upsert` rather than a single ingest call, because the published API
  offers no single ingest operation for these artifacts. Both declare `collapses_to`.
- These pieces are reviewed and internally consistent, not field-tested against a live
  organization. See [Maturity](./docs/verification.md#maturity).
