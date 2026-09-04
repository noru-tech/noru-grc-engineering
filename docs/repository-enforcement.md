# Repository enforcement

`repo-enforcement` is the optional layer that makes the suite's existing review records a GitHub
merge condition. It does not create a Noru claim and it never turns merge approval into permission
to publish to Noru.

The enforced path is:

```text
piece collectors -> committed .noru manifests -> offline aggregate check
                 -> required GitHub check -> CODEOWNERS approval -> merge
                 -> piece diff -> explicit confirmation -> Noru push
```

## Install and adoption

Run `/repo-enforcement:setup` to inspect first and create an exact local-file plan. Setup needs the
real review teams, strict or ratchet cutover, exception lifetime, break-glass team, rollout scope,
and the full commit SHA of the released action. It preserves unrelated CODEOWNERS entries and
protects the policy, workflow, action, and CODEOWNERS paths. Applying that file plan needs a separate
confirmation and does not administer GitHub.

Strict mode accepts no existing failure. Ratchet mode matches only a complete normalized violation
fingerprint under the current policy digest. A candidate baseline is not an approval: every accepted
entry needs a named person, rationale, decision date, and expiry. New, mutated, increased, expired,
resolved, or reintroduced debt fails. Invalid records, tooling failures, credential exposure,
expired exceptions, stale plans, and GitHub/workflow drift cannot be baselined.

Use `/repo-enforcement:status` for the derived worklist. It groups current debt by owning piece and
named person and sorts blockers, stale cleanup, entries due within seven days, and scheduled debt.
`/repo-enforcement:work <fingerprint>` inspects one exact item, routes it to the owning piece, and
prepares the reviewable manifest/lock/baseline change. When remediation makes the old fingerprint
disappear, its baseline entry deliberately becomes stale and must be reviewed and removed in the
same PR. This catches both accidental reintroduction and a detector that merely stopped reporting.
After merge, Noru publication is still shown as separate work through the piece's diff and confirmed
push.

The pull-request workflow runs on every PR with no path filter. It has only `contents: read`, carries
no Noru or GitHub administration credential, passes the current UTC date explicitly, and invokes the
released action at the exact SHA recorded in policy. The action runs every configured piece
independently and emits one JSON report, annotations, and a job summary.

## GitHub administration

After the workflow has completed successfully, `/repo-enforcement:plan` resolves its source
integration, checks every configured team, reads effective rules including organization parents,
and writes an ignored one-hour plan. The plan is bound to the host, organization and repository IDs,
default branch, repository commit, policy digest, integration ID, current ruleset digest and update
time, plugin version, permissions, and exact operation.

`/repo-enforcement:apply` requires explicit confirmation, refetches those bindings, performs only
the planned create/update (never a deletion), and verifies the result. A second unchanged plan is a
skip. `/repo-enforcement:verify` is read-only and detects weakened approvals, removed or rebound
checks, bypass actors, force-push/deletion exposure, missing self-protected CODEOWNERS, and a missing,
unpinned, or changed workflow.

Repository ruleset apply is the supported rollout path in this release. Organization rulesets are
read and verified so inherited policy is visible, and the recommended production design is a
centrally required workflow targeted by a repository property. Creating organization rulesets,
assigning custom properties, and central out-of-repository drift monitoring require organization
administration and remain an explicitly separate rollout phase; this utility refuses to imply that
those writes occurred.

## Boundaries

- Repository validation is whole-repository, deterministic, offline, and uses only commands shipped
  in the released suite. Target repository text is input, never executable configuration.
- GitHub access uses an existing authenticated `gh` session. The plugin never asks for or stores a
  token. Missing permissions stop only the affected GitHub phase.
- A local scheduled monitor is defence in depth, not the authority: somebody able to weaken the
  repository can remove it. Strong drift monitoring belongs in an organization-controlled workflow,
  GitHub App, or scheduled integration outside the protected repository.
- Noru publication remains the owning piece's `diff -> confirmation -> push` operation.
