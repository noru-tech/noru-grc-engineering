---
name: diff
description: Show exactly which risks would be created in Noru, which would change, which would be resolved, and which control links would be added. Reads only; writes nothing.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /threat-model:diff

Show what `/threat-model:push` would do, before it does anything. No writes. Read scopes only:
`read:organization`, `read:risks`, `read:controls`.

## 1. Validate, and emit the parsed manifest

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/validate_manifest.py" <repo>/.noru/threat-model.yml \
  --emit-parsed=<repo>/.noru/.cache/threat-model.parsed.json
```

## 2. Read the current risks from Noru

Call `findOrganization`, then `getOrganizationRisks` with `search: "noru-grc-engineering:threat-model:"`,
and for each of those risks `getRiskControlMappings`. Write `<repo>/.noru/.cache/noru-state.json`:

```json
{
  "fetched_at": "2026-08-27T09:14:00Z",
  "connection": {
    "organization": { "id": "...", "name": "..." },
    "endpoint": "https://api.noru.tech/v1/mcp",
    "scopes": ["read:organization", "read:risks", "read:controls"]
  },
  "risks": [
    { "id": "...", "externalId": "threat-model:<slug>:<key>", "title": "...", "status": "identified",
      "category": "security", "likelihood": "possible", "impact": "major",
      "description": "...", "mitigationStrategy": "..." }
  ],
  "risk_controls": { "<risk id>": ["<control id>"] }
}
```

Keep every field above: they are what the plan compares. `risk_controls` is what lets the plan link
a control added to an existing risk; a CI job cannot read it per risk, and then plans links only for
risks it creates.

## 3. Build the plan

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/diff.mjs" --repo=<repo>
```

- `+ create` — no risk carries this externalId yet (createRisk), or a control is not linked yet
  (linkRiskToControl; for a new risk, the id comes from the createRisk call)
- `~ update` — a field this piece writes would change (updateRisk), or a risk the model no longer
  names is resolved
- `= skip` — already says exactly this

**A plan of all `skip` is the correct result of a second run.**

## 4. Show it to the user

Lead with the resolves and the likelihood or impact changes. Say plainly that a risk resolved
because the model no longer names it has been taken out of the model, not necessarily treated, and
that links are only ever added, never removed. Then stop. `/threat-model:push` is a separate,
explicitly confirmed step.
