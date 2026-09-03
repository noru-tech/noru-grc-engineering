# Generic MCP clients

Any client that speaks MCP over Streamable HTTP can drive these pieces. The plugin packaging is a
convenience; the pieces themselves are scripts plus a documented sequence of tool calls.

## Server

```
POST/GET https://api.noru.tech/v1/mcp
Authorization: Bearer <NORU_API_KEY>
```

Same auth and scope model as Noru's REST API. Scopes on the key decide which tools the session can
even see: **a missing tool means a missing scope, not an outage.**

## Tools each piece uses

Verify any tool name against the server's own `tools/list` before relying on it. Never assume a tool
exists because it would be convenient.

### ai-inventory

| Phase | Tools | Scopes |
|---|---|---|
| `:diff` (read) | `getOrganizationAssets`, `getOrganizationVendors`, `getOrganizationEvidence`, `getOrganizationFrameworks`, `getOrganizationControls` | `read:assets`, `read:vendors`, `read:evidence`, `read:frameworks`, `read:controls` |
| `:push` (write) | `createAsset`, `createVendor`, `createEvidence`, `linkEvidenceToControl` | `write:assets`, `write:vendors`, `write:evidence` |

### evidence-push

| Phase | Tools | Scopes |
|---|---|---|
| `:scan` (read) | `getOrganizationControls`, `getControlContext`, `getEvidenceItems`, `getEvidenceForControl` | `read:controls`, `read:evidence` |
| `:diff` (read) | `getOrganizationEvidence` | `read:evidence` |
| `:push` (write) | `POST /v1/evidence/upload` — **REST, not MCP** | `write:evidence` |

File upload is a deliberate omission from the MCP surface: tool arguments are JSON and cannot carry
a multipart body. The published `createEvidence` tool says so in as many words — "File uploads
(multipart) are not supported via MCP" — so a client should not go looking for one.

### governance-records

| Phase | Tools | Scopes |
|---|---|---|
| `:scan` (read) | `getOrganizationControls`, `getControlContext`, `getEvidenceItems`, `getEvidenceForControl` | `read:controls`, `read:evidence` |
| `:diff` (read) | `getOrganizationEvidence` | `read:evidence` |
| `:push` (write) | `createEvidence`, `linkEvidenceToControl` | `write:evidence` |

`linkEvidenceToControl` is only reached when the record already exists and a control mapping was
added to the manifest afterwards; `createEvidence` carries the mappings on a first push.

### review-signoff

| Phase | Tools | Scopes |
|---|---|---|
| `:scan` (read) | `getOrganizationControls`, `getControlContext`, `getEvidenceForControl`, `getEvidenceItems` | `read:controls`, `read:evidence` |
| `:diff` (read) | `getOrganizationEvidence` | `read:evidence` |
| `:push` (write) | keyed `createEvidence` with expiry; `updateEvidence` only for later expiry drift | `write:evidence` |

New sign-offs carry expiry in the create, so there is no generated-id dependency. A later
`updateEvidence` call appears only when the current-state snapshot shows expiry drift.

### audit-pack

| Phase | Tools | Scopes |
|---|---|---|
| `:scan` (read) | `getOrganizationFrameworks`, `getOrganizationControls`, `getControlContext`, `getEvidenceForControl`, `getEvidenceItems` | `read:frameworks`, `read:controls`, `read:evidence` |
| `:diff` (read) | `getOrganizationEvidence` | `read:evidence` |
| `:push` (write) | `createEvidence`, `linkEvidenceToControl` | `write:evidence` |

This is the piece that mostly *consumes*: the pack under `.noru/audit-pack/` is a local deliverable
and what lands in Noru is the tested conclusion for each control. Each workpaper create carries a
content-addressed server key. Its description marker and the narrowed state probe remain for older
deployments.

### iac-scan

| Phase | Tools | Scopes |
|---|---|---|
| `:scan` (read) | `getSecurityFindings` with `source: "iac-scan"`, `getOrganizationAssets`, `getOrganizationRisks` | `read:risks`, `read:assets` |
| `:diff` (read) | the scan tools plus `findOrganization` for the plan binding | adds `read:organization` |
| `:diff` (read) | `getSecurityFindings` with `source: "iac-scan"` | `read:risks` |
| `:push` (write) | `createSecurityFinding` | `write:risks` |

`createSecurityFinding` is a documented server-side upsert on `source + externalId`, so filing a
finding and closing one are the same call, and running the same call twice lands the same record.
This is the only piece where that holds — do not generalize it to the others, and stop for a human
on a failure you do not understand even here.

### privacy-datamap

| Phase | Tools | Scopes |
|---|---|---|
| `:scan` | none — the collector reads the repository and opens no socket | — |
| `:scan` (read) | `getPrivacyDataMap`, `listPrivacyDatasets`, `getPrivacyTaxonomy` | `read:datamaps` |
| `:diff` (read) | the scan tools plus `findOrganization` for the plan binding | adds `read:organization` |
| `:push` (write) | `ingestDatamap` | `write:datamaps` |

`ingestDatamap` takes the whole map for a source, so the push is one call whether the repository has
four fields or four hundred. `getPrivacyTaxonomy` is the truth about which taxonomy keys are valid;
the snapshot vendored under `contract/lib/taxonomy/` is the offline floor the validator needs when
it runs with no network. A key Noru knows and the snapshot does not is a stale snapshot, fixed by
refreshing that directory — never by editing a vendored copy.

## Control identifiers

`getOrganizationControls` returns two identifiers per control. The lowercase `id` is canonical —
use it for every `controlId` parameter and store it in manifests. The uppercase `controlId` is a
display identifier; MCP accepts it case-insensitively and normalizes it, but round-tripping the
display form through a manifest is how ids drift.

## Sequence

```
1. scan     run the piece's collect.mjs           (offline, deterministic)
2. review   edit .noru/<piece>.yml                (a human adds judgement and owners)
3. validate validate_manifest.py --emit-parsed    (only a valid manifest can proceed)
4. state    call the read tools above, write .noru/.cache/noru-state.json
5. diff     run diff.mjs                          (writes a plan; changes nothing in Noru)
6. confirm  a human reads the plan and agrees
7. push     run push.mjs --confirm                (emits the exact call list, or uploads over REST)
8. execute  make exactly those calls, in order
9. verify   re-run 4-5; every operation must now be "skip"
```

Steps 5 and 6 are not optional and not reorderable. `push.mjs` exits `2` without `--confirm` and
exits `1` if the manifest changed after the plan was written.

## Rate limits and errors

500 requests per 10 minutes per key. `X-RateLimit-Remaining` and `X-RateLimit-Reset` come back on
every REST response; exceeding it returns `429` with `error.code = RATE_LIMITED`.

MCP write errors are structured:

```json
{ "error": { "code": "NOT_FOUND", "message": "…" } }
```

Documented codes: `UNAUTHORIZED`, `FORBIDDEN` (missing scope), `NOT_FOUND`, `BAD_REQUEST`,
`RATE_LIMITED`, `INTERNAL_ERROR`. Write tools may also answer with an outcome rather than a failure
— an already-linked evidence record comes back as `ALREADY_LINKED`, which is benign. Handle codes
you receive; do not hardcode ones you have not seen.

Evidence creates and uploads accept stable idempotency keys and report `created` or `reused`.
Retry the exact payload with the exact key after an ambiguous failure. If the connected server's
published schema lacks the key, use the plugin's marker-probe fallback and refresh state first.
