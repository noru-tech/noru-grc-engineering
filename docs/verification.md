# Verification

What has actually been verified, and what has not. The second list is the important one.

## Verified, and re-verified on every build

Run all of it with no dependencies and no network:

```bash
python3 scripts/check_repo.py          # marketplaces, manifests, schema/vocabulary sync, secrets
python3 scripts/check_vendored_lib.py  # vendored blocks are byte-identical across pieces
python3 scripts/test_validators.py     # schema fixtures + validator unit tests
python3 scripts/test_collectors.py     # collectors detect what the pieces claim they detect
python3 scripts/test_idempotency.py    # a second push is a no-op, end to end
python3 scripts/contract_test.py       # every plugin satisfies requirements 1-9
python3 scripts/test_ci_mode.py        # CI mode fails on drift, on an expired claim, and on
                                       # personal data the baseline does not permit
```

| Property | How it is proven |
|---|---|
| Collectors are deterministic | `contract_test.py` runs each collector twice over two copies of `tests/fixture-repo/` and diffs the derived output byte for byte |
| Collectors are offline | the collector source is scanned for every socket-opening API; a match fails the build |
| Validators are stdlib-only | every `import` in a validator is checked against an allowed standard-library set |
| Exit codes are `0`/`1`/`2` | each validator is executed with no argument, a missing file, an unknown option, a valid fixture and each invalid fixture |
| Invalid manifests produce a *useful* message | each invalid fixture declares the substring its output must contain, not just a non-zero exit |
| Unattributed claims are an error | a fixture with the interpretation block stripped must exit `1`, and must not produce only warnings |
| An Article 50 trigger cannot be recorded without a disclosure check | a fixture that records the trigger and stops there must exit `1` |
| The Article 50 disclosure check reports the right state | `test_collectors.py` runs the real collector over purpose-built repositories: a notice in the same file as the model call is `present`, a notice one directory away is `unclear`, no notice anywhere is `absent`, and a visible caption does not satisfy the machine-readable marking duty |
| A disclosure cannot be called absent without saying where the check looked | a fixture omitting `searched` must exit `1` |
| Text sentiment analysis is not reported as emotion recognition | `test_collectors.py` scans a sentiment scorer and asserts no Article 50(3) trigger; a face-based one asserts the trigger does fire |
| The Article 5 screen is visibly running rather than silent | `test_collectors.py` asserts a clean repository still reports which practices were screened |
| The collector proposes and never asserts | `test_collectors.py` asserts the skeleton never writes `determination: indicated` and flags every finding it proposes `needs_review: true` |
| Findings are written enforceable-first | `check_repo.py` compares the vocabulary's category order against the schema's declaration order; a manifest written tier-first must exit `1` |
| `:push` refuses without `--confirm` | executed: exit `2` |
| `:push` refuses a stale plan | executed: a plan bound to different manifest bytes exits `1`, even with `--confirm` |
| **A second push is a no-op** | `test_idempotency.py` drives scan → validate → diff → push, builds the org snapshot that would exist if every planned write had landed, and asserts the next diff is all `skip` and the next push makes no calls |
| Asset metadata key order does not break idempotency | the snapshot deliberately reverses the key order, because nothing guarantees a JSON object comes back in the order it was sent |
| **A plan does not depend on which YAML loader parsed the manifest** | `test_idempotency.py` parses the same manifest bytes with PyYAML and with the bundled fallback — both in one interpreter, the fallback forced by hiding PyYAML behind a stub that raises on import — and asserts for all six pieces that the plan (markers, arguments, effects and reasons) is identical, both writing into an empty organization and writing into one the same plan has already been pushed into. Needs PyYAML to have two loaders to compare, so it reports itself skipped where there is none and the CI matrix runs a leg with it |
| No catalogue is vendored | every plugin file is scanned for catalogue-shaped evidence-item and control ids; fixtures may only use the reserved `E-ZZ-*` / `zz-*` namespaces |
| No credential leaks into the repository | the tree is scanned for credential-shaped strings |
| **The segregation rules mean the same thing in both languages** | the rules are implemented twice — `collect.mjs` to propose them, `validate_manifest.py` to refuse an unowned one — and `test_collectors.py` runs both over seven cases chosen where they could plausibly diverge: a name differing only in case or whitespace, a comment that is not an approval, an agent whose only reviewer is its operator. Checked in the other direction by removing the lowercasing from one side |
| A clean change raises no violation at all | asserted in both implementations: a rule set that fires on everything is as useless as one that fires on nothing, and easier to ship by accident |
| **A remediated exception closes its finding** | `test_idempotency.py` asserts a `remediated` or `false_positive` disposition is pushed with status `resolved`, so a re-run after the fix closes the record instead of leaving a stale open finding beside a fixed problem |
| One evidence record per window, not one per change | asserted: a blob per change is how a register becomes unreadable |
| A schema describing a *format* is not a schema describing a *record* | found by running the coverage check against this repository, where a `$schema` marker matched all ten files in `contract/`. The JSON Schema and Zod markers were removed and the case is now a regression test: a repository whose only candidates are a contract schema and a Zod request validator raises no coverage finding |
| **A 403 on an optional read does not kill an export** | found on the first live API call: GitHub answers `403 Resource not accessible by integration` — not 404 — when a token may not read branch protection, and a probe tolerating only 404 killed the whole export. `test_collectors.py` now drives the exporter against a stdlib HTTP server returning that exact response, asserts the export completes with the settings omitted, and asserts the other direction: a 403 on the pull requests themselves still fails, because an export missing those is not an export. Checked in the other direction by removing the tolerance |
| A token is not echoed into an error | the same test asserts the token it passed does not appear in stderr on the failing path |
| An unreadable forge setting is not a false setting | `normalizeProtection` omits the protection fields when the probe 404s, because GitHub and GitLab both answer 404 for "not protected" and for "you may not ask". Reporting `protected: false` there would state something untrue |
| **No manifest uses a YAML 1.1 boolean word** | `check_repo.py` scans every `.yml`/`.yaml` under `plugins/`, `contract/` and `tests/` for `yes`/`no`/`on`/`off`/`y`/`n` as a key or an unquoted value, so a file cannot mean two different things on two machines. Checked in the other direction with a probe fixture; GitHub Actions workflows are exempt because `on:` is required there |
| A scaffolded piece satisfies the contract | CI scaffolds one and runs the contract test against it |
| **CI mode fails on drift** | `test_ci_mode.py` adds a model provider to a copy of the fixture repo and asserts exit `3`, that the message names the provider and the `file:line` it arrived at, and that the gate clears again when the file is removed |
| **CI mode fails on personal data the baseline does not permit** | `test_ci_mode.py` builds a repository whose data map matches it, commits a baseline narrower than the map, and asserts exit `7` with an `unpermitted_category` finding. Both routes to that finding are exercised separately, because the fix differs: an explicit `deny` entry, and a value absent from a closed `allow` list |
| A permissive baseline clears the same gate | the same repository with a baseline written in prefixes (`user.contact` for `user.contact.email`) exits `0`, so the gate is not merely always-red |
| **No baseline is reported as skipped, never as passed** | the same repository with no `.noru/privacy-baseline.yml` exits `0` with the policy step's status asserted to be `skipped` — this tool ships no default policy, and the absence of one must not read as a clean result |
| A `--baseline` that does not exist is a broken gate | exits `6`, and `--mode=warn` does not suppress it |
| The baseline is itself aged | `check_expiry.py` run against a baseline whose `expires_at` has passed exits `1`: the policy is a claim like any other |
| The special-category list cannot drift | `check_repo.py` asserts every key `privacy-datamap` treats as Article 9/10 is covered by a prefix root in `contract/lib/taxonomy/special_categories.json`, and that every root is a real Fideslang category. Checked in the other direction by removing a root and adding a fake one |
| **An unreadable schema is a broken gate, not a pass** | `test_ci_mode.py` builds a repository whose only schema is Mongoose, TypeORM, ActiveRecord, GORM and OpenAPI, and asserts exit `6` with a `coverage` finding naming all five formats and citing each one. Five formats, so one stale marker cannot silently turn the case green |
| A repository with genuinely no schema is not dragged down by it | a repository with one ordinary TypeScript file raises no coverage finding — otherwise every repository without a database fails forever |
| A partial map is reported and not gated | a repository with SQL *and* the five unreadable formats does not exit `6`, still raises the finding, and does exit `6` under `--fail-on=coverage` |
| Coverage cannot change an existing manifest's digest | `coverage` is excluded from `digestOf` alongside `generated_by`; the fixture repo's derived digest is byte-identical before and after the field was added |
| **A policy finding says whether this branch introduced it** | `test_ci_mode.py` commits a data map, marks the whole backlog `pre_existing`, adds one unpermitted category, and asserts only that one is `this_pr` — against real `git`, not a stub |
| `--gate-on-new` lets a backlog through and still blocks a new violation | the same repository exits `0` on the backlog alone and `7` once the branch adds one |
| A delta that cannot be computed gates everything, not nothing | an unresolvable `--base-ref` exits `7` under `--gate-on-new` and the step says why — a shallow clone must not quietly disable the gate |
| **CI mode fails on an expired interpretation** | the same manifest with every `expires_at` moved into the past must exit `4`, name the owner, and *not* report drift — the code did not change |
| Both at once is distinguishable from either | exit `1`, with both kinds in the report |
| An invalid manifest is its own exit code | a fixture violating a piece's own cadence rule exits `5`, carries the validator's message through, and blocks the expiry step rather than passing it |
| Warn-only mode reports and does not fail | the identical findings, `"status": "warn"`, exit `0` |
| A check that could not run is not a pass | a piece whose queue is missing reports `skipped`, and `--on-missing-prerequisite=fail` exits `6` |
| CI mode is piece-agnostic | every piece in the marketplace is driven through it green, the orchestrator's source is checked for hardcoded piece names, and CI runs it against a freshly scaffolded piece |
| No credential reaches a step that does not push | asserted on the helper every step goes through: `NORU_API_KEY` is absent from the child environment except for `:push` |
| Missing credential degrades, not errors | with no `NORU_API_KEY` the push step reports `skipped` and the build stays green |
| The bundled loader produces what PyYAML produces | `test_validators.py` compares the fallback loader against pinned PyYAML output on every machine, and re-checks the pin against PyYAML itself wherever it is importable, so a pinned value cannot go stale unnoticed |
| Both YAML loaders agree in CI mode | the whole CI-mode suite runs under an interpreter with PyYAML and one without, and the orchestrator invokes each validator with its own interpreter so the loader cannot change mid-run |

