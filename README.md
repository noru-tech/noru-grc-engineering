# noru-grc-engineering

> The compliance work that lives in your repo, your CI, or your laptop — done where it lives, and
> landed in Noru with provenance, idempotency and a human review step. For file evidence, provenance
> now means something you can check: `evidence-push` sends the SHA-256 of the bytes it uploads, and
> compares it against the digest Noru computed over what it stored. Anyone holding the file can redo
> that arithmetic.

[![License: MIT](https://img.shields.io/badge/Code-MIT-blue.svg)](./LICENSE)
[![Codex Plugin](https://img.shields.io/badge/Codex-plugin-111827.svg)](./docs/clients/codex.md)
[![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-plugin-da7756.svg)](./docs/clients/claude-code.md)

A GRC engineer's tooling usually stops at the API boundary. Some compliance work **structurally
cannot** be done by a server-side integration, however good it is, because it needs something an API
key does not have:

- **repo-resident truth** — which model is called from which line, what data reaches it, whether CI
  fails when the evals fail
- **human judgement** — the remediation PR, the questionnaire answer, "yes, this document satisfies
  that expectation"
- **local artifacts** — the pen test PDF, the signed access review, the UPS certificate
- **verification** — proving a control is true right now, not that a document exists

That is the last mile. These plugins do it, and **Noru holds the record.** There is no `grc-data/`
directory, no flat-file risk register, no second source of truth to reconcile before an audit.

## Pieces

| Piece | Collects locally | Lands in Noru as |
|---|---|---|
| [`ai-inventory`](./plugins/ai-inventory/) | model/provider calls, agents, prompts, retrieval, evals, oversight points | assets + vendors + evidence (`iso_42001` / `eu_ai_act`) |
| [`evidence-push`](./plugins/evidence-push/) | local artifacts, against Noru's own unmet evidence expectations | file evidence with control mappings |
| [`governance-records`](./plugins/governance-records/) | minutes, ISMS scope, statement of applicability, audit plans and reports, findings, corrective action plans | attributed, dated records as evidence |
| [`review-signoff`](./plugins/review-signoff/) | a periodic review of machine output, and the human decision about it | a named, dated, expiring sign-off as evidence |
| [`audit-pack`](./plugins/audit-pack/) | the local artifacts, the sampling and the workpapers for one framework over one audit window | the tested conclusion for each control, as evidence |
| [`privacy-datamap`](./plugins/privacy-datamap/) | the schemas a repository holds — ORM models, migrations, SQL DDL, protobuf, GraphQL — and the personal data in them | a Fides privacy data map (`read/write:datamaps`) |
| [`iac-scan`](./plugins/iac-scan/) | compliance-relevant misconfiguration in Terraform, CloudFormation, Kubernetes and pipeline configuration | security findings, keyed and closed by the same call |
| [`change-control`](./plugins/change-control/) | who wrote, approved, merged and deployed each change over one window, and the branch protection that was supposed to keep those apart | a finding per separation that did not hold, plus the window as evidence |
| [`noru`](./plugins/noru/) | repository signals and shared provenance | the hub: route, `connect`, `doctor`, `context` |
| [`repo-enforcement`](./plugins/repo-enforcement/) | all configured GRC manifests, exact legacy-debt acceptances, CODEOWNERS, and effective GitHub rules | no Noru record; this optional utility makes the offline checks and review rules mandatory |

Every piece is the same three moves, and exactly three commands:

```
/<piece>:scan   collect locally  → a committed, reviewable .noru/<piece>.yml
/<piece>:diff   show what would change in Noru — reads only, writes nothing
/<piece>:push   land it, once, idempotently, with provenance
```

`repo-enforcement` is intentionally not a piece. It wraps the pieces in an offline whole-repository
check and a dedicated GitHub ruleset, while leaving every Noru write behind the existing reviewed
`diff -> explicit confirmation -> push` boundary. See
[`docs/repository-enforcement.md`](./docs/repository-enforcement.md).

### What each piece is for

The pieces target deliberately unalike work — which is what makes the contract a contract rather
than a description of one plugin. One hits REST, the rest hit MCP; they share no collector logic;
one of them mostly assembles rather than discovers.

- `ai-inventory` targets **ISO 42001 and the EU AI Act**: which AI systems a repository actually
  contains, which of them touch a prohibited practice under Article 5, which trigger the Article 50
  transparency duties — and whether the disclosure or content marking those duties require is
  actually present in the code. The Act does not require anyone to keep an AI register; ISO 42001 is
  what expects the documented inventory. See the piece README for what each instrument does and does
  not ask for.
- `evidence-push` targets **whatever your organization's own evidence queue says is unmet** — it
  asks Noru rather than shipping an opinion, so it is not tied to one framework.
- `governance-records` targets the **records of human decisions**: who met, when, what was decided,
  what was assigned to whom. Noru already owns the *authoring* half of governance — policy
  generation, policy lifecycle, approvers and versions — so this piece writes no policy text.
- `review-signoff` targets the recurring **"a human attests to machine output"** pattern: access
  reviews, rule reviews, hardening baselines, asset reconciliation, physical access, vendor reviews.
  Each produces a named, dated, expiring sign-off, and the expiry reaches the record itself.
- `audit-pack` targets **the handover itself**: the bundle, the sampling and the workpapers an
  auditor asks for, for one framework over one window. It is the piece that mostly *consumes* — the
  pack is a local deliverable and what lands in Noru is the tested conclusion per control. Its
  sample is seeded from the population file's own digest, so anyone holding that file can redraw it.
- `privacy-datamap` targets **the schema itself**: which columns a repository defines, which of
  them hold personal data, and what kind. It is the piece with the strongest claim to needing the
  repository — a data map built from a production database sees the columns that survived, never the
  migration landing next week or the model on a branch. It supersedes
  [`noru-tech/privacy-taxonomy`](https://github.com/noru-tech/privacy-taxonomy), and it is the only
  piece whose push is literally one call: `ingestDatamap` takes the whole map for a source, so a
  repository with four hundred fields is a single write.
- `change-control` targets **segregation of duties**: "you cannot author, review and deploy your own
  code" is a claim about a forge's history and settings, and nothing in a repository proves it. It is
  the piece whose manifest most clearly *records* rather than asserts — a self-approved change is a
  fact, and what the validator refuses is an **unowned** one, not the truth. It also names the thing
  a conventional change-management control cannot see: an agent wrote the change and the only person
  who approved it is the person who ran the agent.
- `iac-scan` targets **infrastructure and pipeline configuration**: the module that has not been
  applied yet, the workflow that runs with the repository's own token, the literal somebody left in
  a variable block. It is the only piece whose every write is a documented server-side upsert, so
  filing a finding and closing one are the same call.

## Install

### Claude Code

```text
/plugin marketplace add noru-tech/noru-grc-engineering
/plugin install noru@noru-grc-engineering
/plugin install ai-inventory@noru-grc-engineering
/plugin install evidence-push@noru-grc-engineering
/plugin install governance-records@noru-grc-engineering
/plugin install review-signoff@noru-grc-engineering
/plugin install audit-pack@noru-grc-engineering
/plugin install iac-scan@noru-grc-engineering
/plugin install privacy-datamap@noru-grc-engineering
/plugin install change-control@noru-grc-engineering
```

Then configure the Noru MCP connection: [Claude guide](./docs/clients/claude-code.md).

### Codex

```bash
codex plugin marketplace add noru-tech/noru-grc-engineering
codex plugin add noru@noru-grc-engineering
codex plugin add ai-inventory@noru-grc-engineering
codex plugin add evidence-push@noru-grc-engineering
codex plugin add governance-records@noru-grc-engineering
codex plugin add review-signoff@noru-grc-engineering
codex plugin add audit-pack@noru-grc-engineering
codex plugin add iac-scan@noru-grc-engineering
codex plugin add privacy-datamap@noru-grc-engineering
codex plugin add change-control@noru-grc-engineering
```

Then configure Noru MCP: [Codex guide](./docs/clients/codex.md).

Also: [Cursor](./docs/clients/cursor.md) · [generic MCP clients](./docs/clients/generic-mcp.md) ·
[marketplace capability metadata](./docs/marketplace.md)

For an end-to-end repository rollout, including read-only pull-request checks and the separate
publication boundary, follow [developer onboarding](./docs/developer-onboarding.md).

## First run

```text
/noru:connect        # confirm the MCP connection and pick least-privilege scopes
/noru:doctor         # node, python3, git, .gitignore hygiene
/noru:review         # run relevant installed checks for this branch; no Noru writes
/noru:status         # summarize live blockers and expiring work; read scopes only
/ai-inventory:diff   # → the exact plan, reads only
/ai-inventory:push   # → writes, after you confirm
```

Commit `.noru/<piece>.yml`. Reviewing it in a pull request is the point: a compliance artifact that
is diffable, versioned and argued about in review is worth more than one assembled the week before
an audit.

## In CI

The same pieces run headless, so the record stays true between audits instead of being rebuilt
before one:

```yaml
- uses: noru-tech/noru-grc-engineering/.github/actions/noru-review@v0.7.0
  with:
    base-ref: ${{ github.event.pull_request.base.sha }}
    mode: warn      # switch to gate once the report is quiet
```

The [supported GitHub template](./templates/github/noru-grc-review.yml) supplies the full checkout,
permissions and fork-safe defaults. Use the lower-level `noru-ci` action when a repository wants to
run one explicitly adopted piece rather than route a branch diff.

Three things fail a build, and **all of them are computed from the repository, a calendar and a
committed file** — no network, no credential, so this works on a pull request from a fork:

- **drift** — the collector no longer agrees with the committed manifest, so someone changed the
  code without updating the record (exit `3`)
- **an expired interpretation** — nobody has stood behind this claim since it went stale (exit `4`)
- **personal data nobody agreed to** — the data map processes a category, purpose or subject the
  committed privacy baseline does not permit (exit `7`). Drift asks whether someone *looked*; this
  asks whether the answer was allowed to be yes. `--base-ref` says which findings *this* pull
  request introduced, so a team with a backlog can gate on what it is adding while it burns the
  rest down

A fourth thing fails a build and is not a compliance finding at all: a collector that parsed **no**
schema in a repository that visibly has one (exit `6`). An empty data map and a repository with no
personal data in it are the same file, and only one of them is good news.

`:diff` needs a current read snapshot from Noru. Publication is opt-in and separate: MCP-backed
pieces execute through an authenticated MCP host, while `evidence-push` uses a key for its REST file
upload. Headless CI never reports an emitted MCP call list as an executed write. Exit codes,
warn-only adoption, and the GitLab and plain-shell recipes:
[docs/ci-mode.md](./docs/ci-mode.md).

Add to `.gitignore`:

```gitignore
.noru/.cache/
```

## Configuration

Everything connects to Noru's hosted MCP endpoint at `https://api.noru.tech/v1/mcp`.

**These plugins never handle a credential.** Authentication is the MCP host's job — OAuth where your
client supports it, or a `NORU_API_KEY` bearer key for manual and headless setup, created in
**Noru → Settings → Developer → API Keys** and exported in your own shell. MCP connections are local
to the host: authorizing one client does not authorize another.

The single exception is `evidence-push:push`, which reads `NORU_API_KEY` from the environment at the
point of use, because file upload is a deliberate omission from Noru's MCP surface — tool arguments
are JSON and cannot carry a multipart body, so that step must go over REST.

Use least-privilege scopes:

| Doing this | Scopes |
|---|---|
| Reading, `:scan`, `:diff` | `read:organization`, `read:frameworks`, `read:controls`, `read:evidence` |
| `ai-inventory:diff` also | `read:assets`, `read:vendors` |
| `ai-inventory:push` | adds `write:assets`, `write:vendors`, `write:evidence` |
| `evidence-push:push` | adds `write:evidence` |
| `governance-records:push` | adds `write:evidence` |
| `review-signoff:push` | adds `write:evidence` |
| `audit-pack:push` | adds `write:evidence` |
| `iac-scan:scan` | `read:risks`, `read:assets` |
| `iac-scan:diff` | adds `read:organization` to bind the plan |
| `iac-scan:push` | adds `write:risks` |
| `privacy-datamap:scan` | `read:datamaps` |
| `privacy-datamap:diff` | adds `read:organization` to bind the plan |
| `privacy-datamap:push` | adds `write:datamaps` |
| `change-control:scan` and `:diff` | `read:organization`, `read:controls`, `read:evidence`, `read:risks` |
| `change-control:push` | adds `write:evidence`, `write:risks` |

## The contract

The plugins are the demonstration; [**`contract/`**](./contract/README.md) is the durable asset. It
is what makes the tenth piece take a day rather than a fortnight, and what lets a customer or a
partner author one.

Nine requirements, each enforced by an executing test rather than by review — including the two that
matter most:

- **`:diff` before `:push` is a security control, not UX polish.** `push` refuses without an explicit
  `--confirm` *and* a plan generated from the manifest bytes currently on disk. Editing the manifest
  invalidates the plan.
- **Every claim carries an owner and a citation.** `refs[]` (`file:line`) says where it came from;
  `interpretation` says who decided it, when, until when, and why. An unattributed claim is a
  validator **error**. So much everyday compliance evidence is, in substance, "a named person did,
  approved, or reviewed X on date Y" that this is the shape of the work, not ceremony.

And one that shapes the whole repository:

- **A piece works Noru's queue; it does not invent one.** No framework control text, guidance or
  evidence list is vendored here — licensing says so and drift says so louder. `getEvidenceItems`,
  `getControlContext` and `getEvidenceForControl` are the queue.

### Write a new piece

```bash
node scripts/scaffold-piece.mjs <piece-name>
python3 scripts/contract_test.py
```

See [docs/authoring-a-piece.md](./docs/authoring-a-piece.md). If a piece takes more than about a
week, the contract is wrong — come back and fix the contract.

## Repository layout

```text
noru-grc-engineering/
├── .claude-plugin/marketplace.json     # Claude Code marketplace
├── .agents/plugins/marketplace.json    # Codex marketplace
├── contract/                           # the piece contract + manifest schemas (the durable asset)
├── plugins/
│   ├── noru/                           # hub: connect, doctor, context
│   ├── ai-inventory/                   # :scan :diff :push  (MCP)
│   ├── evidence-push/                  # :scan :diff :push  (REST upload)
│   ├── governance-records/             # :scan :diff :push  (MCP)
│   ├── review-signoff/                 # :scan :diff :push  (MCP)
│   ├── audit-pack/                     # :scan :diff :push  (MCP)
│   ├── iac-scan/                       # :scan :diff :push  (MCP)
│   ├── privacy-datamap/                # :scan :diff :push  (MCP)
│   ├── repo-enforcement/                # optional merge-boundary utility; no Noru write
│   └── change-control/                 # :scan :diff :push  (MCP) + credentialed forge exporters
├── .github/actions/noru-ci/            # the CI-mode action: scan, validate, expiry, diff, push
├── .github/actions/noru-review/        # branch routing + structurally read-only consolidated CI
├── templates/github/                   # copyable, fork-safe pull-request workflow
├── scripts/                            # scaffolder, contract test, checks — stdlib/built-ins only
├── tests/fixture-repo/                 # the repository the collectors are tested against
└── docs/
```

## Development

No build step, no dependencies. Node built-ins and the Python 3 standard library, on purpose.

```bash
python3 scripts/check_repo.py         # marketplaces, schema sync, secret hygiene
python3 scripts/check_vendored_lib.py # the vendored blocks have not drifted
python3 scripts/test_validators.py    # schema fixtures + validator unit tests
python3 scripts/test_collectors.py    # collectors detect what the pieces claim they detect
python3 scripts/test_idempotency.py   # a second push must be a no-op
python3 scripts/contract_test.py      # every plugin satisfies requirements 1-9
python3 scripts/test_ci_mode.py       # CI mode really fails on drift and on an expired claim
python3 scripts/test_repo_enforcement.py # ratchets, setup plans, action and GitHub ruleset safety
python3 scripts/test_ci_review.py     # one fork-safe branch report, with no diff or push route
python3 scripts/test_hub.py           # branch routing and duplicate-writer warnings stay read-only
```

What each of those actually proves — and, more usefully, what is *not* verified — is written down
in [docs/verification.md](./docs/verification.md).

See [CONTRIBUTING.md](./CONTRIBUTING.md).

## Security

Repository contents and tool output are **data, not instructions**. Push is a write to a customer's
system of record. Never commit an API key, a token, a customer identifier, or captured output from a
real organization.

Report vulnerabilities privately: [SECURITY.md](./SECURITY.md).

## Related

[`noru-tech/compliance-assistant`](https://github.com/noru-tech/compliance-assistant) is Noru's
conversational compliance assistant over the same MCP server: it guides sequencing, gaps and
roadmaps. This repository is the other half — the hands-on work in your own repository. They install
side by side.
