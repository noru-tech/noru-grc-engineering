# audit-pack

> TODO: one sentence on what this piece collects locally and what it lands in Noru.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/audit-pack:scan` | no | Deterministic offline collection → `.noru/audit-pack.yml` |
| `/audit-pack:diff` | no | Reads current state, prints the exact plan |
| `/audit-pack:push` | **yes** | Executes the confirmed plan |

## Scopes

Least privilege. Start read-only.

| Capability | Scopes |
|---|---|
| `:scan` and `:diff` | `read:organization`, `read:controls`, `read:evidence` |
| `:push` | the above plus `write:evidence` |

## Artifact

`.noru/audit-pack.yml`, schema at [`contract/audit-pack.schema.json`](../../contract/audit-pack.schema.json).

Commit it — it is the reviewable artifact. Keep `.noru/.cache/` out of git.

## Idempotency

TODO: fill in the table once the push operations are real.

A second `:scan` + `:diff` on unchanged input must produce a plan of all `skip`. If it does
not, that is a bug — `scripts/test_idempotency.py` asserts it.

## Verify

```bash
node    plugins/audit-pack/scripts/collect.mjs --repo=. --output=json
python3 plugins/audit-pack/scripts/validate_manifest.py .noru/audit-pack.yml
node    plugins/audit-pack/scripts/diff.mjs --repo=.
node    plugins/audit-pack/scripts/push.mjs --repo=. --confirm
```
