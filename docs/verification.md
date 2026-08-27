# Verification

What has actually been verified, and what has not. The second list is the important one.

## Verified, and re-verified on every build

Run all of it with no dependencies and no network:

```bash
python3 scripts/check_repo.py          # marketplaces, manifests, schema/vocabulary sync, secrets
python3 scripts/check_vendored_lib.py  # vendored blocks are byte-identical across pieces
python3 scripts/test_validators.py     # schema fixtures + validator unit tests
python3 scripts/test_idempotency.py    # a second push is a no-op, end to end
python3 scripts/contract_test.py       # every plugin satisfies requirements 1-9
```

| Property | How it is proven |
|---|---|
| Collectors are deterministic | `contract_test.py` runs each collector twice over two copies of `tests/fixture-repo/` and diffs the derived output byte for byte |
| Collectors are offline | the collector source is scanned for every socket-opening API; a match fails the build |
| Validators are stdlib-only | every `import` in a validator is checked against an allowed standard-library set |
| Exit codes are `0`/`1`/`2` | each validator is executed with no argument, a missing file, an unknown option, a valid fixture and each invalid fixture |
| Invalid manifests produce a *useful* message | each invalid fixture declares the substring its output must contain, not just a non-zero exit |
| Unattributed claims are an error | a fixture with the interpretation block stripped must exit `1`, and must not produce only warnings |
| `:push` refuses without `--confirm` | executed: exit `2` |
| `:push` refuses a stale plan | executed: a plan bound to different manifest bytes exits `1`, even with `--confirm` |
| **A second push is a no-op** | `test_idempotency.py` drives scan → validate → diff → push, builds the org snapshot that would exist if every planned write had landed, and asserts the next diff is all `skip` and the next push makes no calls |
| Asset metadata key order does not break idempotency | the snapshot deliberately reverses the key order, because nothing guarantees a JSON object comes back in the order it was sent |
| No catalogue is vendored | every plugin file is scanned for catalogue-shaped evidence-item and control ids; fixtures may only use the reserved `E-ZZ-*` / `zz-*` namespaces |
| No credential leaks into the repository | the tree is scanned for credential-shaped strings |
| A scaffolded piece satisfies the contract | CI scaffolds one and runs the contract test against it |

The contract test has also been checked in the other direction — it was confirmed to **fail** when a
collector is made non-deterministic, when a hardcoded evidence list is added to a plugin, and when
the `--confirm` gate is removed. A test that has never failed is not yet a test.

## Not verified — needs a live Noru organization

**None of the following has been run.** Every one of them needs credentials against a real org, and
each is a place where a fixture can be right and reality wrong.

1. **Run `ai-inventory` against a large real codebase.** Run `:scan` and hand-check the output
   against the AI systems known to exist in it. The collector's recall against a real polyglot
   codebase is unproven; it will certainly miss a provider reached through a hand-rolled HTTP client.

2. **`:diff` against a dev org with a scoped key.** Confirm the delta is correct and non-empty, and
   that nothing is written. In particular confirm the state-snapshot shape the commands document
   matches what `getOrganizationAssets`, `getOrganizationVendors`, `getOrganizationEvidence` and
   `getOrganizationControls` actually return.

3. **`:push`, then re-scan and re-push unchanged.** The second run must be a no-op *against the real
   API*. Assert it with `getOrganizationAssets` counts and `updatedAt`. `test_idempotency.py` proves
   the client-side logic; it cannot prove the API behaves the way its documentation describes.
   Specifically unproven: that MCP `createAsset` really upserts on `(source, externalId)` rather
   than creating a second record, as the published upsert behaviour says it does.

4. **The landing shape.** Confirm assets, vendors and evidence appear in the app as intended, that
   evidence links to the `iso_42001` / `eu_ai_act` controls, and that provenance shows repo and
   commit. If the shape is wrong, fixing it after v0.1 is a breaking change to a published contract.

5. **`evidence-push` against a real organization's queue.** Confirm `:scan` lists genuinely unmet
   expectations drawn from `getEvidenceItems` / `getControlContext` for a sample of controls; upload
   one artifact; confirm it links to the right control and satisfies the catalogue item; then re-run
   and confirm no duplicate. The filename-to-title matcher has never met a real catalogue.

6. **The `client_probe` fallback under real conditions.** Both pieces probe for a content marker in
   an evidence description, because no idempotency key is documented for evidence. Confirm
   `getOrganizationEvidence`'s `search` filter actually matches on description text at the volumes a
   real org has, and that the marker survives a round trip through the app unchanged.

Until these are done, treat the pieces as reviewed and internally consistent, not as field-tested.

## Known gaps, stated rather than discovered later

- **No idempotency key is documented for evidence.** Noru's published API documentation documents
  upsert behaviour for assets and security findings; neither `createEvidence` nor
  `POST /v1/evidence/upload` documents a key, so both pieces fall back to a client probe. Edit an
  evidence description in the Noru UI and the probe stops matching; a re-run uploads again. Recorded
  in each `piece.json` with what a documented key would let the piece drop.
- **Neither piece is `mode: single_call`.** Both fan out several individually-keyed writes because
  the published API offers no single ingest operation for these artifacts. Both declare
  `collapses_to`.
- **The MCP `push` does not perform the writes.** It emits the confirmed call list for the client to
  execute, because a script cannot speak MCP without handling a credential. The gate is enforced in
  the script; the execution is the agent's, and an agent that improvises a call outside the list has
  stepped outside the reviewed plan. That is a real residual risk, not a solved problem.
- **Framework identifiers come back as ids, not display names.** Pieces read and store the ids, and
  never try to reconstruct a display name from them.
