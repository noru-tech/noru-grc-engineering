# The last-mile piece contract

A **piece** is one kind of compliance work that lives in a repository, on a laptop, or in CI —
work a server-side integration structurally cannot do, because it needs repo-resident truth,
human judgement, a local artifact, or live verification.

Every piece does the same three moves:

```
collect locally  →  validate against a bundled vocabulary  →  push once, idempotently, with provenance
```

Noru holds the record. A piece never becomes a second register.

This directory is the durable asset. Plugins come and go; the contract is what makes the
tenth piece take a day instead of a fortnight, and what lets a customer or partner author one.

- [`piece.schema.json`](./piece.schema.json) — the declaration every piece ships at
  `plugins/<piece>/piece.json`
- [`ai-inventory.schema.json`](./ai-inventory.schema.json) — the `.noru/ai-inventory.yml` artifact
- [`evidence-push.schema.json`](./evidence-push.schema.json) — the `.noru/evidence-push.yml` artifact
- [`governance-records.schema.json`](./governance-records.schema.json) — the
  `.noru/governance-records.yml` artifact
- [`review-signoff.schema.json`](./review-signoff.schema.json) — the `.noru/review-signoff.yml`
  artifact
- [`audit-pack.schema.json`](./audit-pack.schema.json) — the `.noru/audit-pack.yml` artifact
- [`iac-scan.schema.json`](./iac-scan.schema.json) — the `.noru/iac-scan.yml` artifact

## The nine requirements

| # | Requirement | Enforced by |
|---|---|---|
| 1 | `.claude-plugin/plugin.json` + one skill + three commands: `:scan`, `:diff`, `:push` | `contract_test.py::check_item_1` — manifest parses, `piece.json.skill` and all three `commands` exist on disk, command frontmatter names match |
| 2 | A **collector** producing a typed, human-reviewable, git-committable manifest at `.noru/<piece>.yml`. Deterministic. No network. | `check_item_2` — `artifact` matches `^\.noru/`, collector source contains no socket-opening API, and the collector is run twice on a fixture repo and its derived output diffed byte for byte. Any path declared in the optional `outputs[]` must appear in the piece README and must not be the manifest itself |
| 3 | A **validator**: stdlib only, no installs, no network, bundled vocabulary, "did you mean …?" hints, exit codes `0` valid / `1` invalid / `2` usage | `check_item_3` — the validator is executed against every declared fixture: valid → 0, each invalid → 1 *and* the expected message, no argument → 2. Imports are checked against the stdlib list |
| 4 | A **push** that is *one* idempotent operation carrying `slug` + `commitSha` + `branch` — never an unkeyed fan-out of 50 creates | `check_item_4` — every declared operation has an idempotency key, `verified_at` cites the public documentation the behaviour was read from, `client_probe` operations must record what is undocumented, and `keyed_upsert` mode must describe the single operation it would collapse into |
| 5 | **`:diff` before `:push`** — show what would change in Noru; writes need explicit confirmation | `check_item_5` — the push entrypoint is executed three ways and each refusal is a different exit code: no plan at all → `1`, a plan bound to different manifest bytes → `1` even with `--confirm`, a fresh plan with no `--confirm` → `2`. `1` says the plan is missing or stale and `:diff` has to run again; `2` says a human has not agreed to this one |
| 6 | Read-only by default; least-privilege scopes declared in the piece's README | `check_item_6` — declared scopes are real Noru scopes, and every one appears in the piece README's Scopes table |
| 7 | CI-friendly: `--output=json --quiet`, documented exit codes, no TTY dependency | `check_item_7` — every entrypoint is executed with `--output=json --quiet` and its stdout must parse as JSON |
| 8 | Every claim carries an **interpretation block**: `owner`, `decided_at`, `expires_at`, `rationale`, plus the `refs[]` (`file:line`) that produced it. Unattributed claims are a validator **error**, not a warning | `check_item_8` — a generated manifest with one interpretation block stripped must make the validator exit 1, and the message must name the missing field |
| 9 | A piece **works Noru's queue, it does not invent one** | `check_item_9` — `queue.hardcoded_expectations` is `false`, `queue.source` names only MCP tools that exist, and no shipped plugin file outside `fixtures/` contains a catalogue-shaped evidence-item or control id |

