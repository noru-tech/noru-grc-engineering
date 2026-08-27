# Changelog

All notable changes to this project are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Every plugin in the marketplace shares
one version number; the release workflow fails if they disagree.

## Unreleased

### Added

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
- `review-signoff`'s validator takes `--as-of=YYYY-MM-DD`, which turns an already-expired sign-off
  into an error. Nothing in any validator reads the clock by itself, so this stays deterministic and
  the check is explicit where it belongs — in CI, or before a release.

### Changed

- `contract/README.md` — requirement 8 now spells out the two ways a claim may satisfy the expiry
  rule, after building a piece at each end of it.
- `scripts/test_idempotency.py` enumerates pieces from disk and **fails** when a piece has no
  idempotency test registered, instead of quietly covering a subset while reporting "every piece".
  Its summary line now names the pieces it actually exercised.
- `scripts/check_repo.py` — vocabulary/schema sync entries for both new pieces.


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
- The live-organization verification steps in [docs/verification.md](./docs/verification.md) have
  not been run.
