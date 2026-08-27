# audit-pack

> Assemble what an auditor asks for — the evidence bundle, the sampling and the workpapers, for one
> framework over one window — from Noru's own graph plus the files that only exist locally. Hand the
> pack over; land the tested conclusions in Noru.

Every other piece in this toolkit **discovers** something in a repository and pushes it. This one
mostly **consumes**: it reads what Noru already holds, gathers the local half an integration cannot
reach, and produces an output. That difference is worth reading about before the commands, because
it is the one place the contract had to be reinterpreted rather than followed.

## What lands in Noru, and what deliberately does not

The pack itself — the index, the workpapers, the sampling worksheets under `.noru/audit-pack/` — is a
**local deliverable**. It is a point-in-time export for handover, regenerated from Noru and from the
local files every time `:scan` runs, and nothing ever reads it back. Pushing it would duplicate a
register Noru already keeps, which the contract's own non-goals rule out.

What does belong in the register is the part a folder cannot hold: **the tested conclusion for each
control over this window, and how it was reached.** So one workpaper becomes one evidence record,
mapped to the one control it is about. A single blob mapped to forty controls is the antipattern that
makes evidence unreadable, and "assemble a pack" is exactly the request that invites it.

That is the reinterpretation of `:push` this piece needs, and it is worth stating plainly:

> For a piece that assembles rather than collects, `:push` is not "send the artifact". It is
> "commit the judgements the artifact contains to the system of record, and keep the artifact
> local."

Everything else in the contract fits unchanged — `:scan` is assembly, `:diff` still shows what would
change, and the interpretation block is exactly a workpaper's sign-off.

## The queue is Noru's, and here it is the whole scope

A pack's scope is not something a plugin can hold an opinion about. Every scan asks:

1. `getOrganizationFrameworks` — which frameworks the organization is actually audited against
2. `getOrganizationControls` — the controls in that framework's scope, with their status
3. `getControlContext` — what the framework expects of each control, what is already linked, the
   coverage between them, and **whether a testing procedure is available**
4. `getEvidenceForControl` — every linked record with its status and expiry. A record that expired
   *during* the window is the difference between a control that was covered and one that merely
   looks covered today
5. `getEvidenceItems` — resolves catalogue titles and types

**The testing procedure text is deliberately never stored.** The queue snapshot records only that one
exists. Read it from Noru when you test; write what you actually did in `scope`. A pack that copied
the procedure would go stale the moment the framework moved, and it would vendor catalogue content
this repository is not allowed to hold.

## Sampling you can redraw

An auditor's first question about a sample is "how did you pick those?", and the honest answer has to
be reproducible. The collector draws from the population file's **own digest** as the seed, so:

- the same file always draws the same sample — no clock, no random source
- the manifest records the method, the seed, the size and the drawn references
- anyone holding the population file can order it by `sha256(seed + "|" + reference)` ascending, take
  the first `size` rows, and get exactly the list in the pack

There are two methods and only two, `deterministic_hash` and `full_population`, because both are
reproducible. A judgemental selection is a legitimate audit technique and is not one of them: record
it as a full-population test over the subset you actually scoped, and say in the rationale how the
subset was chosen.

The validator also enforces a floor: a sample below the smallest defensible size for its population
is one an auditor will ask about, so it asks first.

| Population | Smallest sample the validator accepts |
|---|---|
| up to 30 | 5, or all of it if smaller |
| 31–100 | 10 |
| 101–500 | 25 |
| over 500 | 45 |

## Expiry, anchored on the window rather than the signature

`interpretation.expires_at` is required, and the horizon is measured from the **end of the audit
window** — not from `decided_at`:

| Rule | Why |
|---|---|
| `expires_at` is **required** | A pack is assurance about a period, and assurance that never lapses is assurance nobody will renew |
| Must be **after** the window closes | A conclusion that expires inside its own period never asserted anything |
| Measured from the window's end, not the signature | Signing late does not extend what a pack covers |
| `effective`: at most 400 days | Roughly the next annual cycle |
| `deficient` / `not_tested`: at most 120 days | A control you found broken, or never tested, is not something to sign off for a year and revisit at the next audit |
| `decided_at` cannot precede the window's end | A conclusion about a period cannot be drawn while the period is still running |

