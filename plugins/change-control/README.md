# change-control

> Who wrote it, who approved it, who merged it, who put it in production — and the forge
> configuration that was supposed to keep those apart. One window, one record, every separation
> that did not hold owned by a named person.

"You cannot author, review and deploy your own code" is the claim. Nothing in a repository proves
it. The facts live in the forge's history and settings: who approved a pull request, whether the
approver was the author, whether branch protection was on, whether an administrator merged past it.
A `git log` shows none of that, and the only path most organizations have is a hand-exported CSV
somebody assembles the week before an audit.

This is the piece that replaces that CSV.

## The manifest records what happened. It does not ask you to pretend.

This is the design decision worth reading before anything else, because it is the opposite of what a
compliance check usually does.

A change that was genuinely self-approved **is** self-approved. Making that a validation error would
mean you cannot commit a truthful manifest, and the way people resolve that is by not running the
tool. So the validator does not refuse the violation. It refuses an **unowned** one:

```
ERROR changes[1].exceptions: priya.nair@example.com approved their own change — that is
      `approver_is_author`, and nothing in this record owns it. This manifest records what
      happened and will not ask you to pretend otherwise; it asks for a disposition and a
      named owner
```

That is [`review-signoff`](../review-signoff/)'s precedent — *every exception needs a disposition and
a named owner* — rather than [`audit-pack`](../audit-pack/)'s `reviewed_by` cannot be `prepared_by`.
The difference is that a workpaper is **authored** and a change history is **observed**. You can
refuse to let someone sign their own workpaper. You cannot refuse to let last quarter have happened.

The check that does bite is the reverse one: an exception recorded for a rule that nothing in the
change triggers is an error too, because a blanket exception written ahead of time is how a control
stops meaning anything.

## The separations it computes

Each is arithmetic on names — lowercase, compare — which is what lets the collector stay
deterministic. Whether a given organization must hold to any of them is **Noru's** queue to answer,
not this plugin's opinion (requirement 9).

| rule | fires when |
|---|---|
| `approver_is_author` | the author is among the people who approved it |
| `merged_without_independent_approval` | nobody other than the author approved it |
| `deployer_is_author` | the person who wrote it also put it in production |
| `agent_change_without_independent_human` | an agent wrote it and the only approver was the person who ran the agent |
| `bypass_used` | branch protection was stepped around: an admin merge, a force push, skipped checks |

### The agent rule, and why it is here

This toolkit is a set of coding-agent plugins. If an agent writes a change and one human approves it
in the same session, that is **one human wearing two hats**, and an auditor will ask. Nothing in a
conventional change-management control catches it, because the forge records a human author on one
side and a human approver on the other and they are different accounts.

So `author_kind: agent` is a first-class field, `agent_operator` is required when it is set, and an
agent-authored change needs an approver who is neither the agent nor whoever pressed go.

Agent authorship on its own is **not** a finding. `pr-1045` in the fixture is agent-written, reviewed
by an independent human, and clean. The rule is about the second pair of eyes, not about who held
the keyboard.

## Why the collector cannot do this alone

Branch protection, required reviewers, environment approvals and the review history are repository
**settings and history** — not files. No offline collector can read them, and contract requirement 2
forbids a collector from opening a socket. So the work splits:

```
export (credentialed, one job)      →  .noru/.cache/change-events.json
collect (offline, deterministic)    →  .noru/change-control.yml
```

The exporter runs where a token exists and writes a **normalized, forge-neutral** file. The collector
reads that file and nothing else, exactly as [`review-signoff`](../review-signoff/) reads its review
queue. One collector serves every forge; a new forge is a new exporter and no change here.

```bash
node plugins/change-control/scripts/export/github.mjs \
  --repo=example-org/example-app --since=2026-07-01 --until=2026-09-30
node plugins/change-control/scripts/export/gitlab.mjs \
  --project=example-org/example-app --since=2026-07-01 --until=2026-09-30
```

Each reads its token from the environment at the point of use — `GITHUB_TOKEN`, `GITLAB_TOKEN` — and
never writes, logs or echoes it, the same arrangement `evidence-push` uses for `NORU_API_KEY`.

