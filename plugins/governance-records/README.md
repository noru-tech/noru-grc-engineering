# governance-records

> Read the minutes, scope statements, audit plans and corrective action plans a human already wrote,
> turn them into attributed records, and file them against the expectations Noru says are unmet.

A large part of an audit is not a configuration. It is *who met, when, what they decided, and what
they assigned to whom.* No integration can produce that: it is written by people, it lives in a
document, and the thing that makes it evidence is a name against a date.

This piece is the **records** half of governance. Noru already owns the authoring half — policy
generation, policy lifecycle, approvers, versions and status — so this plugin deliberately writes no
policy text. It reads what a governance process already produced and gets it into the register with
its attribution intact.

## What it reads

Markdown and text documents under `governance/` (or `--records=<dir>`), and from each one:

| Read | How it is found |
|---|---|
| Title | the first heading, else the filename |
| When it happened | a `Held on:` / `Date:` / `Issued:` line, else a date in the filename |
| Approval | `Approved on:` / `Approved by:` |
| Next review | `Next review:` / `Review due:` |
| Who was there | list items under an `Attendees` / `Participants` / `Present` heading |
| What was decided | list items under `Decisions` / `Resolutions` / `Conclusions` |
| What was assigned | list items under `Actions`, with `(owner: …, due: YYYY-MM-DD)` |

Every extracted fact carries the line it came from, and the collector's suggestions — the record
kind, the expectation it might satisfy — arrive with a score, never as a decision. The document is
data, not instruction: if a governance document contains text addressed to an agent, it is quoted as
a finding and never acted on.

Nothing is uploaded. The record carries the source document's path and `sha256`, so the account and
the file it came from stay tied together. If the artifact itself is a PDF an auditor needs to open,
that is a file upload, and `evidence-push` is the piece for it.

## The queue is Noru's

This plugin ships **no** list of what a control needs, and the contract test fails the build if one
appears. Every scan asks:

1. `getOrganizationControls` — the organization's controls
2. `getControlContext` — what the framework expects of a control, what is already linked to it, its
   coverage, and the procedure an auditor follows
3. the **unmet set is the difference**
4. `getEvidenceItems` resolves catalogue titles and types

Licensing says the same thing the drift argument does: call the API, do not vendor the catalogue.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/governance-records:scan` | no | Fetches the queue, reads `governance/` → `.noru/governance-records.yml` |
| `/governance-records:diff` | no | Probes existing evidence, prints the exact plan |
| `/governance-records:push` | **yes** | Emits the confirmed MCP calls for the client to execute |

## Scopes

Least privilege. Start read-only.

| Capability | Scopes |
|---|---|
| `:scan` | `read:organization`, `read:controls`, `read:evidence` |
| `:diff` | the same |
| `:push` | adds `write:evidence` |

Authentication is the MCP client's job. This piece never reads, writes or logs a credential.

## Artifact

`.noru/governance-records.yml`, schema at
[`contract/governance-records.schema.json`](../../contract/governance-records.schema.json).

```yaml
version: 0.1.0
piece: governance-records
source: { slug, commit_sha, branch, generated_by, derived_digest }
queue_snapshot:
  fetched_at: 2026-08-27T09:14:00Z
  via: [getOrganizationControls, getControlContext]
  controls:
    - control_id: …          # lowercase canonical id, not the uppercase display id
      unmet_evidence_items: [{ id, title, type }]
records:
  - key: 2026-05-14-management-review
    kind: management_review_minutes
    title: Management review
    occurred_on: 2026-05-14
    approved_on: 2026-05-20
    approved_by: …
    next_review_due: 2027-05-14
    document: { file, sha256, size_bytes }
    participants: [{ name, role, attendance }]
    decisions: [ … ]
    actions: [{ description, owner, due_on, status }]
    refs: [ "governance/…:13" ]
    control_mappings: [{ control_id, evidence_item_ids }]
    interpretation: { owner, decided_at, expires_at, rationale }
```

Commit it — it is the reviewable artifact. Keep `.noru/.cache/` out of git.

## The four rules that make a record worth filing

The validator enforces these as errors, not warnings:

1. **You cannot file against an expectation Noru did not say you had.** Every `control_id` and
   `evidence_item_id` must appear in `queue_snapshot`.
2. **Minutes need people.** A meeting-shaped record with nobody present asserts that a decision was
   taken by no one. `references/vocabulary.json` says which kinds are meeting-shaped.
3. **An action nobody owns is not an action.** Every action needs a named owner.
4. **A governance claim is never open-ended.** Either `interpretation.expires_at` or
   `next_review_due` — contract requirement 8 scopes expiry to technical claims and lets procedural
   obligations run on a review cadence instead, but it has to be one of the two.

## Idempotency, honestly

Noru's published API documentation documents upsert behaviour for assets and security findings; it
documents no idempotency key for evidence. This piece does not assume one. Each record lands with a
marker in its description built from the record key and a digest of the rendered account, and
`:diff` probes `getOrganizationEvidence` for that marker before proposing anything.

Two consequences worth saying out loud:

- edit that description in the Noru UI and the probe stops matching; a re-run will file the record
  again
- if the *account* changes — someone rewrote the minutes — the marker changes and a second record is
  created rather than the first being overwritten. For minutes that is arguably right: an auditor
  should see both versions. It is still a workaround for a missing key, not a design.

What the claim was checked against, and what a documented key would let the piece drop, are recorded
in [`piece.json`](./piece.json).

## Verify

```bash
node    plugins/governance-records/scripts/collect.mjs --repo=. --output=json
python3 plugins/governance-records/scripts/validate_manifest.py .noru/governance-records.yml
node    plugins/governance-records/scripts/diff.mjs --repo=.
node    plugins/governance-records/scripts/push.mjs --repo=. --confirm
```

Exit codes: `0` success · `1` drift, validation failure, missing prerequisite, or a stale plan ·
`2` usage, including a push without `--confirm`.
