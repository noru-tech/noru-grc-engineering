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
- **`review-signoff` sets its expiry in a second, dependent call**; an interrupted push leaves a
  sign-off without its expiry until the piece is re-run.
- **`governance-records` creates rather than updates** when an account is rewritten.
- **The two YAML loaders disagree about YAML 1.1 booleans**; `check_repo.py` stops a file relying on
  it, but the divergence is a property of the machine.
- **The MCP `push` does not perform the writes.** It emits the confirmed call list; an agent that
  improvises a call outside it has stepped outside the reviewed plan.

## CI mode

From [docs/ci-mode.md, "Where this is weaker than it looks"](./docs/ci-mode.md#where-this-is-weaker-than-it-looks):

- **Drift is a digest, not a diff against the base branch.** It cannot say what a pull request
  changed.
- **CI mode checks the repository's record, not Noru's.** A fork pull request cannot see whether a
  control is still satisfied or a record still exists.
- **A queue-driven piece has almost nothing to check offline.** Every piece except `ai-inventory`
  reports `skipped` without the queue Noru serves; the expiry half still works.
- **The policy gate trusts a baseline that people edit**, and sees stored columns, not flows:
  personal data sent to a third party, a log or a prompt without being stored is invisible to it.
- **A partial data map is reported, not gated, by default.** Gate on it with `--fail-on=coverage`.
- **Expiry is only as good as the dates people write.** `--max-age-days` is the ceiling.
- **Composite action outputs are empty when the action fails the job.** Read the JSON report.

## Repository enforcement

From [docs/repository-enforcement.md, "Boundaries"](./docs/repository-enforcement.md#boundaries):

- **A local scheduled monitor is defence in depth, not the authority.** Somebody able to weaken the
  repository can remove it; strong drift monitoring belongs outside the protected repository.
- **Merge approval is never permission to publish.** Noru publication remains each piece's own
  `diff`, confirmation and `push`.

## Non-goals

From [contract/README.md, "Non-goals"](./contract/README.md#non-goals-stated-so-they-can-be-pointed-at):
no local register duplicating Noru, no framework control text, no credential handling, and no SaaS
connectors.
