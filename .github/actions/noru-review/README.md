# `noru-review` action

Routes a pull-request diff through the relevant GRC pieces and runs only their local `scan`,
`validate`, `expiry`, and `policy` checks. It has no input that enables `diff` or `push`, removes
`NORU_API_KEY` from child processes, and writes one consolidated JSON report and job summary.

```yaml
- uses: actions/checkout@v5
  with: { fetch-depth: 0 }
- uses: actions/setup-node@v5
  with: { node-version: "20" }
- uses: noru-tech/noru-review-action@v0
  with:
    base-ref: ${{ github.event.pull_request.base.sha }}
    mode: warn
```

`@v0` follows the newest 0.x release, so a copied example never goes stale. To take changes only
when you choose to, pin a release tag from
[the releases page](https://github.com/noru-tech/noru-grc-engineering/releases) or a full commit
SHA instead. `noru-tech/noru-grc-engineering/.github/actions/noru-review@<tag>` — the path inside
this repository — is the same code at the same tag; the Marketplace repository is generated from it.

Use `pieces: privacy-datamap,ai-inventory` to replace automatic routing with an explicit adopted
set. Change `mode` to `gate` only after the warning report is understood. Pull requests—including
forks—need only `contents: read`; Noru credentials must never be supplied to this action.
