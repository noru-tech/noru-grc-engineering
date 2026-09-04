# repo-enforcement

An optional utility plugin that turns the suite's review artifacts into an enforceable GitHub merge
boundary. It is deliberately not a last-mile piece: it creates no Noru claim and has no
`scan/diff/push` fiction.

The flow is `inspect -> local file plan -> reviewed PR -> GitHub plan -> explicit confirm -> apply ->
verify`. PR validation remains offline. GitHub administration uses an existing authenticated `gh`
session and never requests a pasted token. Noru writes remain separate.

## Commands

| Command | Mutation |
|---|---|
| `/repo-enforcement:setup` | none; prepares a local file plan |
| `/repo-enforcement:status` | none |
| `/repo-enforcement:work` | none; inspects one exact debt item and prepares a reviewed remediation |
| `/repo-enforcement:plan` | none; writes an ignored, expiring plan |
| `/repo-enforcement:apply` | creates or updates the dedicated ruleset after confirmation |
| `/repo-enforcement:verify` | none |

## Files installed in a target repository

- `.noru/enforcement.yml` — desired policy.
- `.noru/enforcement-baseline.json` — exact, expiring legacy-debt acceptances in ratchet mode.
- `.github/workflows/noru-grc.yml` — always-running, credential-free validation.
- `.github/CODEOWNERS` — merged managed block protecting GRC policy and workflow paths.

Cache-only candidates and plans live under `.noru/.cache/`.

`enforce.py baseline worklist` recomputes the current backlog and groups it by piece and named
owner, with blocking, stale-cleanup, due-soon, and scheduled items. `baseline inspect` selects one
exact fingerprint and routes it into the owning piece. The work flow may prepare a PR, but it cannot
approve an agent proposal, silently remove an acceptance, merge, or publish to Noru.

Repository ruleset mutation is supported first. Organisation rulesets, custom-property targeting,
and centrally required workflows are documented in `references/rollout-modes.md` and visible during
inspection, but organisation mutation is intentionally refused until its separate permission and
pilot phase is implemented.
