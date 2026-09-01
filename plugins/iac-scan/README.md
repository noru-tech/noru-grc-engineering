# iac-scan

> Read the Terraform, CloudFormation, Kubernetes and pipeline configuration a repository actually
> contains, decide what each rule that fired means **here**, and land the result in Noru as security
> findings — including closing the ones that stopped firing.

Infrastructure configuration is repo-resident truth. Nothing server-side can read the module that
has not been applied yet, the workflow that runs with the repository's own token, or the literal
somebody left in a variable block. That is the last mile this piece covers.

## The one piece where the contract's target shape is already available

Every other piece in this toolkit lands through an operation with no documented idempotency key, so
it has to probe Noru first and skip what it finds. This one does not.

`POST /v1/security-findings` — and the `createSecurityFinding` tool that fronts it — is documented as
an **idempotent upsert on `source + externalId`**: the same request creates the finding the first
time and updates it every time after. So:

- there is no marker embedded in a description, and no read-before-write probe
- **filing a finding and closing one are the same call** with a different `status`
- re-running is a no-op because the server says so, not because the plugin checked

That is the difference between `client_probe` and `server_upsert` in
[`contract/README.md`](../../contract/README.md#on-requirement-4-honestly), and this piece is the
first one that gets to use the stronger kind.

## Identity, and why it is not the line number

A finding's `externalId` is `<repository slug>:<key>`, and the key is the rule plus a digest of the
**file and the resource** the rule fired against — never the line. Moving a block down a file must
not close one finding and open another; changing what the block says must. Two repositories can
therefore push under one `source` without colliding, and reconciliation only ever touches findings
carrying this repository's own slug.

## A finding is a pointer, never a copy

No matched line text is written into the derived facts, the manifest, or the record that lands in
Noru. One of the bundled rules fires on a line that contains a credential, and a scanner that quoted
what it matched would put that credential into a committed file — and then into a pull request. The
citation is `file:line`; open it there.

## The queue is Noru's

This plugin ships **rules about configuration**. It ships no control ids, no evidence items, no
framework text, and the contract test fails the build if any appear. Everything about the
organization comes from the organization, every scan:

1. `getSecurityFindings` — every finding this piece already has open. This is the half of the queue
   the repository cannot know. A scan that only ever adds is a scan whose register grows for ever;
   knowing what is open is what makes closing possible.
2. `getOrganizationAssets` — the asset register, so a finding attaches to an asset that already
   exists, by that register's own external id. This piece never creates an asset: it cannot tell
   whether the cluster a module describes is the cluster the register already holds.
3. `getOrganizationRisks` — the risk register, so a finding can be filed against a risk the
   organization already carries. This piece never opens a risk.

A manifest may only name an asset or a risk the snapshot returned, and the validator says so.

## What the rules look for

Rules live in [`references/checks.json`](./references/checks.json), which is the only place to add
one. Each says which technology it reads, what property of the configuration it is about, what to do
instead, and a default severity — a *default*, because how bad something is depends on the
environment and only the reviewer knows that.

| Technology | What is read | Examples of what fires |
|---|---|---|
| Terraform | `*.tf` | public object-storage ACLs, ingress open to the whole internet, a managed database published to the internet or declaring no encryption, a credential written in as a literal |
| CloudFormation | templates carrying `AWSTemplateFormatVersion` or `AWS::` resource types | public bucket ACLs, wildcard security-group ingress |
| Kubernetes | manifests carrying `apiVersion` and `kind` | privileged containers, privilege escalation, host namespace sharing, Secret material committed to the repository |
| GitHub Actions | `.github/workflows/*.yml` | actions pinned to a mutable reference, untrusted code checked out in a privileged workflow, `permissions: write-all` |
| GitLab CI | `.gitlab-ci.yml` | a job that turns off transport certificate verification |

Two of the Terraform rules are **absence** checks — they fire on a resource block that declares no
encryption at all. Absence is the interesting half of an infrastructure review and a line scanner
cannot see it, so the collector tracks block extents.

## What it scans

The table above is what each rule *reads*. Which files reach a rule at all is a separate question,
and the answer is **the tracked files, wherever there is a git to ask** — `git ls-files`, the same
set `actions/checkout` gives CI, honouring `.gitignore`, `.git/info/exclude` and your global
excludes file without this collector reimplementing any of them.

That matters more here than for a piece that only counts things. A finding is keyed on the check
and the **file** it fired against, so a gitignored copy of the repository — a worktree, a scratch
checkout, an unpacked archive — does not merely double a number. It opens a *distinct* finding
against a resource at a path that exists on one machine, which this piece then pushes to Noru and
which nobody can close by editing anything. CI scans a checkout and a developer scans a working
tree, so the two disagree permanently.

Three consequences worth knowing. A **tracked** file that an ignore rule also matches is still in
scope — it is in the checkout, so it is configuration this repository ships. An index entry that is
not on disk (a sparse checkout, a pending deletion) is not, because a file the collector cannot open
is not one it can cite. And a `.tf` file you have written but not yet `git add`ed is not scanned
either: it is not in the checkout, so a finding against it would put the same disagreement back in a
smaller form. Stage it and scan again. `vendor/`, `.terraform/` and the rest of the built-in list
stay excluded even when committed — those hold modules someone else wrote.

Scanning something that is **not** a work tree — an exported tarball, a directory with no `.git` —
is a legitimate thing to do, and there the collector reads what is on disk instead. That is a
different question, so it says which one it answered: `coverage.enumerated_by` in the derived facts
is `git` or `walk`, and the scan summary says so in words. Same files either way means the same
`derived_digest`, so an export and a checkout of one commit do not read as drift.

## Expiry, anchored on when the configuration was seen

`interpretation.expires_at` is required, and the horizon is measured from `observed_on` — the commit
date of the state that was scanned — rather than from `decided_at`:

| Rule | Why |
|---|---|
| `expires_at` is **required** | A judgement about a configuration goes stale when the configuration changes, and nothing else in the file says when to look again |
| Measured from `observed_on`, not `decided_at` | A finding observed in March and signed in August is a claim about March's configuration, however recent the signature is. Anchoring on the signature would let an old observation be renewed for ever |
| `open` / `in_progress` / `resolved`: at most 120 days | A live observation the next scan refreshes |
| `accepted` / `false_positive`: at most 400 days | A decision to leave the configuration alone — longer, but never unbounded, because an acceptance nobody revisits is how a known misconfiguration becomes permanent |
| `accepted` and `false_positive` need real reasoning | The judgement most likely to be a shrug is the one that most needs writing down |
| `decided_at` cannot precede `observed_on` | You cannot judge a configuration before you saw it |

`--as-of=YYYY-MM-DD` turns an already-expired judgement into an error. The validator never reads the
clock on its own; that is what keeps it deterministic and keeps the fixtures from rotting.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/iac-scan:scan` | no | Fetches the queue, reads the configuration, writes `.noru/iac-scan.yml` |
| `/iac-scan:diff` | no | Compares against the findings already filed, prints the exact plan including what would be closed |
| `/iac-scan:push` | **yes** | Emits the confirmed MCP calls for the client to execute |

## Scopes

Least privilege. Start read-only.

| Capability | Scopes |
|---|---|
| `:scan` | `read:risks`, `read:assets` |
| `:diff` | the same |
| `:push` | adds `write:risks` |

Security findings live under the risk scopes: the published scope table describes `write:risks` as
"Create/update risks and security findings", and `read:risks` is what listing findings requires.
Authentication is the MCP client's job. This piece never reads, writes or logs a credential.

## Artifact

`.noru/iac-scan.yml`, schema at
[`contract/iac-scan.schema.json`](../../contract/iac-scan.schema.json).

Commit it — it is the reviewable artifact, and the diff of it in a pull request is the record of who
decided what. Keep `.noru/.cache/` out of git.

## Idempotency

| Operation | Kind | Key | Second run |
|---|---|---|---|
| `createSecurityFinding` (file) | `server_upsert` | `source` + `externalId` | skip — nothing about the record would change |
| `createSecurityFinding` (close) | `server_upsert` | `source` + `externalId` | skip — the finding is already `resolved` |

Provenance (`slug`, `commit_sha`, `branch`) travels on every write in `raw`, the field the API
documents as the raw source payload for traceability. It is deliberately **not** part of the
comparison that decides create-versus-update-versus-skip: comparing the commit would make every
commit a write, and "idempotent" would stop meaning anything.

A second `:scan` + `:diff` on unchanged input must produce a plan of all `skip`. If it does not,
that is a bug — `scripts/test_idempotency.py` asserts it.

## Verify

```bash
node    plugins/iac-scan/scripts/collect.mjs --repo=. --output=json
python3 plugins/iac-scan/scripts/validate_manifest.py .noru/iac-scan.yml --as-of="$(date -u +%F)"
node    plugins/iac-scan/scripts/diff.mjs --repo=.
node    plugins/iac-scan/scripts/push.mjs --repo=. --confirm
```