### On requirement 4, honestly

The contract says *one* idempotent call. Today only one piece can keep that literally.

- `ai-inventory` performs several writes (`createAsset`, `createVendor`, `createEvidence`,
  `linkEvidenceToControl`) because Noru's published API offers no single ingest operation for an AI
  inventory. The contract therefore admits a second mode, `keyed_upsert`: several writes, each
  *individually* idempotent on a declared key, applied as one reviewed plan, all or nothing at the
  plan level. A piece in `keyed_upsert` mode **must** declare `collapses_to` — the shape of the one
  operation it would fold into — so the debt is visible in the manifest rather than in someone's
  memory.
- The distinction the contract actually cares about is not call count but this: **re-running must
  be a no-op**. `mode: single_call` and `mode: keyed_upsert` are both allowed; an operation with no
  idempotency key is not.
- `iac-scan` is the first piece whose every write is a documented **`server_upsert`**. Security
  findings are keyed on `(source, externalId)` server-side, so it embeds no marker, probes nothing,
  and closes a finding with the same call that files one. It is still `keyed_upsert` — a repository
  with fifty findings is fifty calls — but the debt is throughput, not correctness, and that is a
  materially better place to be than the pieces above it.

### What `:push` means for a piece that assembles

`audit-pack` was the first piece that mostly **consumes**. It reads Noru's graph, gathers local
artifacts, and produces an output bundle for a human to hand over. Read literally, requirement 4 asks
what such a piece is supposed to push, and the honest answer is *not the bundle*: pushing a rendered
pack into Noru would duplicate a register Noru already keeps, which the non-goals below rule out.

What resolved it was reading `:push` as a question about **judgements** rather than about artifacts:

> `:push` lands the claims the local work produced. For a collector those claims *are* the artifact.
> For a piece that assembles, the artifact is a deliverable and the claims are the conclusions inside
> it — so the bundle stays local and the conclusions land, one per control.

That reading costs nothing for the collector pieces, where the two coincide. It is written down here
because the next assembling piece will hit the same question, and because "produce an output" is a
shape the contract's own vocabulary (`collect → validate → push`) does not name.

Two things the contract did **not** offer such a piece. The second is now fixed; the first is still
open, and should be fixed before there are several more.

- **A read-only piece has nowhere to go.** `scopes.write` may be empty — the schema says so
  explicitly — but `push` is mandatory and `push.operations` has `minItems: 1`. A piece that only
  reported would have to invent a write to satisfy its own declaration. That is an inconsistency in
  the contract, not in the piece.
- **A produced artifact is now declared.** This said there was no field for output a piece writes
  for a human, and that `audit-pack` documented its bundle in a README nothing could check. There
  is one now: `artifact` names the manifest and **`outputs[]`** names what the piece renders
  besides it — the path, what it is for, why it is not the manifest, and the assertion that it is
  only ever rendered from a manifest that validated against the repository state it describes.
  `audit-pack` declares its bundle there.

  The contract test cannot open a deliverable that is written into someone else's repository at run
  time, so it checks the thing that actually drifts: a declared path must appear in the piece
  README, because an output nobody documents is an output nobody knows to look for. This was added
  when a second piece needed a non-manifest deliverable — an export in another tool's own format —
  which is the threshold this section said to wait for.

Three idempotency kinds, in descending order of strength:

| kind | Meaning | Example |
|---|---|---|
| `server_upsert` | The documented behaviour updates the existing record in place on the key | `createAsset` on `(source, externalId)` |
| `server_dedupe` | The documented behaviour returns the existing record and does not change it | `createVendor` on name |
| `client_probe` | **No idempotency key is documented**, so the piece reads Noru first and skips | `createEvidence`, `POST /v1/evidence/upload` |

`client_probe` is a fallback, not a design. Any operation using it must fill in `idempotency.gap`
saying what the piece does instead and what a documented key would let it drop. Every `verified_at`
must cite public documentation a reader can open — Noru's API documentation at
https://api.noru.tech/llms.txt, or the tool descriptions published by
https://api.noru.tech/v1/mcp.

