# CI mode

The commands are the same three moves a person runs — `:scan`, `:diff`, `:push`. CI mode runs them
with nobody watching:

```text
scan  →  validate  →  expiry  →  policy  →  (diff  →  push)
```

The first four need **no network and no credential**. That is the design constraint, not an
accident: a pull request from a fork has no secrets, and a check that only works for people with
write access is not a check, it is a report someone remembers to run.

Three things fail a build. Two are computable from the repository and a calendar; the third needs
one more committed file, and no network either.

## 1. Manifest drift

The collector is deterministic and offline. It reduces everything it found in the repository to a
`derived_digest`, and stamps that digest into `source.derived_digest` in the committed
`.noru/<piece>.yml`. CI re-runs the collector and compares.

If the digests differ, the record no longer describes the code: someone added a model provider,
moved a review, dropped an artifact — and did not update the manifest. There is nothing to fetch
and nothing to authenticate; the answer is a hash of local files against a field in a committed
file.

The failure is readable. Alongside the digests, the report lists what the collector found in the
repository that the manifest names nowhere, with the `file:line` where each was first seen:

```text
  FAIL [drift] .noru/ai-inventory.yml: the committed manifest no longer matches the repository
         present in the repository, named nowhere in the manifest:
           + frameworks[0]: vercel-ai-sdk  (first seen at src/summarize.ts:1)
           + models[0]: claude-sonnet-4-5  (first seen at src/summarize.ts:5)
```

Three states produce the same signal and the message says which one you are in: no committed
manifest at all, a manifest with no recorded digest, or a manifest whose digest no longer matches.

**What the itemisation is and is not.** The digest is the gate; the list underneath is an
explanation. It compares *now* against the *manifest*, not against the previous commit — the
collector's derived facts live in `.noru/.cache/`, which is deliberately not committed, so there is
no "before" to diff against. In practice that means the list can include things that were already
untracked before this change, and it is empty when the difference is only in line numbers. It says
so when that happens:

```text
         the derived facts changed but nothing named appeared or disappeared — the difference is
         in line positions or counts, so re-run :scan and read the manifest diff
```

## 2. An expired interpretation

Contract requirement 8 puts an `interpretation` block on every claim — `owner`, `decided_at`,
`expires_at`, `rationale`, `refs[]`. The piece validators check that the block is present and
well-formed and say nothing about whether the dates have passed, on purpose: a fixture that starts
failing on a Tuesday is a worse test than no test.

Time is CI mode's job. [`scripts/check_expiry.py`](../scripts/check_expiry.py) walks the validated
manifest, finds every mapping that carries an `interpretation`, and reports:

| kind | meaning | fails by default |
|---|---|---|
| `expired` | the expiry is in the past. Nobody has stood behind this claim since it went stale | yes |
| `cadence` | outside the review cadence this pipeline declared with `--max-age-days` | yes |
| `unparsable` | a date that cannot be compared, so the expiry cannot be trusted | yes |
| `expiring` | expires inside the warning window (default 30 days). A heads-up, not a gate | no |
| `unbounded` | no expiry at all — permitted by the contract for a point-in-time procedural claim | no |
| `dangling_ref` | a `file:line` citation that no longer resolves: the file is gone, or the line is past its end | no |

`interpretation.expires_at` is the field the contract requires. Where a piece also records the
expiry of the record it is about to create — `expiry_date` on an evidence upload — that field is
compared the same way: a record that expires in Noru next week is not evidence of anything the week
after.

### Two kinds of cadence, and who checks which

- **A cadence the manifest declares.** `review-signoff` puts `cadence: quarterly` on a review, and
  its validator enforces that `expires_at` lands inside the window that cadence implies, against its
  own bundled vocabulary. CI mode does not re-implement that rule; it runs the validator, which is
  step 2, and a violation surfaces as `invalid` (exit `5`).
- **A cadence the pipeline declares.** `--max-age-days=N` is the review cadence *this branch*
  insists on, for any piece, including ones with no cadence field. It reports a claim last decided
  more than N days ago, and a claim declaring a review window longer than N days. It is **off by
  default** (`0`) — the tool does not invent a compliance opinion you did not state.

