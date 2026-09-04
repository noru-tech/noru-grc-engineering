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
- uses: noru-tech/noru-grc-engineering/.github/actions/noru-ci@v0.7.0
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

## Reading results when the gate fails

A composite action that exits non-zero does not propagate its declared `outputs`. GitHub sets
`steps.<id>.outcome` to `failure`, but `steps.<id>.outputs.status`, `.exit-code`, `.drift` and
`.expired` all come back **empty** — including when the caller sets `continue-on-error: true`.
That is runner behaviour, not something this action can work around while still failing the job.

So outputs are reliable on a passing or warning run, and absent on exactly the run you most want
to inspect. Two ways to read a failing run:

- **`mode: warn`** — identical checks, findings labelled `would-fail`, exit 0, outputs populated.
- **The report file**, which is written *before* the action exits and survives the failure. Its
  default path is `${{ runner.temp }}/noru-ci-<piece>.json` and it carries `exit_code`,
  `status` and `counts`. That default is derived from the piece name alone, so **two
  invocations for the same piece in one job overwrite each other** — pass `report-path`
  explicitly when a job calls this action more than once:

  ```yaml
  - id: gate
    continue-on-error: true
    uses: noru-tech/noru-grc-engineering/.github/actions/noru-ci@v0.7.0
    with: { piece: ai-inventory, repo: . }
  - if: steps.gate.outcome == 'failure'
    run: |
      python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["status"], d["counts"])' \
        "${{ runner.temp }}/noru-ci-ai-inventory.json"
  ```

This repository's own `ci-mode` job asserts a gated run that way, because the earlier version
asserted on outputs and went red for this exact reason.
