# Taxonomy snapshot provenance

The **canonical** vendored snapshot of the Fideslang default taxonomy. Every piece that validates
against Fideslang keys copies the files it needs out of this directory;
`scripts/check_vendored_lib.py` fails CI if a copy drifts, and `--fix` re-copies it.

**Never edit a vendored copy in a piece.** Edit here, then run:

```bash
python3 scripts/check_vendored_lib.py --fix
```

An installed plugin cannot read a file from outside its own directory, which is why the snapshot is
copied rather than shared. The duplication is deliberate; the drift check is what makes it safe.
This is the same arrangement `contract/lib/yaml_mini.py` and `plugins/noru/scripts/lib/plan.mjs`
use, for the same reason.

## Why this is bundled at all, when requirement 9 forbids vendoring a catalogue

Because it is a **vocabulary**, not a catalogue: it says what a value may be *called*, never what
anyone must *do*. Requirement 3 requires the validator to run with no install and no network, and a
vocabulary fetched at validation time is a validator that fails on a plane. The full argument, and
the test to apply to the next file someone wants to bundle, is in
[`contract/README.md`](../../README.md) under "A vocabulary is not a catalogue".

Using the published Fideslang keys rather than an invented vocabulary is the point: an AI inventory
and a privacy data map that disagree about what `user.contact.email` means are two registers, not
one.

Where Noru publishes the same vocabulary — `getPrivacyTaxonomy` does, for exactly this taxonomy —
this snapshot is the offline floor and Noru is the truth. A piece that can reach Noru reconciles
against it and reports a difference rather than silently preferring what is on disk.

## License and attribution

The Fideslang taxonomy is © Ethyca, Inc. and licensed under **Creative Commons Attribution 4.0
International (CC BY 4.0)** — https://creativecommons.org/licenses/by/4.0/.

These JSON files are a **modified** redistribution: the upstream Python definitions were reformatted
to JSON and reduced to the `fides_key` / `name` / `description` fields. No other change was made to
the meaning of any entry. See [`NOTICE`](../../../NOTICE) at the repository root for the full
attribution statement.

**This repository's own code is MIT-licensed; this data directory remains CC BY 4.0.**

## Source

- Repository: https://github.com/ethyca/fideslang
- Files: `src/fideslang/default_taxonomy/{data_categories,data_uses,data_subjects}.py` (branch `main`)
- Taxonomy directory last modified at commit: `21eb1746904d` (2024-11-04)
- Latest published release at snapshot time: `3.1.3`

| File | Entries | Used by |
| --- | --- | --- |
| `data_categories.json` | 85 | `ai-inventory` |
| `data_uses.json` | 56 | — |
| `data_subjects.json` | 15 | — |

Each entry is `{ "fides_key": "...", "name": "...", "description": "..." }`, sorted by `fides_key`.
The parent of any key is the dotted prefix, so the parent of `user.contact.email` is `user.contact`.

## How to refresh

The three source files use a `default_*_factory(fides_key=..., name=..., description=...,
parent_key=...)` call pattern. Re-generate without executing the source — no `fideslang` install,
no import — by parsing it with the standard-library `ast` module:

```bash
base="https://raw.githubusercontent.com/ethyca/fideslang/main/src/fideslang/default_taxonomy"
tmp="$(mktemp -d)"
for f in data_categories data_uses data_subjects; do curl -fsSL "$base/$f.py" -o "$tmp/$f.py"; done

python3 - "$tmp" "$(dirname "$0")" <<'PY'
import ast, json, pathlib, sys
src, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])

def extract(path):
    rows = {}
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            kw = {k.arg: k.value.value for k in node.keywords
                  if k.arg in ("fides_key", "name", "description")
                  and isinstance(k.value, ast.Constant)}
            if "fides_key" in kw:
                rows[kw["fides_key"]] = {"fides_key": kw["fides_key"],
                                         "name": kw.get("name", ""),
                                         "description": kw.get("description", "")}
    return sorted(rows.values(), key=lambda r: r["fides_key"])

for name in ("data_categories", "data_uses", "data_subjects"):
    rows = extract(src / f"{name}.py")
    (out / f"{name}.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
    print(f"{name}: {len(rows)} entries")
PY

python3 scripts/check_vendored_lib.py --fix   # push the new snapshot out to every piece
```

After refreshing, update the commit SHA, the release and the entry counts above — and read the diff
before committing it. A category disappearing upstream invalidates every manifest that used it, and
the validator will report those as unknown keys with no explanation of why they were valid last
week.