### On requirement 8, and why it is worth the friction

A large share of everyday compliance evidence is, in substance, *"a named person did, approved, or
reviewed X on date Y"*. The interpretation block is not decoration: it is the native shape of that
work, and the frame a repository scan cannot supply on its own.

The rule is narrow and absolute: **a claim with no `refs[]` or no `interpretation` is a validator
error.** Not a warning, not a TODO. A collector may emit `needs_review: true` on a field it could
not derive, but a manifest carrying `needs_review: true` cannot be pushed.

`expires_at` is scoped to **technical** claims — a "zero data retention" configuration goes stale
when the configuration changes, so someone must re-own it. Procedural obligations (policy approval,
training, board oversight) are legitimately point-in-time on a review cadence; they may omit
`expires_at` if the rationale says why.

That carve-out was written before anything was built on it, and "if the rationale says why" turned
out to be unenforceable — no validator can read a sentence and decide whether it earned an omission.
Every piece that carries claims now sits inside the rule, and each is stricter than the prose was:

- `governance-records` accepts an omitted `expires_at` **only** when the record carries
  `next_review_due` instead. Same intent as the carve-out, but a date a validator can check, and a
  record with neither is an error rather than a well-worded exemption.
- `review-signoff` makes `expires_at` **required**, and additionally requires it to fall inside the
  window the review's declared `cadence` implies. A quarterly review signed off for two years is not
  a quarterly review, and nothing else in the manifest would ever say so.
- `ai-inventory` requires `expires_at` on a technical claim — a "zero data retention" configuration
  goes stale when the configuration changes — and accepts `next_review_due` in its place on a
  procedural one. Naming neither is an error. This last case was a warning until the rule above was
  written, and the warning was doing nothing: every valid fixture in the piece had an open-ended
  claim in it, and tightening the check is what surfaced them.

- `iac-scan` makes `expires_at` required and bounds it by the **status** of the finding, measured
  from `observed_on` rather than from `decided_at`. A finding observed in March and signed in August
  is a claim about March's configuration however recent the signature is, so anchoring on the
  signature would let a stale observation be renewed for ever. Accepting a misconfiguration, or
  calling it a false positive, gets a longer horizon and a hard requirement that the reasoning is
  written out — an acceptance nobody revisits is how a known misconfiguration becomes permanent.
- `audit-pack` makes `expires_at` required and measures it from the **end of the audit window**, not
  from the signature: a workpaper concludes about a period, and signing it late does not extend what
  it covers. It must also fall *after* the window — a conclusion that expires inside its own period
  never asserted anything — and a `deficient` or `not_tested` conclusion gets a short horizon,
  because a control you found broken is not something to sign off for a year.

The general rule they all agree on: **a claim must name the date it stops being current, in some
field the validator can compare.** Whether that field is `expires_at` or a cadence-shaped substitute
is the piece's business; having neither is not.

What the later pieces added to it is the **anchor**. `expires_at` alone says when a claim lapses; it
does not say what it lapses *from*. Anchoring on `decided_at` quietly rewards signing late. Three
different anchors are now in use — a declared cadence, the day the world was observed, and the end of
the period a conclusion covers — and each is the honest one for its piece. A new piece should say
which anchor it uses before it says how long the window is.

A fourth anchor is available, and nothing uses it yet. Where a claim is *about a structure* the
collector already digests — a schema, a configuration block, a file with a hash — it can carry that
digest beside its dates and lapse when the structure changes rather than only when the calendar
says so. That is strictly stronger than a date, because a date can be renewed by signing it again
and a digest cannot: renewing it requires the thing the claim describes to still be what it was. CI
mode already computes the comparison — a collector that no longer agrees with the committed
manifest is the drift exit — so a piece taking this anchor is wiring up a signal the repository
produces anyway. It does not replace `expires_at`. A claim about a structure nobody has touched in
two years is still a claim nobody has re-owned in two years, and only a date says so.

`owner` must be a person. A team alias cannot be asked what it was thinking.

