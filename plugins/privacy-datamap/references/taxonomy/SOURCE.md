# Vendored — do not edit

The three JSON files in this directory are **verbatim copies**. The canonical snapshot, its
provenance, its CC BY 4.0 attribution and the recipe for refreshing it all live in one place:

**[`contract/lib/taxonomy/`](../../../../contract/lib/taxonomy/SOURCE.md)**

Edit there, then run `python3 scripts/check_vendored_lib.py --fix`. CI fails on the drift, and the
next `--fix` overwrites anything changed here.

The copy exists because an installed plugin cannot read a file from outside its own directory. The
duplication is deliberate; the drift check is what makes it safe.

This piece loads all three — `data_categories.json`, `data_uses.json` and `data_subjects.json` —
because a Fides manifest uses all three: categories tag fields, and a privacy declaration names a
`data_use` and its `data_subjects`.

These files are the **offline floor**, not the truth. Requirement 3 says the validator runs with no
install and no network, so the vocabulary has to be on disk. Where the piece can reach Noru,
`getPrivacyTaxonomy` is authoritative and `:scan` reconciles against it — a key this snapshot has
never heard of is a stale snapshot, not an invalid key.
