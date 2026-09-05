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
python3 scripts/publish_actions.py --check # each action runs from its Marketplace mirror layout
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

## Releasing

Every plugin and every action shares one version number, and `scripts/check_repo.py` fails while
any copy of it disagrees. A release is three steps, and the third one is automatic:

1. **Bump.** Open a `chore: release X.Y.Z` pull request that moves the version everywhere at once:
   both marketplace manifests, the two `plugin.json` files of every piece, every copyable
   `@vX.Y.Z` action pin in the docs, and a `## X.Y.Z — YYYY-MM-DD` section in `CHANGELOG.md`.
2. **Tag.** After the merge, tag that commit and create the GitHub release for this repository:

   ```bash
   git tag vX.Y.Z <merge-commit> && git push origin vX.Y.Z
   gh release create vX.Y.Z --title vX.Y.Z --notes "See CHANGELOG.md"
   ```

3. **Publish.** The tag push runs the `release` workflow. It re-runs the full gate, asserts that the
   tag, the marketplace entries and every manifest agree, and then mirrors the three GitHub Actions
   to their Marketplace repositories (below). Nothing else needs a human — except the first time.

### GitHub Marketplace mirrors

The Marketplace lists one action per public repository, and only when `action.yml` is at that
repository's root. This repository cannot satisfy that, so `scripts/publish_actions.py` mirrors each
action, together with the toolkit it runs (`scripts/`, `plugins/`, `contract/`), into a
distribution repository of its own:

| In this repository | Marketplace repository | `uses:` |
|---|---|---|
| `.github/actions/noru-ci` | `noru-tech/noru-ci-action` | `noru-tech/noru-ci-action@vX.Y.Z` |
| `.github/actions/noru-review` | `noru-tech/noru-review-action` | `noru-tech/noru-review-action@vX.Y.Z` |
| `actions/enforce` | `noru-tech/noru-enforce-action` | `noru-tech/noru-enforce-action@vX.Y.Z` |

Both `uses:` forms — the in-tree path and the mirror — are the same code at the same tag. The
mirrors are generated: never edit them by hand, the next release overwrites the tree. Each carries a
`DISTRIBUTION.json` naming the source commit it was built from.

**Republishing** is safe and idempotent. Re-run the `release` workflow with `workflow_dispatch` and
the version, or from a checkout of the tag:

```bash
python3 scripts/publish_actions.py publish --version=X.Y.Z            # or --dry-run first
```

A mirror that already matches gets no commit; an existing `vX.Y.Z` tag is left alone when it points
at the same tree and is a hard failure when it does not. A released tag is immutable — ship a new
patch version instead. The floating `vX` tag moves only when the release is the newest of its major
line. Run `python3 scripts/publish_actions.py build --out=/tmp/mirrors` to inspect what would be
published without touching anything.

**One-time bootstrap**, per mirror, for whoever administers the `noru-tech` organization:

1. Create the public repository, empty — no README, no license, the first publish supplies both:

   ```bash
   gh repo create noru-tech/noru-ci-action --public --description "GitHub Marketplace distribution of the noru-ci action from noru-tech/noru-grc-engineering"
   ```

2. In this repository, create the `marketplace` environment and give it the secret
   `ACTIONS_PUBLISH_TOKEN`: a fine-grained personal access token of a user with two-factor
   authentication, scoped to the three mirror repositories with *Contents: read and write* and
   nothing else. It must be a user token, not a GitHub App token — the Marketplace lists a
   release automatically only when the account that created it can satisfy the two-factor
   requirement. Add the release maintainers as required reviewers on that environment if you want
   a human gate in front of the publish.
3. Trigger a publish (push a tag, or `workflow_dispatch` the `release` workflow with the current
   version). The mirror now has the tree, the tag and a GitHub release.
4. Open that release in the mirror repository, choose *Edit*, tick **Publish this Action to the
   GitHub Marketplace**, accept the Marketplace Developer Agreement when prompted, pick a primary
   category (*Continuous integration*; *Security* as the secondary) and *Update release*. GitHub
   validates the metadata at this point: `name`, `description` and `branding` in `action.yml`, and
   a `name` no other Marketplace action uses.

After step 4, every release the workflow creates in that mirror is listed automatically. Confirm at
<https://github.com/marketplace?type=actions&query=noru>.

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
