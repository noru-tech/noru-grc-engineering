---
name: diff
description: Show exactly which governance records would be filed in Noru and what they would satisfy. Reads only; writes nothing.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /governance-records:diff

Show what `/governance-records:push` would do, before it does anything. No writes. Read scopes only:
`read:organization`, `read:controls`, `read:evidence`.

## 1. Validate, and emit the parsed manifest

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/validate_manifest.py" <repo>/.noru/governance-records.yml \
  --emit-parsed=<repo>/.noru/.cache/governance-records.parsed.json
```

Only a valid manifest produces the parsed file, so an invalid one cannot reach `:push`.

## 2. Read the current evidence from Noru

No idempotency key is documented for evidence, so the piece does not assume one: it probes instead.
Each record lands with a marker in its description built from the record key and a digest of the
rendered account, and the diff looks for that marker in evidence already in the organization.

Call `findOrganization` and `getOrganizationEvidence`, then write
`<repo>/.noru/.cache/noru-state.json`:

```json
{
  "fetched_at": "2026-08-27T09:14:00Z",
  "connection": {
    "organization": { "id": "...", "name": "..." },
    "endpoint": "https://api.noru.tech/v1/mcp",
    "scopes": ["read:organization", "read:controls", "read:evidence"]
  },
  "evidence": [
    {
      "id": "...",
      "title": "...",
      "description": "...",
      "linkedControls": [{ "id": "lowercase-canonical-id" }]
    }
  ]
}
```

`linkedControls` matters: it is how the diff decides whether a mapping added to the manifest after
the record was filed still needs a link. The `search` filter on `getOrganizationEvidence` matches
description text, so you can narrow to `noru-grc-engineering:governance-records` rather than pulling
the whole register.

## 3. Build the plan

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/diff.mjs" --repo=<repo>
```

- `+ create` `createEvidence` — no evidence carries this record's marker yet
- `+ create` `linkEvidenceToControl` — the record is already filed but a mapping added since is
  missing
- `= skip` — already filed with exactly this account

**A plan of all `skip` is the correct result of a second run.**

## 4. Show it to the user

Print the plan, and for each `create` show the control ids and evidence item ids it will satisfy.
Say plainly that this piece's idempotency is a client-side probe against a marker, not a server-side
key: if someone edits the description in the Noru UI, or the account itself is rewritten, a re-run
files a new record rather than updating the old one. That gap is recorded in `piece.json`.

Then stop. `/governance-records:push` is a separate, explicitly confirmed step.
