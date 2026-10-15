---
name: diff
description: Show exactly which vendors would be created in Noru, the evidence record for the window, and the control links. Reads only; writes nothing.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /dev-agents:diff

Reads only. Nothing is written to Noru by this command. Read scopes: `read:organization`,
`read:vendors`, `read:evidence`.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/validate_manifest.py" <repo>/.noru/dev-agents.yml \
  --emit-parsed=<repo>/.noru/.cache/dev-agents.parsed.json
node "${CLAUDE_PLUGIN_ROOT}/scripts/diff.mjs" --repo=<repo>
```

Before running it, write `<repo>/.noru/.cache/noru-state.json` from `findOrganization`,
`getOrganizationVendors` (fields `id` and `name`, every page) and `getOrganizationEvidence` with
`search: "noru-grc-engineering:dev-agents"` (every page), so the plan compares against what Noru
holds rather than assuming an empty organization:

```json
{
  "fetched_at": "2026-10-05T09:00:00Z",
  "connection": {
    "organization": { "id": "...", "name": "..." },
    "endpoint": "https://api.noru.tech/v1/mcp",
    "scopes": ["read:organization", "read:controls", "read:evidence", "read:vendors"]
  },
  "vendors": [{ "id": "...", "name": "..." }],
  "evidence": [{ "id": "...", "description": "...", "linkedControls": [{ "id": "..." }] }]
}
```

## What the plan will show

- one `createVendor` per supplier — `skip` where a vendor with that name already exists, which the
  published tool description says would be returned rather than duplicated anyway;
- one `createEvidence` for the window — one record, not one per agent or server;
- one `linkEvidenceToControl` per control the manifest chose, carrying `depends_on` when the
  evidence does not exist yet.

A second run on unchanged input must be all `skip`. If it is not, that is a bug, and
`scripts/test_idempotency.py` says so.

Read the plan with the user. `:diff` before `:push` is a security control: the plan is bound to the
manifest bytes on disk, and editing the manifest invalidates it.
