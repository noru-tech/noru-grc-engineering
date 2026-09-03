# Developer onboarding

This is the shortest supported path from a repository to a reviewed Noru change. The pull-request
path is deliberately read-only; publishing is a separate, protected action.

## 1. Install the hub and only the pieces you need

Install `noru` first, then ask it to review the branch. You do not need to choose every piece up
front. Client-specific install and MCP setup are documented for
[Codex](./clients/codex.md), [Claude Code](./clients/claude-code.md),
[Cursor](./clients/cursor.md), and [other MCP clients](./clients/generic-mcp.md).

```text
/noru:connect
/noru:doctor
/noru:review
```

`connect` must show the organization you intend to update. `doctor` checks the local toolchain,
cache hygiene, and possible competing privacy data-map publishers. `review` compares the current
branch with `origin/main`, explains why each piece was selected or skipped, and writes nothing to
Noru. Use `--base-ref=<ref>` if the repository has a different default branch.

## 2. Create the reviewable artifact

Run the selected piece's scan and review the generated file:

```text
/<piece>:scan
```

Commit `.noru/<piece>.yml`; never commit `.noru/.cache/`. For `privacy-datamap`, also review and
commit `.fides/datamap.yml`: it is the Fideslang export, while `.noru/privacy-datamap.yml` carries
the owners, citations, and decisions used by the gate. Resolve every `needs_review` item with a
named owner and rationale rather than accepting classifier output as a compliance conclusion.

## 3. Add a credential-free pull-request check

Start in warning mode and pin a release tag. The full checkout is required when comparing findings
with the merge base.

```yaml
name: grc
on: [pull_request]

permissions:
  contents: read

jobs:
  privacy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - uses: noru-tech/noru-grc-engineering/.github/actions/noru-ci@v0.4.1
        with:
          piece: privacy-datamap
          mode: warn
          base-ref: origin/main
          gate-on-new: true
```

This path runs local collection, validation, expiry, and policy checks. It needs no Noru secret and
does not push. Change `mode` to `gate` after the report is understood and the committed manifest is
current. Repeat the action for each piece the repository has adopted; `/noru:review` helps choose
them but is not itself a replacement for the deterministic per-piece CI checks.

## 4. Review the exact Noru change

In an authenticated MCP client, run:

```text
/<piece>:diff
```

The plan is short-lived and bound to the manifest bytes, repository commit, plugin version, MCP
endpoint, required scopes, and exact Noru organization. A changed branch, manifest, connection, or
expired plan requires a new diff. Read the create/update/close counts and the target organization
before approving it.

## 5. Publish through one protected path

After approval, run `/<piece>:push`. MCP-backed pieces currently require an authenticated MCP host
to execute the reviewed calls; emitting a call plan in headless CI does not execute those calls.
`evidence-push` is the exception because its file upload uses REST and reads `NORU_API_KEY` from the
job environment at the point of use.

If publishing is automated, put it on a protected branch or deployment environment with human
approval—never `pull_request_target`—and make that workflow the only writer for the source. Keep PR
checks read-only. `/noru:doctor` warns when tracked code or workflows indicate more than one privacy
data-map publisher, but the warning is a signal to review, not proof that both paths are active.

See [CI mode](./ci-mode.md) for exit codes, GitLab and shell examples, policy baselines, and staged
adoption.