A validator that accepts its own `--as-of` (as `review-signoff`'s does) is deliberately **not**
given one here. Time is checked once, in the expiry step, so one stale claim cannot come back as two
findings with two different exit codes. Validation stays a question about the file; expiry is the
question about the day.

## 3. Personal data nobody agreed to

Drift asks whether anybody *looked*. It does not ask whether the answer was allowed to be yes.

That gap is the whole reason this step exists. Add a `passport_number` column and the drift gate
fires; re-run `:scan`, re-sign the collection, thirty seconds, green. Nothing anywhere in the
repository ever said you were not permitted to collect it. Drift is a **"someone looked"** gate;
this is the **"this is permitted"** gate, and it needs an artifact drift does not:

`.noru/privacy-baseline.yml` — the agreed taxonomy. Which data categories the organization permits,
which purposes it approved, whose data it covers, and the combinations that are fine apart and
forbidden together.

```yaml
version: 0.1.0
kind: privacy-baseline
source:
  pinned_from: { fetched_at: 2026-08-27T09:00:00Z, via: [getPrivacyTaxonomy, getRopa] }
data_categories:
  allow: [user.contact, user.device]
  deny:  [user.biometric]
data_uses:
  allow: [essential, analytics.reporting]
forbidden_pairs:
  - categories: [user.health_and_medical]
    uses: [marketing]
    reason: No lawful basis was ever established for marketing on health data.
scopes:
  - dataset: payments_db
    confine: [user.financial.credit_card]
    reason: Card numbers live in the PCI-scoped store and nowhere else.
interpretation:
  owner: a.person@example.com
  decided_at: 2026-08-01
  expires_at: 2027-08-01
  rationale: Agreed at the 2026 H2 privacy review; pinned from Noru.
```

### Noru is the truth; this file is the floor

The agreed taxonomy is an **organizational** fact. Noru holds it — `getPrivacyTaxonomy`,
`getPrivacySettings`, `getRopa` and `listProcessingActivities` are where it lives, and this
repository's first non-goal is that a piece never becomes a second register.

So why is there a file at all? Because a privacy gate that needs a credential cannot run on a fork
pull request, and a check that only runs for people with write access is not a check. The file is a
**pinned floor**, exactly as `contract/lib/taxonomy/` is the offline floor for the Fideslang
vocabulary, and it obeys the same rule stated in
[`contract/README.md`](../contract/README.md): a job that *can* reach Noru reconciles against it and
reports a difference. A repository that silently prefers what is on disk has re-created the drift
problem one layer down. `source.pinned_from` records which Noru state the floor was taken from,
because nobody can otherwise tell a floor one day behind from one two years behind.

The baseline is also a **claim**, so it carries an interpretation block and the expiry step ages it
like everything else. A policy nobody has re-owned since 2023 is not a policy, and a gate standing
on one is not a gate.

### What it reports

| kind | meaning | fails by default |
|---|---|---|
| `unpermitted_category` | a data category the baseline does not allow, or denies | yes |
| `unpermitted_use` | a purpose the baseline does not allow | yes |
| `unpermitted_subject` | a data subject the baseline does not cover | yes |
| `unpermitted_pair` | a category and a use that are permitted apart and forbidden together | yes |
| `confined_category` | a category confined to one dataset or system, found somewhere else | yes |
| `undeclared_system` | a system processing personal data that a closed baseline does not name | yes |
| `special_category` | GDPR Article 9 or Article 10 data in the map | no |

`special_category` is advisory on purpose. Whether Article 9 data is *permitted* is what the
category rules answer; this finding exists so a reviewer never has to go looking for the
highest-risk thing in the map. Gate on it with `--fail-on` if you want a second pair of eyes on
every one.

### Matching is by prefix, and the more specific rule wins

Fideslang is a tree, so the baseline is written in terms of subtrees or it becomes an enumeration
nobody maintains. `user.contact` covers `user.contact.email`. `user.biometric` in `deny` covers
`user.biometric.fingerprint`. The boundary is a dot, so `user.contact` does **not** cover
`user.contacts_import`.

Where an `allow` and a `deny` both match, the longer pattern wins. That is what makes "no financial
data, except the card number in payments" two lines instead of a list.

### Which of these did *this* pull request introduce?

A team turning this on for the first time has a backlog, and a gate that blocks on the backlog on
day one is a gate that is reverted on day two. `--base-ref` is what makes adoption possible:

```bash
python3 scripts/ci_check.py --piece=privacy-datamap --base-ref=origin/main --gate-on-new
```

It reads the committed manifest as it stood at the **merge base** — one `git show`, no second
checkout, no collector re-run — evaluates it against today's baseline, and stamps every finding
`first_seen: this_pr` or `pre_existing`. `--gate-on-new` then gates only on the first kind. The
backlog is still reported in full; it just stops blocking while it is burned down.

The base manifest is judged against **today's** baseline, not the baseline as it stood then. The
question is "which of these is this branch responsible for", and widening the policy is a change to
the policy, judged in its own diff.

Two failure modes, and the direction each falls in:

- **The base cannot be resolved** — a `depth: 1` clone does not contain it. The step says so, no
  finding is stamped, and **everything gates**. A delta nobody can compute must not quietly disable
  the gate. Fetch the base branch (`fetch-depth: 0`) or drop the flag.
- **No manifest existed at the base** — every finding is `this_pr`, which is exactly right for a
  branch that adds the data map.

This also closes half of what "Where this is weaker than it looks" says about drift below: the
policy step now *can* say what a pull request changed. Drift still cannot, because the collector's
previous derived facts are not committed.

### No baseline is a skip, never a pass

A repository that has not agreed a taxonomy yet has nothing for this step to check, and this tool
does not ship a default policy — inventing one is the "ship an opinion" failure requirement 9 exists
to prevent. So the step reports `skipped` and names the file it looked for.

Passing `--baseline=` at a path that does not exist is different: that is a **broken gate**, and it
exits `6` even under `--mode=warn`.

```bash
python3 scripts/check_policy.py .noru/privacy-datamap.yml --baseline=.noru/privacy-baseline.yml
```

## Exit codes

The per-step tools keep the house convention (`0` fine, `1` the thing you checked is wrong, `2` you
called it wrong). The orchestrator adds codes above `2` so a workflow can react differently to each
gate without parsing text. Nothing is added to the meaning of `0`, `1` or `2`.

| code | meaning |
|---|---|
| `0` | every requested check passed — or `--mode=warn`, or nothing had any input to work on |
| `1` | more than one distinct failure condition fired; read the report |
| `2` | usage error: an unknown flag, an unknown piece, a date that is not a date |
| `3` | **manifest drift** |
| `4` | **an expired interpretation**, or one outside the declared cadence |
| `5` | the manifest failed validation |
| `6` | a check could not run at all: no `node`, no `python3`, an unreadable declaration, a child that crashed |
| `7` | **personal data the privacy baseline does not permit** |

`dangling_ref` is off by default; if you add it to `--fail-on` it exits `3` alongside drift, because
a citation that no longer resolves is the same class of failure — the record has come loose from the
code.

`6` is the one that matters most and the one people forget. A check that could not run is not a
check that passed, and `--mode=warn` does **not** suppress it — a broken gate should be loud even
while the findings are still advisory.

The standalone tools:

| tool | `0` | `1` | `2` |
|---|---|---|---|
| `scripts/check_expiry.py` | nothing that `--fail-on` covers | at least one such finding | usage / unreadable file |
| `scripts/check_policy.py` | nothing that `--fail-on` covers | at least one such finding | usage / unreadable file / not a baseline |
| each piece's `validate_manifest.py` | valid, warnings allowed | validation errors | usage / unparseable YAML |
| each piece's `collect.mjs --check` | matches | drift | usage / IO error |

## Warn-only mode

Turning on a gate that has never run is how a gate gets reverted. Start here:

```bash
python3 scripts/ci_check.py --piece=ai-inventory --mode=warn
```

`--mode=warn` runs exactly the same checks, reports exactly the same findings, labels the ones that
*would* block, and exits `0`. The JSON report carries `"status": "warn"` and the same
`counts`, so a dashboard cannot tell the difference — only the build can.

Two intermediate steps between "warn" and "gate", if you want them:

```bash
# gate on expiry only, keep drift advisory while the team catches the manifest up
python3 scripts/ci_check.py --piece=ai-inventory --fail-on=expired

# gate on everything except the two advisory kinds (this is the default)
python3 scripts/ci_check.py --piece=ai-inventory --fail-on=drift,invalid,expired,cadence,unparsable
```

`--fail-on=none` is the same as `--mode=warn` for findings, but still exits `6` on a tooling failure.

## The GitHub Action

```yaml
name: compliance
on: [pull_request]

permissions:
  contents: read

jobs:
  inventory:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - uses: noru-tech/noru-grc-engineering/.github/actions/noru-ci@v0.4.1
        with:
          piece: ai-inventory
          mode: warn        # switch to gate once the report is quiet
```

This works on a pull request from a fork. It needs no secret, and it will not ask for one.

Pin a release tag, never a branch — a compliance gate that changes under you is worse than no gate.
This action is not part of `v0.1.0`; use the tag of the first release that carries it.

### Inputs

| input | default | what it does |
|---|---|---|
| `piece` | *(required)* | which piece to run |
| `repo` | `.` | path to the repository to check |
| `mode` | `gate` | `gate` fails on a finding, `warn` reports and exits `0` |
| `steps` | `scan,validate,expiry,policy` | add `diff` and `push`, or use `all` |
| `fail-on` | *(tool default)* | which finding kinds gate the build, or `none` |
| `baseline` | `.noru/privacy-baseline.yml` | the agreed taxonomy for the policy step. Absent → skipped; a path that does not exist → a tooling failure |
| `base-ref` | *(none)* | compare against the merge base with this ref, so findings say whether this pull request introduced them. Needs `fetch-depth: 0` |
| `gate-on-new` | `false` | gate only on findings this branch introduced. Requires `base-ref`; without it nothing is stamped and everything gates |
| `max-age-days` | `0` | the review cadence this pipeline declares. `0` declares none |
| `warn-within-days` | `30` | how far ahead to report an expiry that has not passed yet |
| `as-of` | *(today)* | evaluate expiry against a fixed date. For testing |
| `state` | *(none)* | a Noru state snapshot, so the `diff` step has something to compare against |
| `on-missing-prerequisite` | `skip` | `fail` turns a step with no input into a tooling failure |
| `plugins` | *(bundled)* | where the pieces live, if not the ones shipped with the action |
| `require-yaml-loader` | *(report only)* | assert the runner's YAML loader is `pyyaml` or `fallback` |
| `summary` | `true` | write a job summary table |

### Outputs

`status`, `exit-code`, `report` (path to the JSON), `drift` (`true`/`false`), `expired` (a count),
`unpermitted` (a count of the policy findings that gate this run — the advisory ones are excluded,
so a green build never reports a number here).

### What the action assumes about the runner, stated out loud

It installs **nothing**. It needs `node` and `python3` already on the runner and fails with a clear
message if either is missing — adding an install step would break the atomicity promise in
[CONTRIBUTING.md](../CONTRIBUTING.md) and hide it inside an action nobody reads.

That leaves one environment assumption worth naming, because it has already caused a real bug here:
the manifest validators use **PyYAML when it is importable and a bundled fallback parser otherwise**,
so *which loader runs is a property of the runner image, not of this repository*. GitHub's
`ubuntu-latest` system `python3` has PyYAML; a job that installed `actions/setup-python` usually does
not. The action prints which loader it got on every run, and `require-yaml-loader: pyyaml` (or
`fallback`) turns that into an assertion, so a runner image change fails the build instead of
quietly switching parser underneath your compliance gate.

Within a run, the orchestrator invokes each validator with the **same interpreter it is running on**,
never with whatever `python3` resolves to at that moment, so the loader cannot change between the
step that validated a manifest and the step that read it.

Pinning the loader is still worth doing even though no piece's *plan* depends on it any more:
`scripts/test_idempotency.py` parses the same manifest with both loaders and asserts the plan is
identical, and `scripts/test_validators.py` holds the fallback to what PyYAML produces. What the pin
buys is a named failure instead of a silent switch — and the loaders are not yet interchangeable on
everything, so see the Known gaps in [verification.md](./verification.md).

## The push half, and why it is a separate job

`:diff` compares the manifest against the organization, and `:push` writes to it. Neither is
possible without a credential, so both are opt-in and both degrade rather than fail:

- with no `NORU_API_KEY` in the environment, the `push` step reports
  `skipped: NORU_API_KEY is not present…` and the build stays green. The offline checks above it
  still ran and still gate.
- with no state snapshot, the `diff` step reports `skipped` for the same reason. `:diff` needs to
  know what is already in Noru, and reading that needs a read-scoped connection.

```yaml
  land-it:
    # Only where secrets exist, and only off the release path. Never on pull_request_target.
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment: compliance     # so a human can require an approval on the write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - uses: noru-tech/noru-grc-engineering/.github/actions/noru-ci@v0.4.1
        env:
          NORU_API_KEY: ${{ secrets.NORU_API_KEY }}
        with:
          piece: evidence-push
          mode: gate
          steps: all
          state: .noru/.cache/noru-state.json
```

Rules this obeys, and that a reviewer should check it still obeys:

- the key is passed in `env:` from a secret and **never** as a `with:` input, so it does not travel
  through the action's inputs
- the orchestrator never reads its value. It checks only whether the variable is *present*, and
  passes the environment through to the piece's own push entrypoint, which reads it at the point of
  use
- every step that does not push runs with `NORU_API_KEY` **removed** from its child environment
- everything captured from a child process is passed through the same redaction the plugins use
  before it can reach a log, the report or a job summary
- `:push` still refuses to act without `--confirm` and a plan generated from the manifest bytes
  currently on disk. Putting `steps: all` in a workflow is that confirmation, given once, in a file
  that goes through review — which is why it belongs on a protected branch with an environment, not
  on `pull_request`

## A whole pipeline, end to end

[`.github/workflows/compliance.yml`](../.github/workflows/compliance.yml) in this repository is the
worked example, running against this repository. Copy it and change three things: the piece list,
the window, and whether the jobs are `warn` or `gate`.

The shape it demonstrates:

| job | credential | gates on |
|---|---|---|
| privacy | none | drift, coverage, expiry, and the privacy baseline. Runs on a fork pull request |
| change-control | a **forge** token, not a Noru one | who wrote, approved, merged and deployed each change in the window |
| land-it | `NORU_API_KEY`, behind an `environment:` | the write. Off the pull-request path, on a protected branch only |

Four things in it are worth copying deliberately rather than by accident:

- **`fetch-depth: 0`** on the checkout, or `--base-ref` cannot resolve the merge base and every
  finding gates instead of only the new ones.
- **The forge token is not the Noru token.** The exporter reads `GITHUB_TOKEN` or `GITLAB_TOKEN` at
  the point of use; the collector that consumes its output takes no credential at all, which is what
  keeps the offline half offline.
- **The export is deleted at the end of the job.** It is a list of who reviewed what and when —
  personal data about colleagues — and an artifact store keeps things for ninety days.
- **The write job is separate, on `main`, behind an environment.** Never on `pull_request_target`.

## The generic recipe

Nothing here is GitHub-specific. The action is a thin wrapper over one command:

```bash
python3 scripts/ci_check.py \
  --piece=ai-inventory \
  --repo="$PWD" \
  --mode=gate \
  --max-age-days=365 \
  --report=ci-report.json \
  --output=json --quiet
```

No TTY, no prompts, no colour, no interactive `git`. Everything it prints on stdout with
`--output=json` is a single JSON document; everything else goes to stderr.

Get the toolkit onto the runner however that CI system prefers — a git clone at a tag, a vendored
submodule, a cached artifact. There is nothing to build and nothing to install.

### GitLab CI

```yaml
compliance:
  image: node:20
  before_script:
    - apt-get update -qq && apt-get install -y -qq python3 git
    - git clone --depth 1 --branch v0.2.0 https://github.com/noru-tech/noru-grc-engineering /opt/ngce
  script:
    - python3 /opt/ngce/scripts/ci_check.py --piece=ai-inventory --repo="$CI_PROJECT_DIR"
        --plugins=/opt/ngce/plugins --mode=gate --report=ci-report.json --output=text
  artifacts:
    when: always
    paths: [ci-report.json]
```

### A plain shell gate

```bash
#!/usr/bin/env sh
# Exit codes are the interface. Anything a pipeline needs to branch on is here.
python3 "$NGCE/scripts/ci_check.py" --piece="$1" --repo="$PWD" --output=text
case "$?" in
  0) echo "compliance: clean" ;;
  3) echo "compliance: the manifest is stale — re-run the piece's :scan and commit it"; exit 1 ;;
  4) echo "compliance: an interpretation expired — someone has to re-own it"; exit 1 ;;
  5) echo "compliance: the manifest is invalid"; exit 1 ;;
  6) echo "compliance: the check could not run — this is a tooling problem"; exit 1 ;;
  7) echo "compliance: personal data nobody agreed to — read the baseline, or stop collecting it"; exit 1 ;;
  *) echo "compliance: failed"; exit 1 ;;
esac
```

### Only the policy half

`check_policy.py` stands alone too, and takes a manifest and a baseline directly:

```bash
python3 scripts/check_policy.py .noru/privacy-datamap.yml \
  --baseline=.noru/privacy-baseline.yml --output=json --quiet
```

### Only the expiry half

`check_expiry.py` stands alone and reads a manifest directly, if all you want is the calendar:

```bash
python3 scripts/check_expiry.py .noru/ai-inventory.yml --max-age-days=365 --output=json --quiet
```

## Where this is weaker than it looks

Written down here rather than discovered later.

- **Drift is a digest, not a diff against the base branch.** The check answers "does the manifest
  match the repository as it is now", which is the right question, but it cannot say *what this pull
  request changed* — the previous derived facts are not committed. Comparing against the merge base
  would need a second checkout and a second collector run.
- **A fork pull request cannot check anything that lives in Noru.** Whether a control is still
  satisfied, whether the evidence is still linked, whether someone deleted the record last week —
  none of that is visible offline. CI mode checks that the *repository's own record* is true and
  current. It does not check that Noru agrees with it.
- **A queue-driven piece has almost nothing to check offline.** Every piece except `ai-inventory`
  builds its manifest from a queue Noru serves. Without that queue the collector cannot run at all,
  and the job reports `skipped`, not `pass`. The expiry half still works on a committed manifest;
  the drift half does not.
- **The policy gate is only as good as the baseline, and the baseline is a file people edit.**
  Nothing offline can tell an agreed taxonomy from one widened last Tuesday to make a build go
  green. Three things make that visible rather than impossible: the baseline carries an
  interpretation block, so widening it has an owner and a date; it expires, so it cannot be widened
  once and forgotten; and it is a committed file, so widening it is a diff in a pull request rather
  than a setting somebody changed. Reconciling it against Noru in a credentialed job is what closes
  the loop, and that job is the one place the difference between the floor and the agreed truth is
  visible at all.
- **The policy gate sees stored columns, not flows.** It reads what a data map says a repository
  *holds*. Personal data that is never stored but is sent to a third party, written to a log, or put
  in a prompt does not appear in a schema and so does not appear here. `ai-inventory` covers the
  model-call half; nothing yet covers third-party egress.
- **An empty data map is now caught, but a *partial* one is only reported.** The collector looks
  for the schema shapes it knows it cannot parse — TypeORM, Mongoose, Sequelize, ActiveRecord, Ecto,
  GORM, OpenAPI, JSON Schema, Zod — and a repository where it parsed *nothing* and found one of
  those is exit `6`, not a pass. Where it parsed something and still missed something, that is a
  `coverage` finding which is advisory by default: failing there would block every repository with
  one Zod file beside its SQL. Gate on it with `--fail-on=coverage` when the map is meant to be
  complete. What none of this catches is a schema in a shape nobody wrote a marker for.
- **Expiry is only as good as the dates people write.** Nothing offline can tell whether an
  `expires_at` was chosen thoughtfully or set to two years out to stop the build complaining.
  `--max-age-days` is the blunt instrument that puts a ceiling on that. The sharp one is the anchor
  a piece declares for itself — a cadence in `review-signoff`, the day the configuration was observed
  in `iac-scan`, the end of the audit window in `audit-pack` — which is why such a field is worth
  having in more pieces.

## A note on Action outputs

When the action fails the job, its declared outputs are not propagated — a GitHub composite-action
behaviour that `continue-on-error` does not change. `steps.<id>.outcome` still reports `failure`.
Read a failing run from the JSON report — `${{ runner.temp }}/noru-ci-<piece>.json` by default,
written before the action exits. That default is per-piece, not per-invocation, so set
`report-path` when one job calls the action twice or the second run overwrites the first; or run in `mode: warn` where outputs are populated. See the
action's own README for a worked example.