The CI-mode gates have also been checked in the other direction, which is the only way a gate is
worth anything: the drift condition and the expiry condition were each constructed in a scratch
repository and observed failing with their own exit code and message before being asserted.

The contract test has also been checked in the other direction — it was confirmed to **fail** when a
collector is made non-deterministic, when a hardcoded evidence list is added to a plugin, and when
the `--confirm` gate is removed. A test that has never failed is not yet a test.

## Maturity

These pieces are **reviewed and internally consistent, not field-tested.** Everything above is
proven against fixtures on every build; none of it has been exercised against a live organization
at production scale. Treat a first run as something to check, not something to trust:

- A collector's recall against a large polyglot codebase is unproven. It will miss a provider
  reached through a hand-rolled HTTP client.
- The Article 50 disclosure states are a *repository* fact. Whether that fact matches the running
  product — a notice injected by a design system, a disclosure that lives in another repo — is
  exactly what a scan cannot see. Check both directions by hand the first time.
- `governance-records`' extractor has only met documents written to its own conventions. A real
  minute book will not be.
- The filename-to-expectation matcher in `evidence-push` has only met fixture catalogues.
- **The upload digest checks are not covered by an executed test.** `push.mjs` now recomputes the
  SHA-256 of the bytes on disk before sending (refusing a file that changed after `:scan`), sends it
  as `expectedDigest`, and compares the digest Noru returns for what it stored. Every one of those
  paths needs a live API to reach, and no script in this repository stands one up — so by this
  repository's own standard the gates have never been observed failing, and are not yet tests. The
  pre-send guard is reachable offline and is the one worth wiring up first.
