# Working in this repository

Read [CONTRIBUTING.md](./CONTRIBUTING.md) first. It is short and every rule in it is enforced by
a script. The three that bite most often:

- **Everything here is public.** No private Noru paths, behaviour, analysis or plans — only what a
  customer can already read in the published API and MCP documentation.
- **Stdlib and built-ins only.** No `npm install`, no `pip install`, no network in `:scan` or
  validation. If a change needs a dependency, the change is wrong.
- **Every claim gets a test.** An invariant in a comment, README or PR description ships with the
  check that asserts it.

## Before opening a pull request

Run the verification block in CONTRIBUTING.md, in full, and `git diff --check`. It ends with
`python3 scripts/publish_actions.py --check`, which builds each GitHub Action's Marketplace mirror
into a temporary directory and runs the action from that layout — so a change that works in-tree
but would break the published action fails here, not after the release.

Update `CHANGELOG.md` under `## Unreleased` for anything user-visible.

## Releasing

The full runbook is CONTRIBUTING.md, "Releasing". The short form:

1. `chore: release X.Y.Z` pull request — one version everywhere, a `## X.Y.Z` changelog section.
   `python3 scripts/check_repo.py` fails while any copy disagrees.
2. Tag the merge commit `vX.Y.Z`, push the tag, create the GitHub release for this repository.
3. The `release` workflow verifies the tag and **publishes the three actions to their GitHub
   Marketplace repositories** (`noru-tech/noru-ci-action`, `noru-tech/noru-review-action`,
   `noru-tech/noru-enforce-action`). This is automatic on every tag.

To republish a version — the workflow failed, a mirror was recreated, a secret was rotated — re-run
the `release` workflow by `workflow_dispatch` with the version, or from a checkout of the tag:

```bash
python3 scripts/publish_actions.py publish --version=X.Y.Z --dry-run
```

then without `--dry-run`. It is idempotent: matching trees get no commit, matching tags are left
alone, and a released tag that points elsewhere is a hard failure. Never move a released tag; ship
a new patch version.

The first listing of each mirror on the Marketplace is a one-time manual step (edit the release,
tick "Publish this Action to the GitHub Marketplace"); GitHub exposes no API for it. Everything
after that is automatic.
