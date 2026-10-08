# Portable privacy CI

The privacy-datamap plugin includes two command-line scripts for any CI runner with Git,
Node 18+ and Python 3.9+. No Python or Node dependencies need installing. Use the same
Python minor version when accepting a baseline and checking it: Python AST fingerprints
are version-specific. Install a pinned toolkit release outside the repository being scanned.

```sh
python3 "$TOOLKIT/plugins/privacy-datamap/scripts/check_privacy.py" --repo="$REPOSITORY"
```

The offline gate requires tracked `.noru/privacy-datamap.yml`,
`.noru/privacy-datamap.lock.json` and `.fides/datamap.yml`. It checks taxonomy and unresolved
review, expiry, schema and registered processing evidence, discovery, accepted baseline
freshness and export consistency. It writes diagnostic cache only. Human review, sealing and
export generation happen outside CI. Unregistered processing evidence remains outside the
baseline's monitoring scope. Exit codes: 0 passed, 1 blocked, 2 tooling/usage error.

This strict privacy-specific gate complements the existing multi-piece `scripts/ci_check.py`;
it does not replace that runner's policy-baseline or reporting options.

## REST publication

After the gate succeeds, use the separate publisher in a trusted publication job:

```sh
python3 "$TOOLKIT/plugins/privacy-datamap/scripts/publish_datamap.py" \
  "$REPOSITORY/.fides/datamap.yml" --publish \
  --branch="$SOURCE_BRANCH" --commit-sha="$SOURCE_COMMIT"
```

Set `NORU_API_BASE_URL`, `NORU_API_KEY` and `NORU_SOURCE_SLUG` in the runner environment.
`NORU_SOURCE_NAME` is optional. Branch and commit can alternatively be supplied through
`NORU_SOURCE_BRANCH` and `NORU_SOURCE_COMMIT_SHA`; command-line arguments take precedence.
There is no CI-provider autodetection. Map your provider's variables to these inputs in its
pipeline configuration. The publisher never reads GitLab or GitHub environment variables.

Without `--publish`, the publisher performs local document-shape checks only; it does not
replace the privacy gate. It accepts JSON and the collector's generated block-YAML format.
Duplicate keys, empty maps, nonfinite numbers and unsupported YAML features are rejected.
For other YAML formats, produce the package's export first or supply JSON. No dependency
installation is required, and parsing is identical whether PyYAML is installed or absent.

The publisher uses the documented `POST /v1/privacy/datamaps` endpoint and requires
`write:datamaps`. The API key selects the organization; the stable source slug selects its map.
Publish the complete snapshot: omissions can archive prior records. Identical maps are no-ops.
HTTPS is required, redirects are refused, and uncertain writes are not automatically retried.
Warnings cause a nonzero exit after the write; inspect Noru before retrying. Response bodies
and credentials are not printed. Exit codes: 0 succeeded, 1 validation/publication failure,
2 command-line usage error. Serialize publication per source and prevent older commits from
publishing after newer ones using your CI provider's controls.

See the [public Noru API documentation](https://api.noru.tech/llms.txt).
