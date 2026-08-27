# Vendored — do not edit

`data_categories.json` in this directory is a **verbatim copy**. The canonical snapshot, its
provenance, its CC BY 4.0 attribution and the recipe for refreshing it all live in one place:

**[`contract/lib/taxonomy/`](../../../../contract/lib/taxonomy/SOURCE.md)**

Edit there, then run `python3 scripts/check_vendored_lib.py --fix`. CI fails on the drift, and the
next `--fix` overwrites anything changed here.

The copy exists because an installed plugin cannot read a file from outside its own directory. The
duplication is deliberate; the drift check is what makes it safe.

`ai-inventory` uses `data_categories.json` only. `data_uses.json` and `data_subjects.json` are in
the canonical directory and are not copied here, because a piece vendors the vocabulary its
validator actually loads and nothing more.
