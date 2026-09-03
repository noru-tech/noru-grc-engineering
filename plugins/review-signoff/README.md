# review-signoff

> A machine produced a list. A named person went through it, said what they found, said what they
> did about the exceptions, and signed for a fixed period. That signature is the evidence.

Access review reconciliation. Periodic firewall rule review. Hardening baseline checks. Asset
reconciliation. Physical access review. Vendor security review. Different subjects, one shape: an
export, a human, a decision, and an expiry date.

The export is machine output — an integration can produce it. The attestation cannot be produced by
anything but a person, and it is the attestation an auditor asks for.

## Why this piece is where contract requirement 8 earns its keep

Everywhere else in this toolkit the `interpretation` block is attribution *on* a claim. Here it **is**
the claim, so the validator treats it as the deliverable rather than as metadata:

| Rule | Why |
|---|---|
| `expires_at` is **required**, not a warning | A periodic review with no end date stops being periodic the moment it is filed |
| `expires_at` must fall in the window `cadence` implies | A quarterly review signed off for two years is not a quarterly review, and nothing else in the file says so |
| `decided_at` cannot precede `performed_on` | You cannot sign off a review before you did it |
| `confirmed + exceptions` must reconcile with `records_reviewed` | A sign-off that does not account for what it covered is a signature on an unread page |
| Every exception needs a disposition and a named owner | An exception nobody owns will still be there next quarter |

The expiry reaches Noru itself, not just the text of the record: `:push` sets the evidence record's
`expiresAt` from the sign-off's own date, so the register goes stale on the day the human said it
would.

## Expiry checking, without a clock

The validator never reads the clock — that is what keeps it deterministic, and what keeps fixtures
from rotting. Pass `--as-of=YYYY-MM-DD` and an already-expired sign-off becomes an error:

```bash
python3 plugins/review-signoff/scripts/validate_manifest.py .noru/review-signoff.yml \
  --as-of="$(date -u +%F)"
```

That is the check worth running in CI or before a release: not "did the manifest change", but
"has anyone stood behind this claim recently".

## The queue is Noru's, and it has two halves

This plugin ships **no** list of what a control needs, and the contract test fails the build if one
appears. Every scan asks:

1. `getOrganizationControls` — the organization's controls
2. `getControlContext` — what the framework expects of a control, what is already linked, coverage,
   and the procedure an auditor follows → the unmet set is the difference
3. `getEvidenceForControl` — every linked record with its owner, status, type and **expiry**. The
   expired and nearly-expired ones are the other half of the queue: a review that went stale is due
   again, and only Noru knows when
4. `getEvidenceItems` resolves catalogue titles and types

Licensing says the same thing the drift argument does: call the API, do not vendor the catalogue.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/review-signoff:scan` | no | Fetches the queue, hashes and counts the exports in `reviews/` → `.noru/review-signoff.yml` |
| `/review-signoff:diff` | no | Probes existing evidence and its expiry, prints the exact plan |
| `/review-signoff:push` | **yes** | Emits the confirmed MCP calls for the client to execute |

## Scopes

Least privilege. Start read-only.

| Capability | Scopes |
|---|---|
| `:scan` | `read:organization`, `read:controls`, `read:evidence` |
| `:diff` | the same |
| `:push` | adds `write:evidence` |

Authentication is the MCP client's job. This piece never reads, writes or logs a credential.

## Artifact

`.noru/review-signoff.yml`, schema at
[`contract/review-signoff.schema.json`](../../contract/review-signoff.schema.json).

```yaml
version: 0.1.0
piece: review-signoff
source: { slug, commit_sha, branch, generated_by, derived_digest }
queue_snapshot:
  fetched_at: 2026-08-27T09:14:00Z
  via: [getOrganizationControls, getControlContext, getEvidenceForControl]
  controls:
    - control_id: …          # lowercase canonical id, not the uppercase display id
      unmet_evidence_items: [{ id, title, type }]
      expiring_evidence: [{ evidence_id, title, status, expires_at }]
reviews:
  - key: 2026-q3-access-review
    kind: access_review
    cadence: quarterly
    performed_on: 2026-07-01
    supersedes: …            # the previous period's evidence id, where the queue showed one
    input: { file, sha256, size_bytes, records_reviewed, produced_by }
    outcome: { confirmed, exceptions }
    exceptions: [{ reference, disposition, owner, note, resolved_on }]
    refs: [ "reviews/…csv:1" ]
    control_mappings: [{ control_id, evidence_item_ids }]
    interpretation: { owner, decided_at, expires_at, rationale }
```

Commit it — it is the reviewable artifact. Keep `.noru/.cache/` out of git, and think about whether
the export itself belongs in git at all: an entitlement dump is a list of who can reach what.

`refs[]` is a `file:line` citation like every other piece, but here it usually points at the export
being attested rather than at a line of source. Line `1` on the export is the honest answer when the
whole file is what was reviewed.

## Idempotency

`createEvidence` carries a content-addressed `idempotencyKey` and `expiresAt`, so the sign-off and
its validity window land atomically. An identical retry returns the original evidence id; editing
the description later does not defeat the key. The description marker remains the compatibility
probe for deployments predating this server contract.

`updateEvidence` is now only a drift repair: it appears when an existing sign-off's stored expiry
no longer matches the attestation. Re-signing changed content produces a new historical record.

A new period is a new key. Last quarter's access review is not this quarter's, and the piece will
not pretend otherwise.

## Verify

```bash
node    plugins/review-signoff/scripts/collect.mjs --repo=. --output=json
python3 plugins/review-signoff/scripts/validate_manifest.py .noru/review-signoff.yml
python3 plugins/review-signoff/scripts/validate_manifest.py .noru/review-signoff.yml --as-of=2027-01-01
node    plugins/review-signoff/scripts/diff.mjs --repo=.
node    plugins/review-signoff/scripts/push.mjs --repo=. --confirm
```

Exit codes: `0` success · `1` drift, validation failure, an expired sign-off under `--as-of`, a
missing prerequisite, or a stale plan · `2` usage, including a push without `--confirm`.
