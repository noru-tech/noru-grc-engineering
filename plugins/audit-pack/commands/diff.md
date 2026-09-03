---
name: diff
description: Show exactly which workpaper conclusions would be filed in Noru and what they would satisfy. Reads only; writes nothing.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /audit-pack:diff

Show what `/audit-pack:push` would do, before it does anything. No writes. Read scopes only:
`read:organization`, `read:frameworks`, `read:controls`, `read:evidence`.

## 1. Validate, and emit the parsed manifest

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/validate_manifest.py" <repo>/.noru/audit-pack.yml \
  --emit-parsed=<repo>/.noru/.cache/audit-pack.parsed.json
```

Only a valid manifest produces the parsed file, so an invalid one cannot reach `:push` — and the same
file is what the pack under `.noru/audit-pack/` is rendered from.

## 2. Read the current evidence from Noru

No idempotency key is documented for evidence, so the piece does not assume one: it probes instead.
Each workpaper lands with a marker in its description built from the pack key, the workpaper key and
a digest of the rendered workpaper.

Call `findOrganization` and `getOrganizationEvidence`, then write
`<repo>/.noru/.cache/noru-state.json`:

```json
{
  "fetched_at": "2026-08-27T09:14:00Z",
  "connection": {
    "organization": { "id": "...", "name": "..." },
    "endpoint": "https://api.noru.tech/v1/mcp",
    "scopes": ["read:organization", "read:frameworks", "read:controls", "read:evidence"]
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

`linkedControls` matters: it is how the diff decides whether a workpaper that is already filed still
needs its control link. The `search` filter on `getOrganizationEvidence` matches description text, so
you can narrow to `noru-grc-engineering:audit-pack` rather than pulling the whole register.

## 3. Build the plan

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/diff.mjs" --repo=<repo>
```

- `+ create` `createEvidence` — no evidence carries this workpaper's marker yet
- `+ create` `linkEvidenceToControl` — the record is filed but the control link is missing
- `= skip` — already filed with exactly this conclusion

**A plan of all `skip` is the correct result of a second run.**

## 4. Show it to the user

Print the plan, and say two things plainly:

- **the pack itself is not being pushed.** What lands is the tested conclusion per control; the
  index, the workpapers and the sampling worksheets stay local, because Noru is the register and a
  folder is not.
- **idempotency here is a client-side probe against a marker, not a server-side key.** If someone
  edits a description in the Noru UI, or a control is re-tested, a re-run files a **new** record
  rather than updating the old one. For an audit trail that is the behaviour you want — both
  conclusions stay visible — but it is a consequence of a missing key, not a design choice, and the
  gap is recorded in `piece.json`.

Then stop. `/audit-pack:push` is a separate, explicitly confirmed step.
