# findings-ingest

> Read the SARIF and OSV-Scanner reports your CI produces and does **not** upload anywhere, decide
> what each result means **here**, and land the result in Noru as security findings filed against
> risks the organization already carries — including closing the ones no input reports any more.

Some scanner output never leaves the pipeline that produced it: a SAST run whose SARIF is kept only
as a build artifact, an OSV-Scanner run over a lockfile, a container or secret scan nobody uploads.
Nothing server-side can see those results. That is the last mile this piece covers, and the only one.

## Only what nothing else imports

A report uploaded to a forge alert store — code scanning, dependency alerts, a merge request
security report — is already visible to anything that imports that store. If your Noru organization
has an integration that imports it, ingesting the same report here files every result a second time,
under a second `source`, and the two sets never reconcile: one closes when the forge says so, the
other when this piece stops seeing it.

So every input in `.noru/findings-ingest.inputs.json` declares `reaches_forge_alert_store: false`,
and both the collector and the validator refuse an input that does not. The flag is a statement
somebody makes after checking, not a default:

- if the report is uploaded to the forge's alert store, leave it out
- if the report is not uploaded but an integration already brings the same tool's results into
  Noru some other way, leave it out too — the flag cannot see that, and you can
- otherwise, declare it

Do not double-ingest what an integration already imports. Check what your organization's
integrations bring in before adding an input.

## Inputs

`.noru/findings-ingest.inputs.json` is committed, and is JSON so the offline collector needs no YAML
parser:

```json
{
  "inputs": [
    {
      "id": "semgrep",
      "path": ".noru/.cache/findings/semgrep.sarif",
      "format": "sarif",
      "class": "code",
      "reaches_forge_alert_store": false
    },
    {
      "id": "osv",
      "path": ".noru/.cache/findings/osv-scanner.json",
      "format": "osv",
      "class": "dependency",
      "reaches_forge_alert_store": false
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | Lowercase handle. Every finding key starts with it, so two reports can never address the same finding. Renaming it closes and reopens everything the input produced |
| `path` | Where your CI writes the report, inside the repository. Under `.noru/.cache/` keeps it out of git |
| `format` | `sarif` (SARIF 2.1.0, from any tool that writes it) or `osv` (OSV-Scanner's `--format json`) |
| `class` | `code`, `dependency`, `container`, `secret` or `configuration`. Every finding carries its input's class, and `links.risk_by_class` files a whole class against one risk |
| `reaches_forge_alert_store` | Must be `false`. See above |

The scanners are yours, run by your CI before the collector. A report that is missing is an error,
never an empty result: reading nothing would close every finding the input ever produced the first
time a scanner step failed. Run scanners from the repository root; a path a scanner reports is
resolved against this checkout, and a result in a file that is not in it is counted and not filed.

## Identity, and why it is not the line number

A finding's `externalId` is `<repository slug>:<key>`, and the key is the input id, the rule and a
digest of:

- for SARIF: the tool, the rule, the **file** and the **logical location** (the function or class
  the result is in, when the tool reports one)
- for OSV: the advisory, the **lockfile** and the **package**

Never the line, and never a fingerprint the scanner computed from the line's content. Moving code
down a file must not close one finding and open another, and a content hash would turn every edit
near a problem into a new problem. One rule firing three times in one function is therefore one
finding with three citations, which is one thing to fix.

An advisory is known by an id that is a record in its own right, so an alias published later does
not change which id a finding carries. A scanner upgrade that renames its rules does close and
reopen findings; the scan output names each input's tool and version, so the cause is visible.

## A finding is a pointer, never a copy

No result message, snippet or matched text is read into the derived facts, the manifest, or the
record that lands in Noru. A secret scanner's message can contain the secret it found, and a
collector that quoted it would put that secret into a committed file and then into a pull request.
Titles come from the rule's own description, written by whoever wrote the rule; for an advisory, from
its published summary. The citation is `file:line`; open it there.

## The queue is Noru's

This plugin ships **formats and classes**. It ships no control ids, no evidence items, no framework
text, no remediation windows and no deadlines. Everything about the organization comes from the
organization, every scan:

1. `getSecurityFindings` — every finding this piece already has open. A result that stopped
   appearing has left a finding open somewhere; knowing what is open is what makes closing possible.
2. `getOrganizationAssets` — the asset register, so a finding attaches to an asset that already
   exists. This piece never creates an asset.
3. `getOrganizationRisks` — the risk register. A whole class (`links.risk_by_class`) or one finding
   (`risk_id`) may be filed against a risk the organization already carries. The validator refuses
   any risk that is not in the snapshot, and this piece never opens one.
4. `getRiskTreatments` — read only for due dates, recorded per risk as `due_on`.

## Deadlines are Noru's too

A finding carries **no due date**, and the validator refuses one. The only deadline this piece knows
is the due date of the linked risk's treatment, as Noru holds it; `--as-of` reports an open finding
that is past it. If no treatment carries a due date, findings have none. This piece never computes a
remediation window from a severity, and nothing it writes to Noru contains a date to be met.

The expiry below is not a deadline. It says when a person has to look at the *judgement* again.

## Expiry, anchored on when the result was observed

`interpretation.expires_at` is required, and the horizon is measured from `observed_on` — the commit
date of the state the scanners looked at — rather than from `decided_at`:

| Rule | Why |
|---|---|
| `expires_at` is **required** | A judgement about a result goes stale when the code changes, and nothing else in the file says when to look again |
| Measured from `observed_on`, not `decided_at` | A result observed in March and signed in August is a claim about March's code |
| `open` / `in_progress` / `resolved`: at most 120 days | A live observation the next scan refreshes |
| `accepted` / `false_positive`: at most 400 days | A decision to leave the code alone — longer, but never unbounded |
| `accepted` and `false_positive` need real reasoning | The judgement most likely to be a shrug is the one that most needs writing down |
| A result suppressed in the source is proposed as `accepted` | Somebody already decided to leave it. The manifest is where that decision gets a name, a reason and an end |

`--as-of=YYYY-MM-DD` turns an already-expired judgement into an error. The validator never reads the
clock on its own.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/findings-ingest:scan` | no | Fetches the queue, reads the declared reports, writes `.noru/findings-ingest.yml` |
| `/findings-ingest:diff` | no | Compares against the findings already filed, prints the exact plan including what would be closed |
| `/findings-ingest:push` | **yes** | Emits the confirmed MCP calls for the client to execute |

