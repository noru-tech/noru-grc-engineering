# `noru-review` action

Routes a pull-request diff through the relevant GRC pieces and runs only their local `scan`,
`validate`, `expiry`, and `policy` checks. It has no input that enables `diff` or `push`, removes
`NORU_API_KEY` from child processes, and writes one consolidated JSON report and job summary.

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0 }
- uses: actions/setup-node@v4
  with: { node-version: "20" }
- uses: noru-tech/noru-grc-engineering/.github/actions/noru-review@v0.5.0
  with:
    base-ref: ${{ github.event.pull_request.base.sha }}
    mode: warn
```

Use `pieces: privacy-datamap,ai-inventory` to replace automatic routing with an explicit adopted
set. Change `mode` to `gate` only after the warning report is understood. Pull requests—including
forks—need only `contents: read`; Noru credentials must never be supplied to this action.
