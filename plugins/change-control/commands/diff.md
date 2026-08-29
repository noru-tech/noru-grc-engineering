---
name: diff
description: Show exactly what would change in Noru — the findings each owned exception would file or close, and the evidence record for the window. Reads only; writes nothing.
---

# /change-control:diff

Reads only. Nothing is written to Noru by this command.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/validate_manifest.py" .noru/change-control.yml \
  --emit-parsed=.noru/.cache/change-control.parsed.json
node "${CLAUDE_PLUGIN_ROOT}/scripts/diff.mjs" --repo=<repo>
```

Before running it, write `.noru/.cache/noru-state.json` from the piece's read tools —
`getOrganizationEvidence` and `getSecurityFindings` — so the plan compares against what Noru
actually holds rather than assuming an empty organization.

## What the plan will show

- one `createSecurityFinding` per owned exception, keyed on `(source, externalId)`. An exception
  dispositioned `remediated` or `false_positive` is pushed with status `resolved`, so a re-run after
  the fix **closes** the finding rather than leaving a stale open one beside a fixed problem;
- one `createEvidence` for the window — one record, not one per change;
- one `linkEvidenceToControl` per mapping, carrying `depends_on` because the evidence id does not
  exist until the create above runs.

A second run on unchanged input must be all `skip`. If it is not, that is a bug, and
`scripts/test_idempotency.py` says so.

Read the plan. `:diff` before `:push` is a security control, not UX polish: the plan is bound to the
manifest bytes on disk, and editing the manifest invalidates it.