- The artifact digest proves the bytes Noru stored are the bytes this repository uploaded. It says
  nothing about whether those bytes describe the running system — the same limit already stated
  above for Article 50 disclosure states being a *repository* fact. A signed screenshot of the wrong
  dashboard is still the wrong dashboard, verifiably.
- `iac-scan`'s rules are text-and-block matchers, not a parser. They will miss a misconfiguration
  expressed through a module input, a variable default or a generated template, and they will fire
  on a resource that a later override makes safe. Read the citation before you accept the finding —
  which is the workflow the piece is built around, but it is a real recall and precision ceiling.
- **A detector-shaped repository is a pathological input for `ai-inventory`.** Running it against
  this repository reports nine providers, six models and thirty-one Article 50 triggers, in a
  toolkit that calls no model at all: 220 of the 289 citations point into `plugins/`, where the
  collector is matching its own pattern tables. Anything that *lists* provider names — a scanner, a
  policy engine, an allowlist, another compliance tool — will be reported as containing them. The
  collector cannot tell a detector from the thing it detects, and nothing here fixes that; it is
  written down so a first run against such a repository is read rather than believed.
- **`change-control`'s exporters are tested against a canned server, not a live forge.** The HTTP
  layer is now exercised — the exporters accept `--api=`, so `test_collectors.py` stands up a
  stdlib server and drives a real export through it, including the 403 case. What is still unproven
  is the *shape* of what a real forge returns: field names, pagination behaviour and the response
  bodies are read from published documentation, not observed. The first live run found one bug this
  way (see below); treat the next one as something to read line by line too.