**Consequence to be straight about:** on a fork pull request there is no token, so there is no export,
so CI mode reports this piece as `skipped` rather than `pass`. That is the honest ceiling of a check
whose input is an API.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/change-control:scan` | no | Reads the export, computes the separations that did not hold → `.noru/change-control.yml`, every violation flagged `needs_review: true` |
| `/change-control:diff` | no | Probes existing findings and evidence, prints the exact plan |
| `/change-control:push` | **yes** | Files each owned exception as a security finding and the window as evidence |

A manifest carrying any `needs_review: true` cannot be pushed. The collector proposes; a person
decides.

## Expiry, anchored on the window

`expires_at` is required and measured from the **end of the window**, following `audit-pack`: an
account of July, signed in December, does not cover more of July for being signed late.

| Rule | Why |
|---|---|
| `expires_at` is required | An account of a period that never lapses is one nobody will renew |
| Must be **after** the window closes | A record that expires inside its own period never asserted anything |
| At most **400 days** after the window closes | Roughly the next annual cycle |
| At most **120 days** where any exception is `deferred` | A separation nobody has fixed yet is not something to sign off for a year |
| `decided_at` cannot precede the window's end | A conclusion about a period cannot be drawn while the period is still running |

Nothing here reads the clock — that is what keeps the fixtures from rotting. Staleness is CI mode's
job, in the expiry step.

## What else the validator insists on

| Rule | Why |
|---|---|
| An agent-authored change names its operator | Somebody pressed go, and they are not an independent reviewer of what came back |
| A bypass names a kind and a reason | A bypass nobody wrote down is indistinguishable from a control that held |
| A `remediated` exception names the day it was put right | Otherwise it is a deferred one wearing a better word |
| Approvals cannot predate the change; deploys cannot predate the merge | A timeline that cannot have happened is a record nobody should rely on |
| Every change falls inside the declared window | A record filed under the wrong quarter covers neither |
| `window.complete: false` is a **warning**, surfaced | A partial export means absence is not evidence of absence, and an auditor must be told rather than left to infer it from a round number |

The `controls` block is checked more gently on purpose: an unprotected default branch, zero required
approvals, admins exempt from protection and force-push allowed are all **warnings**. This piece does
not get to decide that an organization must require two approvals. It reports what a reviewer will be
asked about.

## Scopes

Least privilege. Start read-only.

| Capability | Scopes |
|---|---|
| `:scan` | `read:organization`, `read:controls`, `read:evidence`, `read:risks` |
| `:diff` | the same |
| `:push` | adds `write:evidence`, `write:risks` |

The exporters need a **forge** token, not a Noru one, and the least-privileged form of it: read
access to pull requests, reviews and repository administration-read for the settings. Authentication
to Noru remains the MCP client's job. This piece never reads, writes or logs a Noru credential.

## Artifact

`.noru/change-control.yml`, schema at
[`contract/change-control.schema.json`](../../contract/change-control.schema.json).

Commit it — it is the reviewable artifact. Keep `.noru/.cache/` out of git, and think hard about the
export: it is a list of who reviewed what and when, which is personal data about your colleagues.
The manifest that cites it is the thing that belongs in the repository.

Because the export lives in `.noru/.cache/`, the `refs[]` in a committed manifest point at a file
that is not committed. CI mode reports that as `dangling_ref`, which is advisory by default and the
correct outcome: the citation says where the claim came from, and the record it came from is
deliberately not in git.

## Idempotency

| Operation | Transport | Kind | Key |
|---|---|---|---|
| `createSecurityFinding` | MCP | `server_upsert` | `(source, externalId)` |
| `createEvidence` | MCP | `server_key` | content-addressed `idempotencyKey` |
| `linkEvidenceToControl` | MCP | `server_dedupe` | evidence + control + evidence item |

Each exception becomes one security finding keyed on `(source, externalId)`, which is a documented
server-side upsert — so re-running the piece after an exception is remediated **closes** the finding
with the same call that filed it, exactly as `iac-scan` does. The window's attestation is an evidence
record with a stable server key. Its description marker remains the older-server fallback recorded
in [`piece.json`](./piece.json).

## Verify

```bash
node    plugins/change-control/scripts/collect.mjs --repo=. --output=json
python3 plugins/change-control/scripts/validate_manifest.py .noru/change-control.yml
node    plugins/change-control/scripts/diff.mjs --repo=.
node    plugins/change-control/scripts/push.mjs --repo=. --confirm
```

Exit codes: `0` success · `1` drift, validation failure, a missing export, or a stale plan · `2`
usage, including a push without `--confirm`.
