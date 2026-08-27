# evidence-push

> Ask Noru which evidence expectations are unmet, match local artifacts against that queue, and
> upload them as file evidence with control mappings.

The pen test PDF, the signed access review, the insurance certificate, the UPS test record, the CCTV
still. A great deal of what an auditor asks for is a local artifact a human has to hand over, and no
integration can fetch it from anywhere.

## The queue is Noru's

This plugin ships **no** list of what a control needs, and the contract test fails the build if one
appears. Every scan asks:

1. `getOrganizationControls` — the organization's controls
2. `getControlContext` — `predefinedEvidenceItems` (the framework's expectation),
   `linkedEvidenceItems` with `qualifiesRequirement` (what is already satisfied), `coverage`, and
   `guidance.testing` (how an auditor tests it)
3. the **unmet set is the difference**
4. `getEvidenceItems` resolves catalogue titles and types

Licensing says the same thing the drift argument does: call the API, do not vendor the catalogue.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/evidence-push:scan` | no | Fetches the queue, matches `.noru/artifacts/` against it → `.noru/evidence-push.yml` |
| `/evidence-push:diff` | no | Probes existing evidence, prints the exact upload plan |
| `/evidence-push:push` | **yes** | Uploads over REST |

## Scopes

| Capability | Scopes |
|---|---|
| `:scan` | `read:organization`, `read:controls`, `read:evidence` |
| `:diff` | the same |
| `:push` | adds `write:evidence` — **and a REST bearer key**, see below |

## Why this piece is REST, not MCP

File upload is a deliberate omission from Noru's MCP surface: tool arguments are JSON and cannot
carry a multipart body. So `:push` calls `POST /v1/evidence/upload` directly and needs
`NORU_API_KEY` with `write:evidence`:

```bash
export NORU_API_KEY="…"    # your shell, for this command only
```

The script reads it from the environment at the point of use and never writes it to a file, a plan
or a log line. Never paste a key into an assistant conversation; if you do, rotate it. Set
`NORU_API_URL` to point at a non-production host.

## Artifact

`.noru/evidence-push.yml`, schema at [`contract/evidence-push.schema.json`](../../contract/evidence-push.schema.json).

```yaml
version: 0.1.0
piece: evidence-push
source: { slug, commit_sha, branch, generated_by, derived_digest }
queue_snapshot:
  fetched_at: 2026-08-27T09:14:00Z
  via: [getOrganizationControls, getControlContext]
  controls:
    - control_id: …          # lowercase canonical id, not the uppercase display id
      unmet_evidence_items: [{ id, title, type }]
uploads:
  - file: .noru/artifacts/q2-access-review.pdf
    sha256: …
    size_bytes: …
    mime_type: application/pdf
    title: …
    expiry_date: 2026-12-31T23:59:59Z
    control_mappings: [{ control_id, evidence_item_ids }]
    interpretation: { owner, decided_at, expires_at, rationale }
```

The validator rejects any mapping to a control or item that is not in `queue_snapshot`. You cannot
satisfy an expectation Noru did not say you had.

## Upload limits, checked locally

- **50MB** maximum per file
- accepted types: PDF, Word, Excel, PowerPoint, `text/plain`, `text/csv`, `application/json`,
  JPEG, PNG, GIF, WebP, ZIP

Both are taken from the published **Evidence Upload Guidance** in Noru's API documentation
(<https://api.noru.tech/llms.txt>) and live in
[`references/vocabulary.json`](./references/vocabulary.json). Checking locally means a bad file fails
in the validator, not halfway through an upload batch.

`controlMappings` is always sent; the legacy `controlIds` field never is.

## Idempotency, honestly

Noru's published API documentation documents upsert behaviour for assets and security findings; it
documents no idempotency key for `POST /v1/evidence/upload`. This piece does not assume one. It
embeds the artifact's sha256 in the evidence description and probes `getOrganizationEvidence` before
uploading, so a second run has something of its own to recognise.

Two consequences worth saying out loud:

- edit that description in the Noru UI and the probe stops matching; a re-run will upload again
- a blind retry after a failure is how duplicates happen, so `push.mjs` never retries by itself

What the claim was checked against, and what a documented key would let the piece drop, are recorded
in [`piece.json`](./piece.json).

## Verify

```bash
node    plugins/evidence-push/scripts/collect.mjs --repo=. --output=json
python3 plugins/evidence-push/scripts/validate_manifest.py .noru/evidence-push.yml
node    plugins/evidence-push/scripts/diff.mjs --repo=.
node    plugins/evidence-push/scripts/push.mjs --repo=. --confirm --dry-run
node    plugins/evidence-push/scripts/push.mjs --repo=. --confirm
```

Exit codes: `0` success · `1` drift, validation failure, missing prerequisite, missing credential,
or a failed upload · `2` usage, including a push without `--confirm`.
