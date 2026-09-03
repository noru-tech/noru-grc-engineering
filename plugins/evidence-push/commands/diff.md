---
name: diff
description: Show exactly which local artifacts would be uploaded to Noru and what they would map to. Reads only; uploads nothing.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /evidence-push:diff

Show what `/evidence-push:push` would upload, before it uploads anything. No writes. Read scopes
only: `read:organization`, `read:controls`, `read:evidence`.

## 1. Validate, and emit the parsed manifest

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/validate_manifest.py" <repo>/.noru/evidence-push.yml \
  --emit-parsed=<repo>/.noru/.cache/evidence-push.parsed.json
```

Only a valid manifest produces the parsed file, so an invalid one cannot reach `:push`.

## 2. Read the current evidence from Noru

Each upload carries an `Idempotency-Key` derived from the artifact digest. Its description also
carries a digest marker, and the diff keeps that probe for older Noru deployments.

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
  "evidence": [{ "id": "...", "title": "...", "description": "..." }]
}
```

The `search` filter on `getOrganizationEvidence` matches description text, so you can narrow to
`noru-grc-engineering:evidence-push` rather than pulling the whole register.

## 3. Build the plan

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/diff.mjs" --repo=<repo>
```

Each line is one artifact:

- `+ create` — no evidence in the org carries this artifact's content marker
- `= skip` — already uploaded (same bytes), or the file is missing from the working tree

**A plan of all `skip` is the correct result of a second run.**

## 4. Show it to the user

Print the plan, and for each `create` show the control ids and evidence item ids it will satisfy.
Say plainly that each upload carries a stable `Idempotency-Key` derived from the artifact digest.
The marker probe is retained for older Noru deployments; description edits do not defeat the
server-side key on current deployments.

Then stop. `/evidence-push:push` is a separate, explicitly confirmed step.
