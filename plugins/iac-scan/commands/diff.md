---
name: diff
description: Show exactly which security findings would be filed in Noru, which would change, and which would be closed. Reads only; writes nothing.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /iac-scan:diff

Show what `/iac-scan:push` would do, before it does anything. No writes. Read scopes only:
`read:risks`, `read:assets`.

## 1. Validate, and emit the parsed manifest

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/validate_manifest.py" <repo>/.noru/iac-scan.yml \
  --emit-parsed=<repo>/.noru/.cache/iac-scan.parsed.json
```

Only a valid manifest produces the parsed file, so an invalid one cannot reach `:push`.

## 2. Read the current findings from Noru

`createSecurityFinding` is documented as an idempotent upsert on `source + externalId`, so this step
is not a probe standing in for a missing key — it is how the plan can tell you *what would change*
rather than only *what would be written*.

Call `getSecurityFindings` with `source: "iac-scan"` and write
`<repo>/.noru/.cache/noru-state.json`:

```json
{
  "fetched_at": "2026-08-27T09:14:00Z",
  "security_findings": [
    {
      "id": "...",
      "source": "iac-scan",
      "externalId": "<slug>:<key>",
      "title": "...",
      "checkName": "...",
      "description": "...",
      "severity": "high",
      "status": "open",
      "category": "configuration",
      "observedAt": "2026-08-20T00:00:00Z",
      "assetId": null,
      "riskId": null,
      "ownerEmail": null
    }
  ]
}
```

Keep every field above: they are what the plan compares, and a field missing from the snapshot looks
like a field that changed.

## 3. Build the plan

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/diff.mjs" --repo=<repo>
```

- `+ create` — no finding carries this `externalId` yet
- `~ update` — a finding exists and something the manifest says would change; the reason names which
  fields
- `~ update` (**close**) — a finding is open in Noru under this repository's slug and no rule
  reproduced it. The same keyed upsert sets `status: resolved`
- `= skip` — already filed saying exactly this, or already resolved

**A plan of all `skip` is the correct result of a second run.**

## 4. Show it to the user

Print the plan. Lead with the closes and the severity changes — those are the two a reader will
actually argue with. For each `create`, say which rule fired and where.

Say plainly what is *not* compared: provenance travels on every write in `raw`, but it is not part of
the comparison, so a finding that has not otherwise changed keeps the commit it was last written
from. That is deliberate — comparing the commit would make every commit a write.

Then stop. `/iac-scan:push` is a separate, explicitly confirmed step.
