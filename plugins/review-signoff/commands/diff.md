---
name: diff
description: Show exactly which sign-offs would be filed in Noru, what they would satisfy, and what expiry each record would carry. Reads only; writes nothing.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /review-signoff:diff

Show what `/review-signoff:push` would do, before it does anything. No writes. Read scopes only:
`read:organization`, `read:controls`, `read:evidence`.

## 1. Validate, and emit the parsed manifest

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/validate_manifest.py" <repo>/.noru/review-signoff.yml \
  --emit-parsed=<repo>/.noru/.cache/review-signoff.parsed.json
```

Only a valid manifest produces the parsed file, so an invalid one cannot reach `:push`.

## 2. Read the current evidence from Noru

No idempotency key is documented for evidence, so the piece does not assume one: it probes. Each
sign-off lands with a marker in its description built from the review key and a digest of the
rendered attestation, and the diff looks for that marker in evidence already in the organization.

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
    { "id": "...", "title": "...", "description": "...", "expiresAt": "2026-10-04T23:59:59Z" }
  ]
}
```

`expiresAt` matters here: it is how the diff decides whether the record already carries the expiry
the sign-off claims. The `search` filter on `getOrganizationEvidence` matches description text, so
you can narrow to `noru-grc-engineering:review-signoff` rather than pulling the whole register.

## 3. Build the plan

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/diff.mjs" --repo=<repo>
```

Two operations per sign-off:

- `createEvidence` — `+ create` when no evidence carries this sign-off's marker, `= skip` when it
  already does
- `updateEvidence` — `~ update` to put the sign-off's expiry on the record, `= skip` when the record
  already expires on that day

On a first run the update is addressed to a record that does not exist yet, so it carries
`depends_on`: the evidence id comes from the create above it.

**A plan of all `skip` is the correct result of a second run.**

## 4. Show it to the user

Print the plan, and for each `create` show the control ids and evidence item ids it will satisfy,
who signed it, and the date it expires. Say plainly that:

- idempotency here is a client-side probe against a marker, not a server-side key — edit the
  description in the Noru UI and a re-run files the record again
- a re-signed period produces a second record rather than replacing the first
- the expiry is a real date in Noru, not just text: the record will go stale on it

That gap is recorded in `piece.json`. Then stop — `/review-signoff:push` is a separate, explicitly
confirmed step.
