# threat-model

> Read the threat model a repository commits in the Open Threat Model format, decide what each
> threat means **here**, and land it in Noru's risk register linked to the organization's own
> controls — including resolving the threats the model no longer names, and flagging the components
> other pieces found that the model leaves out.

A threat model is design-time risk assessment, and it goes stale the way design documents do: the
system grows a datastore, a model provider or a bucket, and nobody adds it. This piece keeps the
model, the register and the code in one loop.

## The format: Open Threat Model

The piece reads [Open Threat Model](https://github.com/iriusrisk/OpenThreatModel) (OTM, CC BY-SA
4.0): trust zones, components, dataflows, threats and mitigations, each with a stable id, and a
JSON Schema. It was chosen over a single tool's own format because it is vendor-neutral and several
threat modelling tools export it, so a team keeps the tool it likes and commits what it exports.

Commit the model as JSON, named `*.otm.json`, anywhere in the repository. The collector has no
dependencies and therefore no YAML parser; a model kept in YAML is converted once
(`python3 -c 'import json,sys,yaml; json.dump(yaml.safe_load(sys.stdin), sys.stdout, indent=2)' < model.otm.yaml > model.otm.json`).
An invalid model — a threat referenced but not defined, an id defined twice — stops the scan and
says where.

## One risk per threat, per element

A threat in OTM is attached to components and dataflows. "SQL injection" on the API and on the admin
console are two risks with two owners, so the piece proposes one risk per threat on each element it
is attached to. A risk is keyed on the model's project, the element and the threat — never on a
position in the file — so reordering the model keeps every risk's identity; only its citations, JSON
pointers into the file, move with it.

The skeleton proposes a likelihood and an impact from the model's 0–100 scores, in five equal bands
(`rare` … `certain`, `negligible` … `catastrophic`), and a status from the threat's state in the
model (`mitigated` → `monitoring`, anything else → `identified`). Proposals only: the person who owns
the risk decides, and says why when they differ.

## Undeclared components

When ai-inventory, privacy-datamap or iac-scan have run in the same repository, their derived facts
are read too. Every provider and vector store (ai-inventory), dataset and system (privacy-datamap)
and resource with a finding (iac-scan) that no component of the model names is reported as
**undeclared**. A component names one when its id, name or a tag equals it once normalized (lowercase,
every other run of characters as `-`), or a tag reads `signal:KIND:KEY` exactly. Nothing fuzzier: a
guess that a component *is* the dataset would hide the one that is missing.

An undeclared component is added to the model (re-scan, and it is no longer reported) or decided
`out_of_scope` with a reason and an expiry. The validator refuses anything else.

## The queue is Noru's

This plugin ships no threat library, no control catalogue and no mitigations. Everything about the
organization comes from the organization:

1. `getOrganizationRisks` — the risks this piece filed before, found by a marker on the first line
   of their description, so a re-scan knows what is in the register and what the model dropped.
2. `getOrganizationControls` — the controls a risk may be linked to. A manifest may only name a
   control the snapshot returned.
3. `getRiskControlMappings` — an existing risk's links, read interactively, so a control added to an
   existing risk is linked once.

## Expiry, anchored on when the model was seen

`interpretation.expires_at` is required and measured from `observed_on`, the commit date of the
model it judges. A judgement in `identified`, `assessing`, `mitigating` or `monitoring` may stand
180 days; `accepted` and `resolved` 365, and accepting a risk needs its reasoning written down. An
undeclared component decided out of scope may stand 365 days. `--as-of=YYYY-MM-DD` turns an expired
judgement into an error. The expiry is when somebody looks again, not when the risk is due: a due
date is the risk's own, in Noru.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/threat-model:scan` | no | Fetches the queue, reads the model and other pieces' facts, writes `.noru/threat-model.yml` |
| `/threat-model:diff` | no | Compares against the risks already filed, prints the exact plan including what would be resolved |
| `/threat-model:push` | **yes** | Emits the confirmed MCP calls for the client to execute |

## Scopes

Least privilege. Start read-only.

| Capability | Scopes |
|---|---|
| `:scan` | `read:risks`, `read:controls` |
| `:diff` | adds `read:organization` to bind the plan to its target |
| `:push` | adds `write:risks` (it covers creating, updating and linking risks) |

Authentication is the MCP client's job. This piece never reads, writes or logs a credential.

## Artifact

`.noru/threat-model.yml`, schema at
[`contract/threat-model.schema.json`](../../contract/threat-model.schema.json). Commit it with the
model; keep `.noru/.cache/` out of git.

## Idempotency

| Operation | Kind | Key | Second run |
|---|---|---|---|
| `createRisk` | `client_probe` | `externalId` | skip — the probe finds the risk |
| `updateRisk` | `client_probe` | `riskId` | skip — every field it writes already matches |
| `linkRiskToControl` | `client_probe` | `riskId` + `controlId` | skip — the link exists |

No upsert on a risk's externalId is documented, so the piece reads first. Noru does document that an
externalId is unique within the organization, so a create the probe should have caught is refused
rather than filed twice. A new risk is linked to its controls in the same push: the link takes the
id the create returns. An existing risk's links are compared only when the snapshot carries them
(the interactive diff reads `getRiskControlMappings`); a CI job cannot read them per risk, so it links
only the risks it creates. Links are never removed.

**Resolved means no longer modelled, not treated.** A risk this repository filed that the model no
longer names is set to `resolved`, with a note saying why.

A second `:scan` + `:diff` on unchanged input must produce a plan of all `skip`.
`scripts/test_idempotency.py` asserts it.

## Verify

```bash
node    plugins/threat-model/scripts/collect.mjs --repo=. --output=json
python3 plugins/threat-model/scripts/validate_manifest.py .noru/threat-model.yml --as-of="$(date -u +%F)"
node    plugins/threat-model/scripts/diff.mjs --repo=.
node    plugins/threat-model/scripts/push.mjs --repo=. --confirm
```