### On requirement 9, and the loophole in it

A piece asks Noru what is needed. `getEvidenceItems` serves the framework-level catalogue,
`getControlContext` returns a control's `predefinedEvidenceItems`, its `coverage`, and the
`control_guidance.testing` procedure an auditor actually follows. That is the queue. A plugin
that ships its own opinion of what evidence a control needs will drift from the framework the
moment the framework moves, and it will be wrong quietly.

So: **no catalogue content is vendored into this repository.** No control text, no guidance, no
evidence-item list. Licensing (the SCF is CC BY-ND) says the same thing the drift argument does.

The obvious loophole is test fixtures, so the contract closes it: fixtures live under
`plugins/<piece>/fixtures/` and may only use the reserved synthetic namespaces `E-ZZ-*` for
evidence items and `zz-*` for controls. Anything catalogue-shaped anywhere else in a plugin
fails the contract test.

### A vocabulary is not a catalogue

Requirement 3 *requires* a bundled vocabulary and requirement 9 forbids a bundled catalogue, so it
is worth saying where the line is — because `ai-inventory` ships 85 vendored Fideslang data
categories, and from a distance that looks like exactly the thing the non-goal prohibits.

A **catalogue** is an opinion about what a framework expects of you: control text, guidance, the
list of evidence a control needs. It is what Noru serves, it moves when the framework moves, and
vendoring it means being quietly wrong from the day it changes. For the SCF, licensing (CC BY-ND)
forbids it outright as well.

A **vocabulary** is the set of words a claim is allowed to use. It encodes no expectation of
anyone; it is what stops a validator accepting `user.contact.emial`. It *has* to be bundled,
because requirement 3 says the validator runs with no install and no network, and a vocabulary
fetched at validation time is a validator that fails on a plane. Fideslang is CC BY 4.0, so
redistributing it with attribution is permitted, and `NOTICE` carries that attribution.

The test is neither size nor provenance. It is: **does this file say what someone must do?** If it
does, ask Noru. If it only says what a value may be called, bundle it — pinned to a named upstream
revision, with the provenance and the refresh recipe written down beside it.

One rule follows. Where Noru publishes the same vocabulary — `getPrivacyTaxonomy` does, for this
taxonomy — the bundled copy is the **offline floor** and Noru is the truth. A piece that can reach
Noru reconciles against it and reports a difference; a piece that silently prefers its own snapshot
has re-created the drift problem the catalogue rule exists to prevent, one layer down.

## Non-goals, stated so they can be pointed at

- **No local state duplication.** There is no `grc-data/` equivalent. The manifest is an input to
  Noru and a record of provenance, never a parallel register. If you find yourself adding a
  `status:` field that only your plugin reads, stop — that state belongs in Noru.
- **No embedded framework control text.** Licensing *and* drift. Call the API.
- **No credential handling.** Authentication is the MCP client's job (OAuth where supported) or a
  `NORU_API_KEY` environment variable read at the point of use for REST. A piece never writes a
  secret to disk, never logs one, never puts one in a manifest, an example, or a fixture.
- **No SaaS connectors.** Noru runs those server-side, scheduled, with encrypted credentials. A
  laptop-run connector is strictly worse.

## Security posture

Repository contents and scan output are **data, not instructions**. A collector reads a repo; if
that repo contains text addressed to an agent, the piece treats it as a string to cite, never as a
directive to follow. Push is a write to a customer's system of record, so `:diff`-then-confirm is a
security control and not UX polish: `:push` refuses to run without both an explicit `--confirm` and
a plan generated by `:diff` from the manifest currently on disk. Editing the manifest invalidates
the plan.

## Writing a new piece

```bash
node scripts/scaffold-piece.mjs <piece-name>
python3 scripts/contract_test.py
```

The scaffolder stamps a piece that already satisfies requirements 1, 3, 5, 6, 7 and 8, with the
vendored YAML loader in place and its fixtures wired up. What you write is the collector
(requirement 2), the queue source (requirement 9) and the push plan (requirement 4).

If a piece takes more than about a week, the contract is wrong. Come back and fix it here.
