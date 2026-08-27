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
python3 scripts/test_ci_mode.py        # CI mode fails on drift and on an expired interpretation
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
| **A plan does not depend on which YAML loader parsed the manifest** | `test_idempotency.py` re-spaces every prose string in a validated manifest the way the other loader would have written it, and asserts for all six pieces that the plan — markers, arguments, effects and reasons — is identical, both writing into an empty organization and writing into one the same plan has already been pushed into |
| No catalogue is vendored | every plugin file is scanned for catalogue-shaped evidence-item and control ids; fixtures may only use the reserved `E-ZZ-*` / `zz-*` namespaces |
| No credential leaks into the repository | the tree is scanned for credential-shaped strings |
| A scaffolded piece satisfies the contract | CI scaffolds one and runs the contract test against it |
| **CI mode fails on drift** | `test_ci_mode.py` adds a model provider to a copy of the fixture repo and asserts exit `3`, that the message names the provider and the `file:line` it arrived at, and that the gate clears again when the file is removed |
| **CI mode fails on an expired interpretation** | the same manifest with every `expires_at` moved into the past must exit `4`, name the owner, and *not* report drift — the code did not change |
| Both at once is distinguishable from either | exit `1`, with both kinds in the report |
| An invalid manifest is its own exit code | a fixture violating a piece's own cadence rule exits `5`, carries the validator's message through, and blocks the expiry step rather than passing it |
| Warn-only mode reports and does not fail | the identical findings, `"status": "warn"`, exit `0` |
| A check that could not run is not a pass | a piece whose queue is missing reports `skipped`, and `--on-missing-prerequisite=fail` exits `6` |
| CI mode is piece-agnostic | every piece in the marketplace is driven through it green, the orchestrator's source is checked for hardcoded piece names, and CI runs it against a freshly scaffolded piece |
| No credential reaches a step that does not push | asserted on the helper every step goes through: `NORU_API_KEY` is absent from the child environment except for `:push` |
| Missing credential degrades, not errors | with no `NORU_API_KEY` the push step reports `skipped` and the build stays green |
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
- `iac-scan`'s rules are text-and-block matchers, not a parser. They will miss a misconfiguration
  expressed through a module input, a variable default or a generated template, and they will fire
  on a resource that a later override makes safe. Read the citation before you accept the finding —
  which is the workflow the piece is built around, but it is a real recall and precision ceiling.
- `audit-pack` has only ever assembled a scope of two controls. A real framework is a hundred or
  more, and neither the pack's readability at that size nor the push's call count at that size has
  been seen.

Run `:diff` before your first `:push`, and read it.

## Known gaps, stated rather than discovered later

- **No idempotency key is documented for evidence.** Noru's published API documentation documents
  upsert behaviour for assets and security findings; neither `createEvidence` nor
  `POST /v1/evidence/upload` documents a key, so both pieces fall back to a client probe. Edit an
  evidence description in the Noru UI and the probe stops matching; a re-run uploads again. Recorded
  in each `piece.json` with what a documented key would let the piece drop.
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
- **The two YAML loaders still disagree; the pieces no longer care.** The validators use PyYAML
  where it is importable and a bundled fallback otherwise, and the two do not agree byte for byte on
  a folded (`>`) block scalar. Measured against PyYAML 6.0.3, the fallback drops the trailing
  newline the spec calls for, folds a blank line to a space where PyYAML makes a paragraph break,
  folds a more-indented line instead of keeping it, strips trailing spaces inside a folded line, and
  ignores the `+` chomping and explicit-indentation indicators. Every one of those is a whitespace
  difference inside prose, so every piece now normalises manifest free text before it reaches a
  rendered body, a content digest or a planned argument, and `scripts/test_idempotency.py` asserts
  for all six that the plan does not move — and for any piece added later, since it reads the pieces
  off disk. `scripts/templates/diff.mjs.tmpl` carries the same normalisation, so a scaffolded piece
  has it from birth. The divergence itself is unfixed: the loaders are still not interchangeable, and
  a piece that reads manifest prose without normalising it would reintroduce the bug. See the note
  below for the one divergence normalising cannot cover.
- **The bundled fallback loader silently truncates prose containing a `#`.** It strips comments line
  by line before it assembles a block scalar, so a rationale reading `tracked in ticket #4412 until
  the rollout completes` loads as `tracked in ticket rollout completes` without PyYAML, and intact
  with it. This is a content bug rather than an idempotency one and normalising whitespace does not
  touch it: the two loaders disagree about the *characters*, not the spacing. A manifest is more
  likely to hit this than it looks — issue numbers, `C#`, and a `#` in a URL fragment all qualify.
- **The MCP `push` does not perform the writes.** It emits the confirmed call list for the client to
  execute, because a script cannot speak MCP without handling a credential. The gate is enforced in
  the script; the execution is the agent's, and an agent that improvises a call outside the list has
  stepped outside the reviewed plan. That is a real residual risk, not a solved problem.
- **Framework identifiers come back as ids, not display names.** Pieces read and store the ids, and
  never try to reconstruct a display name from them.
- **CI mode's drift check is a digest, not a diff against the base branch.** It answers "does the
  manifest match the repository as it is now", which is the right question, but it cannot say what a
  particular pull request changed: the previous derived facts live in `.noru/.cache/` and are not
  committed. So the itemisation under a drift failure can include things that were already untracked
  before the change, and it is empty when only line numbers moved — it says so when that happens.
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
