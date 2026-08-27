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

No idempotency key is documented for `POST /v1/evidence/upload`, so the piece does not assume one:
it probes instead. Each upload's description carries a marker containing the artifact's content
digest, and the diff looks for that marker in evidence already in the org.

Call `getOrganizationEvidence` and write `<repo>/.noru/.cache/noru-state.json`:

```json
{
  "fetched_at": "2026-08-27T09:14:00Z",
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
Say plainly that this piece's idempotency is a client-side probe against a marker, not a server-side
key — if someone edits the description in the Noru UI, the probe stops matching and a re-run will
upload again. That gap is recorded in `piece.json`.

Then stop. `/evidence-push:push` is a separate, explicitly confirmed step.
