# `noru-ci` action

Runs one last-mile piece headless: `scan → validate → expiry`, and optionally `diff → push`.

The default mode needs **no network and no credential**, so it works on a pull request from a fork.
Full documentation, the exit-code table and the non-GitHub recipes are in
[`docs/ci-mode.md`](../../../docs/ci-mode.md).

```yaml
- uses: actions/checkout@v4
- uses: actions/setup-node@v4
  with:
    node-version: "20"
- uses: noru-tech/noru-grc-engineering/.github/actions/noru-ci@v0.2.0
  with:
    piece: ai-inventory
    mode: warn      # switch to gate once the report is quiet
```

Two things fail the build, and both are computed locally:

- **drift** — the collector's derived facts no longer match the digest committed in
  `.noru/<piece>.yml`, so the record does not describe the code (exit `3`)
- **an expired interpretation** — a claim whose expiry has passed, or that is outside the review
  cadence the pipeline declared (exit `4`)

It installs nothing. `node` and `python3` must already be on the runner; the action fails with a
clear message if either is missing, and prints which YAML loader the runner will use.

**Credentials.** Never pass a key as a `with:` input. Put it in the job or step `env:` from a
secret — `NORU_API_KEY: ${{ secrets.NORU_API_KEY }}` — only in a job that has secrets, and only for
the opt-in `steps: all` push. The action never reads the value; it checks only whether the variable
is present, and the piece's own push entrypoint reads it at the point of use.