- **`change-control` infers an admin merge from its shape.** A merge with no approving review is
  what the API shows; *why* it happened is not in the API. The exporter records the shape, says so
  in the reason text it writes, and the command tells you to replace it. Nothing automates the
  distinction between an administrator overriding protection and a repository that never required a
  review in the first place.
- **Nothing can discover who ran an agent.** `author_kind: agent` is derived from the forge's own bot
  flag, but no API knows which person started the run. `agent_operator` is therefore left empty and
  the validator refuses the manifest until a human fills it in. That is a deliberate refusal to
  guess, and it means the rule is only as good as the person answering.
- `audit-pack` has only ever assembled a scope of two controls. A real framework is a hundred or
  more, and neither the pack's readability at that size nor the push's call count at that size has
  been seen.

Run `:diff` before your first `:push`, and read it.

## Known gaps, stated rather than discovered later

- **No idempotency key is documented for evidence — but the ingredient now exists.** Noru's published
  API documentation documents upsert behaviour for assets and security findings; neither
  `createEvidence` nor `POST /v1/evidence/upload` documents a key, so both pieces still fall back to
  a client probe. Edit an evidence description in the Noru UI and the probe stops matching; a re-run
  uploads again. Recorded in each `piece.json` with what a documented key would let the piece drop.
  What changed is that Noru now computes a canonical content digest at capture and returns it, which
  is exactly the stable key the probe has been approximating. Closing this gap is no longer blocked
  on Noru computing anything — only on the API documenting the digest as an upsert key and honouring
  it on write.
- **No piece is `mode: single_call`.** Every one fans out several individually-keyed writes because
  the published API offers no single ingest operation for these artifacts. Every one declares
  `collapses_to`. `iac-scan` is the closest: its writes are documented server-side upserts, so the
  remaining debt there is one call per finding rather than any question about correctness.
- **`audit-pack` produces an output the contract does not describe.** `piece.json` declares the
  manifest a piece writes; there is no field for a deliverable it renders for a human. The pack
  under `.noru/audit-pack/` is documented in the piece README and is only ever rendered from a
  validated manifest, but nothing machine-readable says it exists, so nothing checks it.
- **`iac-scan` closes a finding when no rule reproduces it, which is not the same as fixed.** A rule
  that was renamed, a file that moved out of scope, and a misconfiguration that was genuinely
  remediated all look identical from here. The plan says so in its reason text and the push command
  tells the user to say which kind of close it was; nothing automates that distinction.
