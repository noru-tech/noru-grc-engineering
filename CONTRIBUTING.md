# Contributing to noru-grc-engineering

Thanks for improving this toolkit. It is intentionally small and public: a contract, a hub, two
pieces, and the tests that keep them honest.

## Everything here is public

This repository is world-readable, and so is every pull request against it. Treat anything you write
here as published the moment you type it.

**The only Noru facts that belong in this repository are ones a customer can already see for
themselves:**

- the public REST API at `api.noru.tech` — its published documentation and
  <https://api.noru.tech/llms.txt>
- the public MCP endpoint `https://api.noru.tech/v1/mcp` and the tool descriptions it publishes
- documented scopes, documented tool names, documented endpoint paths, documented limits

**Never commit any of the following, in code, comments, docs, commit messages, fixtures or PR
text:**

1. **Internal source layout.** No file path, module, directory, class, service, method, table or
   migration name from any private Noru repository. `verified_at`, `Source of truth:` and "see
   `<path>`" citations must point at public documentation, not at a file only Noru can open.
2. **Internal implementation behaviour.** How Noru works underneath its published API — including
   how it deduplicates, upserts, retries or stores anything — unless the published documentation
   says so. There is a real difference between the two sentences below, and only the second one
   belongs here:
   - *"Noru has no dedupe key on evidence, so a re-push duplicates."* — a weakness inferred from
     private code, written up as a finding. That is a vulnerability disclosure wearing a design
     note. Never publish it.
   - *"No idempotency key is documented for evidence, so this piece does not assume one."* — a
     statement about what the public documentation does and does not cover, and the defensive
     choice that follows from it. Publish that.

   If the docs are silent, say they are silent and design defensively. Do not fill the silence with
   what you happen to know.
3. **Internal analysis.** No coverage percentages, catalogue counts, bucketing methodology, or
   rankings derived from Noru's own control or evidence seed data. Say what a piece targets; do not
   publish a measurement of how much of a framework it moves.
4. **Internal prioritisation and positioning.** No "wedge", no comparison of one piece's worth
   against another's, no competitor framing, no kill conditions, no effort estimates. State what a
   piece does.
5. **Unreleased plans.** No unshipped tool names, unshipped scopes, backend backlog, or dates. If a
   piece would collapse into an operation that does not exist yet, describe the *shape* of that
   operation without naming it as though it were coming.
6. **Anything identifying.** No customer or employee names, no internal project, infrastructure or
   cloud-project identifiers, no internal tool names, no internal URLs, no output captured from a
   real organization.

**Before you open a pull request**, read your own diff as a stranger would:

```bash
git diff --stat
git diff -U0 | grep -nEi "packages/|apps/src|monorepo|internal|source of truth|% of the"
```

Anything that only makes sense to someone with access to Noru's private code does not belong here.
Ask instead of guessing — an unanswered question is cheaper than a public leak.

## Ground rules

- **Atomic.** Runtime code uses Node built-ins for `.mjs` and the Python 3 standard library for
  `.py`. No `npm install`, no `pip install`, no vendored third-party package, no network during
  `:scan` or validation. PyYAML may be used opportunistically, but the bundled fallback loader must
  keep working without it. If CI ever needs an install step, something has broken this promise.
- **No framework catalogue.** No control text, no guidance, no evidence-item list, ever. Licensing
  and drift, and the contract test enforces it.
- **No credentials.** Never commit an API key, a bearer token, a customer identifier, a generated
  local config, or output captured from a real organization. Use `<NORU_API_KEY>` in examples.
- **No invented API surface.** Never write down an MCP tool name, a scope, a field or an endpoint
  you have not checked against Noru's published documentation or the MCP server's own `tools/list`.
  `piece.json` requires a `verified_at` citing public documentation for every idempotency claim, for
  exactly this reason. If the behaviour is not documented, record that instead of citing something
  the reader cannot open.
- **Every claim gets a test.** If you write an invariant in a comment, a README or a PR description,
  add the check that asserts it. A guarantee nothing tests is a guess with better formatting.
- Update `CHANGELOG.md` for user-visible changes.

