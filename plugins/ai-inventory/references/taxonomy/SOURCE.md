# Taxonomy snapshot provenance

`data_categories.json` is a **vendored snapshot** of the Fideslang default data-category taxonomy.
The `ai-inventory` validator checks `data_categories` values against this file and never imports or
installs the `fideslang` package, and never reaches the network.

Using the Fideslang keys rather than an invented vocabulary is deliberate: an AI inventory and a
privacy data map that disagree about what "user.contact.email" means are two registers, not one.

## License and attribution

The Fideslang taxonomy is © Ethyca, Inc. and licensed under **Creative Commons Attribution 4.0
International (CC BY 4.0)** — https://creativecommons.org/licenses/by/4.0/.

This JSON file is a **modified** redistribution: the upstream Python definitions were reformatted to
JSON and reduced to the `fides_key` / `name` / `description` fields. See [`NOTICE`](../../../../NOTICE)
at the repository root for the full attribution statement.

**This repository's own code is MIT-licensed; this data directory remains CC BY 4.0.**

## Source

- Repository: https://github.com/ethyca/fideslang
- File: `src/fideslang/default_taxonomy/data_categories.py` (branch `main`)
- Taxonomy directory last modified at commit: `21eb1746904d` (2024-11-04)
- Latest published release at snapshot time: `3.1.3`
- Entries: 85

Each entry is `{ "fides_key": "...", "name": "...", "description": "..." }`. The parent of any key is
the dotted prefix, so the parent of `user.contact.email` is `user.contact`.

## How to refresh

Re-generate without executing the source (no `fideslang` install needed) by parsing it with the
standard-library `ast` module:

```bash
url="https://raw.githubusercontent.com/ethyca/fideslang/main/src/fideslang/default_taxonomy/data_categories.py"
tmp="$(mktemp -d)"
curl -fsSL "$url" -o "$tmp/data_categories.py"

python3 - "$tmp/data_categories.py" "$(dirname "$0")/data_categories.json" <<'PY'
import ast, json, pathlib, sys
src, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
rows = {}
for n in ast.walk(ast.parse(src.read_text())):
    if isinstance(n, ast.Call):
        kw = {k.arg: k.value.value for k in n.keywords
              if k.arg in ("fides_key", "name", "description") and isinstance(k.value, ast.Constant)}
        if "fides_key" in kw:
            rows[kw["fides_key"]] = {"fides_key": kw["fides_key"],
                                     "name": kw.get("name", ""),
                                     "description": kw.get("description", "")}
out.write_text(json.dumps(sorted(rows.values(), key=lambda r: r["fides_key"]), indent=2, ensure_ascii=False) + "\n")
PY
```

After refreshing, update the commit SHA, release and entry count above.
