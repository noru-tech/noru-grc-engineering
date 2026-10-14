# Known limitations

A short index of what this toolkit does not do, or does not yet prove. Each entry is a summary of a
statement already made, and argued in full, in the linked document; read that before relying on the
summary. Nothing here is new: if a limitation is missing from this list, the linked documents are
the authority.

## Maturity

From [docs/verification.md, "Maturity"](./docs/verification.md#maturity):

- **Reviewed and internally consistent, not field-tested.** Everything is proven against fixtures
  on every build; none of it has been exercised against a live organization at production scale.
  Treat a first run as something to check, not something to trust, and run `:diff` before your
  first `:push`.
- **`/noru:review` and `/noru:status` are agent-orchestrated.** Their routing and read-only
  capability matrix are tested; the host's cross-skill invocation and live MCP report assembly are
  not simulated.
- **Collector recall is unproven on large polyglot codebases.** `ai-inventory` will miss a provider
  reached through a hand-rolled HTTP client.
- **A detector-shaped repository is a pathological input for `ai-inventory`.** Anything that lists
  provider names, such as a scanner, a policy engine or an allowlist, is reported as containing them.
- **Article 50 disclosure states are a repository fact.** Whether they match the running product is
  what a scan cannot see.
- **`governance-records`, `evidence-push` and `audit-pack` have only met fixture inputs.** A real
  minute book, a real evidence catalogue and a full framework scope have not been seen.
- **The upload digest checks in `evidence-push` are not covered by an executed test**, because every
  path needs a live API. The digest proves the stored bytes are the uploaded bytes, not that they
  describe the running system.
- **`iac-scan` rules are text-and-block matchers, not a parser.** They miss misconfiguration
  expressed through module inputs, variable defaults or generated templates, and can fire on a
  resource a later override makes safe.
- **`findings-ingest` cannot see what else imports a report.** `reaches_forge_alert_store: false`
  is a statement a person makes; nothing checks it against the organization's integrations, so a
  wrong one files results twice. It reads SARIF 2.1.0 and OSV-Scanner JSON only, and a result in a
  file that is not in the checkout is counted, not filed.
- **`change-control` exporters are tested against a canned server, not a live forge.** An admin
  merge is inferred from its shape, and nothing can discover who ran an agent: `agent_operator` is
  left for a human to fill in.

## Known gaps

From [docs/verification.md, "Known gaps"](./docs/verification.md#known-gaps-stated-rather-than-discovered-later):

- **No idempotency key is documented for evidence**, so evidence writes fall back to a client
  probe; editing an evidence description in Noru can make a re-run upload again.
- **No piece is `mode: single_call`.** Each fans out several individually keyed writes.
- **`audit-pack`'s rendered pack is not described by the contract**, so nothing machine-readable
  checks it.
- **`iac-scan` closes a finding when no rule reproduces it**, which is not the same as fixed.
  `findings-ingest` does the same when no input reports it.
- **`review-signoff` sets its expiry in a second, dependent call**; an interrupted push leaves a
  sign-off without its expiry until the piece is re-run.
- **`governance-records` creates rather than updates** when an account is rewritten.
- **The two YAML loaders disagree about YAML 1.1 booleans**; `check_repo.py` stops a file relying on
  it, but the divergence is a property of the machine.
- **The MCP `push` does not perform the writes.** It emits the confirmed call list. In an
  interactive host the agent executes it, and an agent that improvises a call outside it has
  stepped outside the reviewed plan. In CI, `scripts/mcp_exec.mjs` executes exactly that list and
  refuses one that differs from the plan.
- **The id a create returns is read from an undocumented result shape.** Where a call depends on an
  earlier create (`depends_on`), the executor takes the result's `id`, or the one nested object's
  `id`; the published tool descriptions do not document the result. If neither is there, the run
  stops before the dependent call.

## CI mode

From [docs/ci-mode.md, "Where this is weaker than it looks"](./docs/ci-mode.md#where-this-is-weaker-than-it-looks):

- **Drift is a digest, not a diff against the base branch.** It cannot say what a pull request
  changed.
- **CI mode checks the repository's record, not Noru's.** A fork pull request cannot see whether a
  control is still satisfied or a record still exists.
- **Two pieces cannot build their snapshot headless.** `ai-inventory` needs a person to say which
  frameworks are the AI ones, and `privacy-datamap` needs judgement about what the privacy tools
  return. Their `state` step reports `skipped`; pass a snapshot with `--state` to run `diff`.
- **A queue-driven piece has almost nothing to check offline.** Every piece except `ai-inventory`
  reports `skipped` without the queue Noru serves; the expiry half still works.
- **The policy gate trusts a baseline that people edit**, and sees stored columns, not flows:
  personal data sent to a third party, a log or a prompt without being stored is invisible to it.
- **A partial data map is reported, not gated, by default.** Gate on it with `--fail-on=coverage`.
- **Expiry is only as good as the dates people write.** `--max-age-days` is the ceiling.
- **Composite action outputs are empty when the action fails the job.** Read the JSON report.
- **GitLab shows the security report in a merge request on Ultimate only, and ingests it only from a
  job that succeeded**, so a failing gate withholds it; Code Quality reaches every tier.
  `findings-ingest` never fills one, and an `iac-scan` finding arrives twice where an integration
  imports GitLab's vulnerability report; `security-findings: omit` turns it off.
- **Confirmation by a reviewed plan is GitHub-only.** It reads GitHub's pull request and artifact
  APIs; the GitLab `noru-ci.yml` has no `fetch-reviewed-plan`.
- **The CI/CD Catalog needs a GitLab-hosted component project.** Until one is published, the GitLab
  components are included with `include: remote` from a release tag of this repository.
- **An agent's renewal is judged against one parent on a push.** Without `--base-ref`, a push of
  several commits is compared with its last commit's parent only. Gate the pull request with
  `--base-ref`. A team alias as operator is caught by its usual shapes, not by knowing your teams;
  the policy's named list of operators, behind CODEOWNERS, is the control. See
  [docs/ci-mode.md](docs/ci-mode.md#where-this-is-weaker-than-it-looks).

## Repository enforcement

From [docs/repository-enforcement.md, "Boundaries"](./docs/repository-enforcement.md#boundaries):

- **A local scheduled monitor is defence in depth, not the authority.** Somebody able to weaken the
  repository can remove it; strong drift monitoring belongs outside the protected repository.
- **Merge approval confirms only a reviewed plan.** A passing merge confirms the plan its pull
  request showed for review, and nothing else: the pull request came from this repository, its
  plan came from the declared workflow, which the merge did not change, acc found an approval
  independent of the author, the merge changed the manifest, and the plan rebuilt after the merge
  is the same. Otherwise Noru publication remains each piece's own
  `diff`, confirmation and `push`.

## Non-goals

From [contract/README.md, "Non-goals"](./contract/README.md#non-goals-stated-so-they-can-be-pointed-at):
no local register duplicating Noru, no framework control text, no credential handling in a piece, and no SaaS
connectors.
