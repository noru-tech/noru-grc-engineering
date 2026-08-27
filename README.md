# noru-grc-engineering

> The compliance work that lives in your repo, your CI, or your laptop — done where it lives, and
> landed in Noru with provenance, idempotency and a human review step.

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
| [`noru`](./plugins/noru/) | — | the hub: `connect`, `doctor`, `context` |

Every piece is the same three moves, and exactly three commands:

```
/<piece>:scan   collect locally  → a committed, reviewable .noru/<piece>.yml
/<piece>:diff   show what would change in Noru — reads only, writes nothing
/<piece>:push   land it, once, idempotently, with provenance
```

### What each piece is for

The pieces target deliberately unalike work — which is what makes the contract a contract rather
than a description of one plugin. One hits REST, three hit MCP; they share no collector logic.

- `ai-inventory` targets **ISO 42001 and the EU AI Act**: what an AI register needs, discovered from
  the repository where the answers actually live.
- `evidence-push` targets **whatever your organization's own evidence queue says is unmet** — it
  asks Noru rather than shipping an opinion, so it is not tied to one framework.
- `governance-records` targets the **records of human decisions**: who met, when, what was decided,
  what was assigned to whom. Noru already owns the *authoring* half of governance — policy
  generation, policy lifecycle, approvers and versions — so this piece writes no policy text.
- `review-signoff` targets the recurring **"a human attests to machine output"** pattern: access
  reviews, rule reviews, hardening baselines, asset reconciliation, physical access, vendor reviews.
  Each produces a named, dated, expiring sign-off, and the expiry reaches the record itself.

## Install

### Claude Code

```text
/plugin marketplace add noru-tech/noru-grc-engineering
/plugin install noru@noru-grc-engineering
/plugin install ai-inventory@noru-grc-engineering
/plugin install evidence-push@noru-grc-engineering
/plugin install governance-records@noru-grc-engineering
/plugin install review-signoff@noru-grc-engineering
```

Then configure the Noru MCP connection: [Claude guide](./docs/clients/claude-code.md).

### Codex

```bash
codex plugin marketplace add noru-tech/noru-grc-engineering
codex plugin add noru@noru-grc-engineering
codex plugin add ai-inventory@noru-grc-engineering
```

Then configure Noru MCP: [Codex guide](./docs/clients/codex.md).

Also: [Cursor](./docs/clients/cursor.md) · [generic MCP clients](./docs/clients/generic-mcp.md)

## First run

```text
/noru:connect        # confirm the MCP connection and pick least-privilege scopes
/noru:doctor         # node, python3, git, .gitignore hygiene
/ai-inventory:scan   # → .noru/ai-inventory.yml, reviewed like any other diff
/ai-inventory:diff   # → the exact plan, reads only
/ai-inventory:push   # → writes, after you confirm
```

Commit `.noru/<piece>.yml`. Reviewing it in a pull request is the point: a compliance artifact that
is diffable, versioned and argued about in review is worth more than one assembled the week before
an audit.

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
│   └── review-signoff/                 # :scan :diff :push  (MCP)
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
python3 scripts/test_idempotency.py   # a second push must be a no-op
python3 scripts/contract_test.py      # every plugin satisfies requirements 1-9
```

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