`--as-of=YYYY-MM-DD` turns an already-expired conclusion into an error. The validator never reads the
clock on its own; that is what keeps it deterministic and keeps the fixtures from rotting.

## What else the validator insists on

| Rule | Why |
|---|---|
| Every control id and evidence item id must be in the queue snapshot | Requirement 9. A pack covers what Noru put in scope |
| One workpaper, one control | Two accounts of one control is two conclusions somebody has to reconcile, and an auditor reconciles them in the least generous way available |
| An inspected file must be one this scan digested, at that digest | What an auditor gets handed has to be the bytes that were tested, or the digests prove nothing |
| An inspected evidence id must be one the queue returned | The pack cites what Noru says is linked, never an id from memory |
| `reviewed_by` cannot be `prepared_by` | Recording a pack as reviewed while nobody checked spends the credibility of a control that was never applied |
| An `effective` conclusion cannot carry a **deferred** exception | Either it was not effective, or the exception is not really deferred |
| A control in scope with no workpaper is a **warning** | Scoping a control out is a legitimate decision. It has to be a visible one |

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/audit-pack:scan` | no | Fetches the scope, digests the local artifacts, draws the samples, writes `.noru/audit-pack.yml` — and, once the manifest validates, renders the pack under `.noru/audit-pack/` |
| `/audit-pack:diff` | no | Probes existing evidence, prints the exact plan |
| `/audit-pack:push` | **yes** | Emits the confirmed MCP calls for the client to execute |

The pack is rendered only from a manifest that **validated against this same repository state**. A
pack built from an unreviewed file would be handed to an auditor looking exactly like a real one, so
`:scan` renders the scope and the inputs and says plainly that there are no conclusions yet.

## Scopes

Least privilege. Start read-only.

| Capability | Scopes |
|---|---|
| `:scan` | `read:organization`, `read:frameworks`, `read:controls`, `read:evidence` |
| `:diff` | the same |
| `:push` | adds `write:evidence` |

Authentication is the MCP client's job. This piece never reads, writes or logs a credential.

## Artifact

`.noru/audit-pack.yml`, schema at
[`contract/audit-pack.schema.json`](../../contract/audit-pack.schema.json).

Commit it — it is the reviewable record of what was tested and concluded. Whether the rendered pack
under `.noru/audit-pack/` belongs in git is your call: it is regenerated on every scan and it names
people. Keep `.noru/.cache/` out of git either way.

## Idempotency

| Operation | Kind | Key | Second run |
|---|---|---|---|
| `createEvidence` | `client_probe` | marker in the description | skip — the marker is already there |
| `linkEvidenceToControl` | `client_probe` | `evidenceId` + `controlId` | skip — the link already exists |

No idempotency key is documented for evidence, so this piece does not assume one: it embeds a marker
built from the pack key, the workpaper key and a digest of the rendered workpaper, and probes before
creating. Re-testing a control changes the workpaper, so it files a **new** record rather than
overwriting the old conclusion — which is the behaviour you want from an audit trail, and is also
what the missing key forces. The gap is recorded in `piece.json`.

A second `:scan` + `:diff` on unchanged input must produce a plan of all `skip`. If it does not,
that is a bug — `scripts/test_idempotency.py` asserts it.

## Verify

```bash
node    plugins/audit-pack/scripts/collect.mjs --repo=. --output=json
python3 plugins/audit-pack/scripts/validate_manifest.py .noru/audit-pack.yml \
  --emit-parsed=.noru/.cache/audit-pack.parsed.json --as-of="$(date -u +%F)"
node    plugins/audit-pack/scripts/collect.mjs --repo=.   # renders the pack
node    plugins/audit-pack/scripts/diff.mjs --repo=.
node    plugins/audit-pack/scripts/push.mjs --repo=. --confirm
```
