# iac-scan

> TODO: one sentence on what this piece collects locally and what it lands in Noru.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/iac-scan:scan` | no | Deterministic offline collection → `.noru/iac-scan.yml` |
| `/iac-scan:diff` | no | Reads current state, prints the exact plan |
| `/iac-scan:push` | **yes** | Executes the confirmed plan |

## Scopes

Least privilege. Start read-only.

| Capability | Scopes |
|---|---|
| `:scan` and `:diff` | `read:organization`, `read:controls`, `read:evidence` |
| `:push` | the above plus `write:evidence` |

## Artifact

`.noru/iac-scan.yml`, schema at [`contract/iac-scan.schema.json`](../../contract/iac-scan.schema.json).

Commit it — it is the reviewable artifact. Keep `.noru/.cache/` out of git.

## Idempotency

TODO: fill in the table once the push operations are real.

A second `:scan` + `:diff` on unchanged input must produce a plan of all `skip`. If it does
not, that is a bug — `scripts/test_idempotency.py` asserts it.

## Verify

```bash
node    plugins/iac-scan/scripts/collect.mjs --repo=. --output=json
python3 plugins/iac-scan/scripts/validate_manifest.py .noru/iac-scan.yml
node    plugins/iac-scan/scripts/diff.mjs --repo=.
node    plugins/iac-scan/scripts/push.mjs --repo=. --confirm
```
