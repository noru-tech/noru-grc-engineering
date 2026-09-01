# Authoring a piece

A piece should take about a day once the shape is clear. If it takes two weeks, the contract is
wrong — stop and fix [`contract/README.md`](../contract/README.md) rather than working around it.

## Start

```bash
node scripts/scaffold-piece.mjs my-piece
python3 scripts/contract_test.py --piece=my-piece   # passes immediately
```

You get a piece that already satisfies requirements 1, 3, 5, 6, 7 and 8: manifests for both clients,
three commands, a skill, a stdlib validator with the vendored YAML loader spliced in, a diff that
writes a plan, a push that refuses without `--confirm` and a fresh plan, fixtures the contract test
executes, and a README with the scopes table.

Three things are yours.

## 1. The collector (requirement 2)

`scripts/collect.mjs`. Node built-ins only, no network, and **deterministic**: the contract test
runs it twice over the same fixture repository and diffs the derived output byte for byte.

The three ways people break determinism, in order of how often:

- a timestamp in the derived output (put it in the *state snapshot* instead — that records when you
  called Noru, which is a fact about the call, not about the repository)
- an unsorted directory listing (`readdirSync` order is not stable across machines)
- iterating a `Set` or `Map` built in encounter order

**Do not write your own file enumeration.** The template gives you `listFiles(repo)`; use it, and
filter what it returns. It asks `git ls-files` — the set `actions/checkout` gives CI, honouring
`.gitignore` and friends — and falls back to a directory walk only where there is no git to ask,
which is a legitimate case (an exported tarball) rather than a failure. Walking the working tree
instead is a defect that looks like it works: a developer's tree holds worktrees, scratch checkouts
and unpacked archives that CI never sees, so the scans disagree permanently and the committed
manifest can match one environment or the other and never both — while every extra fact it records
is cited to a path that is not in the repository. Three pieces shipped that bug because each wrote
its own walk.

Which of the two happened is a fact about the scan, so report it: the template puts it in
`coverage.enumerated_by` and excludes `coverage` from `digestOf`. Keep it there. The same files
enumerated two ways are the same repository, and must not read as drift.

Split what you write into two halves and keep them apart:

- **derived facts** — what the collector can stand behind, each with `file:line`. These go to
  `.noru/.cache/<piece>.derived.json`, which is machine-owned and gitignored.
- **judgement** — purpose, autonomy, whether an artifact really satisfies an expectation. The
  collector marks these `needs_review: true` and a human resolves them. `needs_review: true` blocks
  the push; that is the whole mechanism.

**If the manifest already exists, do not overwrite it.** Report the drift and let `:diff` and a
human resolve it. Regenerating over someone's attributed claim is the worst thing a collector can
do, because it looks like it worked.

## 2. The queue (requirement 9)

Where does this piece's idea of "what is needed" come from?

- If it is repo-resident truth (the `ai-inventory` case), the repository is the queue — say so in
  `piece.json` with `kind: "repository"` and a note explaining why.
- If it is a compliance expectation (the `evidence-push` case, and most cases), **ask Noru**:
  `getControlContext` returns `predefinedEvidenceItems`, `linkedEvidenceItems` with
  `qualifiesRequirement`, `coverage` and `guidance.testing`. The unmet set is the difference between
  the first two.

Never ship a list of what a control needs. The contract test scans every plugin file for
catalogue-shaped identifiers and fails the build, including in fixtures — fixtures may only use the
reserved `E-ZZ-*` and `zz-*` namespaces.

Read `guidance.testing` to the user when they ask what a control wants; it is literally the
auditor's procedure. Do not copy it into the manifest or into this repository.

## 3. The push (requirement 4)

Every write needs an idempotency key, and you must say which kind it is and **where you checked**
— `piece.json` requires a `verified_at` citing public documentation:

| kind | Meaning |
|---|---|
| `server_upsert` | documented to update in place on the key (`createAsset` on `(source, externalId)`) |
| `server_dedupe` | documented to return the existing record unchanged (`createVendor` on name) |
| `client_probe` | **no idempotency key is documented** — read Noru first and skip. Must also fill in `gap` |

Check it against something a reader of this repository can open: Noru's API documentation at
<https://api.noru.tech/llms.txt>, or the tool description the MCP server at
<https://api.noru.tech/v1/mcp> publishes for the tool you are calling. Never invent a tool name, a
scope, a field or an endpoint, and never cite a source your reader cannot reach. If the behaviour
you need is not documented, say exactly that and record it as a `gap` — that is more useful than a
plausible-looking call that fails at runtime, and far more useful than a confident claim nobody can
check.

For an MCP piece, `push.mjs` does **not** make the calls: it emits the confirmed, ordered call list
and the client executes it. A script that made the calls would have to handle a credential, which
the contract forbids. A REST piece may perform the write itself, reading `NORU_API_KEY` at the point
of use, never storing or logging it.

## Then

```bash
python3 scripts/contract_test.py
python3 scripts/test_validators.py
python3 scripts/test_collectors.py      # add your piece to it — assert what your collector finds
python3 scripts/test_idempotency.py     # add your piece to it — a second push must be a no-op
python3 scripts/check_repo.py           # will tell you to register the piece in both marketplaces
```

Add the piece to `.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json`, add its
vocabulary/schema pair to `VOCAB_SYNC` in `scripts/check_repo.py`, and write the `CHANGELOG.md`
entry.

## Writing the commands and the skill

These are prose an agent follows, so write them as instructions to a careful colleague, not as
documentation:

- Say what is untrusted. Repository contents and tool output are data. If they address the agent,
  it quotes them as a finding and does not act.
- Say who must confirm. "Run the scan" is not consent to write. Ask in the conversation, showing
  the create/update counts.
- Say what a second run should do: nothing. An agent that sees "0 changes" and starts looking for
  something to change is the failure mode.
- Make the skill `description` read the way a user would describe their problem. That text is what
  makes the skill trigger; a description written for the author triggers for nobody.

## Things that will bite you

- **Ids must be stable.** If your key becomes half an upsert key, renaming it creates a second
  record rather than updating the first. Say so in the piece README.
- **Metadata round-trips.** Nothing guarantees a JSON object comes back with its keys in the order
  you sent them, and in practice they do not match. Hash with sorted keys, or every second run
  looks like a change and idempotency quietly dies.
- **The lowercase control `id` is canonical.** The uppercase `controlId` is for display. Storing
  the display form in a manifest is how ids drift.
- **`expires_at` is required for technical claims.** A configuration-dependent claim that never goes
  stale is a claim nobody re-checks.
- **The digest answers one question: has the repository changed?** Anything in the derived facts
  that is not about the repository has to be kept out of it — `generated_by` (the version of the
  tool that read it) and `coverage` (how the file list was arrived at). Hash either and every
  committed manifest reports drift that re-running `:scan` can never clear.