## Scopes

Least privilege. Start read-only.

| Capability | Scopes |
|---|---|
| `:scan` | `read:risks`, `read:assets` |
| `:diff` | adds `read:organization` to bind the plan to its target |
| `:push` | adds `write:risks` |

Security findings, risks and risk treatments live under the risk scopes: the published scope table
describes `write:risks` as "Create/update risks and security findings". Authentication is the MCP
client's job. This piece never reads, writes or logs a credential.

## Artifact

`.noru/findings-ingest.yml`, schema at
[`contract/findings-ingest.schema.json`](../../contract/findings-ingest.schema.json).

Commit it and `.noru/findings-ingest.inputs.json`. Keep `.noru/.cache/`, and the reports in it, out
of git.

## Idempotency

| Operation | Kind | Key | Second run |
|---|---|---|---|
| `createSecurityFinding` (file) | `server_upsert` | `source` + `externalId` | skip — nothing about the record would change |
| `createSecurityFinding` (close) | `server_upsert` | `source` + `externalId` | skip — the finding is already `resolved` |

Provenance (`slug`, `commit_sha`, `branch`) and the input, tool and rule travel on every write in
`raw`. They are deliberately **not** part of the comparison: comparing the commit would make every
commit a write.

**Closed means not reported, not fixed.** A finding is closed when no declared input reports it at
the commit scanned. A renamed rule, a narrowed scanner configuration or an input removed from the
inputs file closes findings too. The close says which.

A second `:scan` + `:diff` on unchanged input must produce a plan of all `skip`. If it does not,
that is a bug — `scripts/test_idempotency.py` asserts it.

## In CI

Run the scanners first, writing to the paths the inputs file names, then the piece:

```bash
semgrep scan --sarif --output .noru/.cache/findings/semgrep.sarif
osv-scanner scan source --format json --output .noru/.cache/findings/osv-scanner.json . || [ $? -eq 1 ]
node    plugins/findings-ingest/scripts/collect.mjs --repo=. --check --output=json
```

OSV-Scanner exits 1 when it finds something; that is a result, not a failure, and the collector is
what decides whether the manifest is current. Any other non-zero exit still fails the step.

## Verify

```bash
node    plugins/findings-ingest/scripts/collect.mjs --repo=. --output=json
python3 plugins/findings-ingest/scripts/validate_manifest.py .noru/findings-ingest.yml --as-of="$(date -u +%F)"
node    plugins/findings-ingest/scripts/diff.mjs --repo=.
node    plugins/findings-ingest/scripts/push.mjs --repo=. --confirm
```