- **`review-signoff` sets its expiry in a second, dependent call.** The published `createEvidence`
  input fields do not carry an expiry, so the record is created first and the expiry applied to it
  afterwards. A push interrupted between the two leaves a sign-off without its expiry; re-running
  the piece repairs it.
- **`governance-records` creates rather than updates when an account is rewritten.** The marker
  includes a digest of the rendered record, so re-filed minutes become a second record. For an
  account of a meeting that is arguably correct — an auditor should see both — but it is a
  consequence of having no documented key, not a decision anyone made.
- **The two YAML loaders still disagree about YAML 1.1 booleans, but a file can no longer walk into
  it unnoticed.** PyYAML resolves `yes`, `no`, `on` and `off` to booleans; the bundled fallback
  leaves them as strings. This entry used to say "no fixture is written that way, so nothing fails
  today" — and then one was: `change-control` named an approval's date field `on`, PyYAML read the
  *key* as `True`, and every local run passed while the CI matrix failed. `check_repo.py` now
  rejects a YAML 1.1 boolean word used as a key or an unquoted value anywhere under `plugins/`,
  `contract/` or `tests/`, with GitHub Actions workflows carved out because `on:` is GitHub's own
  required syntax there. The divergence itself is unchanged and still a property of the machine; what
  is fixed is that nothing in this repository can rely on it by accident.
- **The MCP `push` does not perform the writes.** It emits the confirmed call list for the client to
  execute, because a script cannot speak MCP without handling a credential. The gate is enforced in
  the script; the execution is the agent's, and an agent that improvises a call outside the list has
  stepped outside the reviewed plan. That is a real residual risk, not a solved problem.
- **Framework identifiers come back as ids, not display names.** Pieces read and store the ids, and
  never try to reconstruct a display name from them.
- **The delta covers the policy step only.** `--base-ref` compares committed *manifests*, which is
  what makes a policy finding attributable to a branch. Drift is still a digest with no "before" to
  compare against, for the reason below.
- **CI mode's drift check is a digest, not a diff against the base branch.** It answers "does the
  manifest match the repository as it is now", which is the right question, but it cannot say what a
  particular pull request changed: the previous derived facts live in `.noru/.cache/` and are not
  committed. So the itemisation under a drift failure can include things that were already untracked
  before the change, and it is empty when only line numbers moved — it says so when that happens.
- **The policy gate trusts a file that people edit.** Nothing offline distinguishes an agreed
  taxonomy from one widened on Tuesday to make a build green. The baseline carries an owner, a date
  and an expiry, and widening it is a diff in a pull request rather than a silent setting — but the
  only thing that actually reconciles it against what the organization agreed is a credentialed job
  reading Noru, and that job is not written yet. Until it is, the floor is trusted.
- **The policy gate sees stored columns, not flows.** It reads what a data map says a repository
  holds. Personal data sent to a third party, written to a log, or placed in a prompt without ever
  being stored is invisible to it. `ai-inventory` covers the model-call half; third-party egress is
  covered by nothing.
- **An empty data map satisfies the policy gate.** `privacy-datamap` reads five schema formats, so a
  repository whose schema lives only in TypeORM, Mongoose, ActiveRecord, Ecto or a Zod DTO produces
  an empty map — and an empty map contains no unpermitted category. The gate is sound and its input
  may be silently incomplete, which is the more dangerous of the two failure modes and is not yet
  closed.
- **CI mode checks the repository's record, not Noru's.** Offline it cannot see whether a control is
  still satisfied, whether the evidence is still linked, or whether someone deleted the record last
  week. A fork pull request gets the two local gates and nothing else, which is the honest ceiling
  of a check with no credential.
- **A queue-driven piece has little to check offline.** Every piece except `ai-inventory` builds its
  manifest from a queue Noru serves, so without it the collector cannot run and CI mode reports
  `skipped`. The expiry half still works on a committed manifest; the
  drift half does not.
- **Nothing offline can tell a considered expiry from a convenient one.** An `expires_at` set two
  years out to stop a build complaining passes every check here. `--max-age-days` is the blunt
  ceiling; a manifest-declared anchor is the sharp one — a `cadence` in `review-signoff`, the day the
  configuration was observed in `iac-scan`, the end of the audit window in `audit-pack`.