## Layout

```text
contract/                    the piece contract, manifest schemas, the vendored yaml_mini source
plugins/noru/                hub plugin (and the canonical plan.mjs every piece vendors)
plugins/<piece>/             an installable piece
scripts/                     scaffolder, contract test, checks — stdlib and built-ins only
scripts/templates/           the files scaffold-piece.mjs stamps out
tests/fixture-repo/          the repository collectors are exercised against
docs/                        client setup and authoring guides
```

## Adding a piece

```bash
node scripts/scaffold-piece.mjs <piece-name>
```

You get a piece that already satisfies most of the contract. What is yours to write: the collector
(requirement 2), the queue source (requirement 9), and the push plan (requirement 4). Then add it to
both marketplace manifests — `scripts/check_repo.py` fails if you forget.

Read [`contract/README.md`](./contract/README.md) first and
[`docs/authoring-a-piece.md`](./docs/authoring-a-piece.md) second.

## Verification

Before opening a pull request:

```bash
python3 scripts/check_repo.py          # marketplaces, schema/vocabulary sync, secret hygiene
python3 scripts/check_vendored_lib.py  # vendored blocks are byte-identical
python3 scripts/test_validators.py     # schema fixtures + validator unit tests
python3 scripts/test_collectors.py     # collectors detect what the pieces claim they detect
python3 scripts/test_idempotency.py    # a second push is a no-op
python3 scripts/contract_test.py       # every plugin satisfies requirements 1-9
python3 scripts/test_ci_mode.py        # CI mode fails on drift and on an expired interpretation
python3 scripts/test_repo_enforcement.py # enforcement ratchet, action and GitHub adapter safety
git diff --check
```

And inspect for secrets:

```bash
git grep -naE "noru_[A-Za-z0-9]{12,}" -- .
git grep -naE "Bearer [A-Za-z0-9._~+/=-]{20,}" -- .
```

These should find nothing. Placeholders like `Bearer <NORU_API_KEY>` are fine in documentation.

Use `-a`. A single control character anywhere in a file makes `grep` and `git grep` treat the whole
file as binary and skip it silently — which is exactly the file a scan most needs to read. Do not
put a raw control character in a source file; write the escape (`\u0000`) instead.

### Changing a vendored library

`contract/lib/yaml_mini.py` and `plugins/noru/scripts/lib/plan.mjs` are copied verbatim into every
piece, because an installed plugin cannot import across plugin boundaries. Edit the canonical copy,
then:

```bash
python3 scripts/check_vendored_lib.py --fix
```

Never edit a vendored copy directly — the next `--fix` will overwrite it, and CI will catch the
drift in the meantime.

### Local plugin install check

```bash
tmpdir="$(mktemp -d)"
CODEX_HOME="$tmpdir" codex plugin marketplace add <path-to-this-repo>
CODEX_HOME="$tmpdir" codex plugin list --marketplace noru-grc-engineering
CODEX_HOME="$tmpdir" codex plugin add ai-inventory@noru-grc-engineering
```

The temporary `CODEX_HOME` keeps this out of your real configuration.

## Style

- Comments explain **why**, not what. The reader can see what the code does.
- Error messages should tell someone what to do next. `"unknown data category 'user.contact.emai'
  (did you mean 'user.contact.email'?)"` is worth ten lines of documentation.
- Exit codes are part of the interface: `0` success, `1` the thing you checked is wrong, `2` you
  called it wrong. Do not add a third meaning to any of them.
- Prefer early returns and guard clauses over nesting.

## Pull requests

1. Branch from `main`.
2. Keep the change scoped.
3. Run the verification block above.
4. Describe the behaviour change, how you tested it, and any security implication.

By contributing you agree that your code and documentation are licensed under this project's MIT
license. The vendored Fideslang taxonomy under `contract/lib/taxonomy/`, and every verbatim copy of
it under `plugins/<piece>/references/taxonomy/`, remains CC BY 4.0 — see [`NOTICE`](./NOTICE).
Refresh it in the canonical directory only, then run `python3 scripts/check_vendored_lib.py --fix`;
never edit a vendored copy.
