#!/usr/bin/env python3
"""Keep the vendored library blocks byte-identical across pieces.

An installed plugin has to be self-contained: it cannot import from a sibling plugin, it cannot read
a file from outside its own directory, and it cannot assume a package was installed. So three things
are vendored into the pieces that need them:

  * contract/lib/yaml_mini.py        -> inlined into plugins/<piece>/scripts/validate_manifest.py
  * plugins/noru/scripts/lib/plan.mjs -> copied to plugins/<piece>/scripts/lib/plan.mjs
  * contract/lib/taxonomy/*.json      -> copied to plugins/<piece>/references/taxonomy/*.json

Duplication is fine as long as it cannot drift silently. This script is what stops it drifting.

The taxonomy arm is driven by each piece's own `validator.vocabulary` declaration rather than by a
list kept here: a piece vendors the vocabulary its validator actually loads and nothing more, so
declaring the file is what opts into the check. A vocabulary file a piece never declared is a file
nothing loads.

Usage:
    python3 scripts/check_vendored_lib.py [--fix] [--output=json] [--quiet]
Exit codes: 0 = in sync, 1 = drift found, 2 = usage / IO error.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY_BEGIN = "# --- BEGIN VENDORED yaml_mini ---"
PY_END = "# --- END VENDORED yaml_mini ---"
PY_PLACEHOLDER = "#VENDORED_YAML_MINI#"


def canonical_python():
    text = (ROOT / "contract" / "lib" / "yaml_mini.py").read_text(encoding="utf-8")
    start = text.index(PY_BEGIN)
    end = text.index(PY_END) + len(PY_END)
    return text[start:end]


def canonical_plan():
    return (ROOT / "plugins" / "noru" / "scripts" / "lib" / "plan.mjs").read_text(encoding="utf-8")


def piece_dirs():
    plugins = ROOT / "plugins"
    if not plugins.is_dir():
        return []
    return sorted(
        p for p in plugins.iterdir() if p.is_dir() and (p / "piece.json").is_file()
    )


def sync_python(path, block, fix):
    text = path.read_text(encoding="utf-8")
    if PY_PLACEHOLDER in text:
        if not fix:
            return f"{path.relative_to(ROOT)}: still contains the {PY_PLACEHOLDER} placeholder"
        path.write_text(text.replace(PY_PLACEHOLDER, block), encoding="utf-8")
        return None
    if PY_BEGIN not in text or PY_END not in text:
        return f"{path.relative_to(ROOT)}: missing the vendored yaml_mini block"
    start = text.index(PY_BEGIN)
    end = text.index(PY_END) + len(PY_END)
    if text[start:end] == block:
        return None
    if not fix:
        return (
            f"{path.relative_to(ROOT)}: vendored yaml_mini block has drifted from "
            "contract/lib/yaml_mini.py"
        )
    path.write_text(text[:start] + block + text[end:], encoding="utf-8")
    return None


def taxonomy_files(piece):
    """Taxonomy files this piece declares it validates against, from its own piece.json.

    Returns (relative path, canonical path) pairs. A piece opts into the check by declaring the
    file in validator.vocabulary; nothing here knows which pieces use the taxonomy.
    """
    decl = json.loads((piece / "piece.json").read_text(encoding="utf-8"))
    out = []
    for rel in decl.get("validator", {}).get("vocabulary", []):
        parts = pathlib.PurePosixPath(rel).parts
        if len(parts) == 3 and parts[0] == "references" and parts[1] == "taxonomy":
            out.append((rel, ROOT / "contract" / "lib" / "taxonomy" / parts[2]))
    return out


def sync_taxonomy(path, canonical, fix):
    if not canonical.is_file():
        return (
            f"{path.relative_to(ROOT)}: declared in piece.json, but there is no canonical "
            f"{canonical.relative_to(ROOT)} to copy from"
        )
    want = canonical.read_bytes()
    if path.is_file() and path.read_bytes() == want:
        return None
    if not fix:
        if not path.is_file():
            return f"{path.relative_to(ROOT)}: missing (copy {canonical.relative_to(ROOT)})"
        return (
            f"{path.relative_to(ROOT)}: has drifted from {canonical.relative_to(ROOT)} — edit the "
            "canonical snapshot, never a vendored copy"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(want)
    return None


def sync_plan(path, canonical, fix):
    if path.is_file() and path.read_text(encoding="utf-8") == canonical:
        return None
    if not fix:
        if not path.is_file():
            return f"{path.relative_to(ROOT)}: missing (copy plugins/noru/scripts/lib/plan.mjs)"
        return (
            f"{path.relative_to(ROOT)}: has drifted from plugins/noru/scripts/lib/plan.mjs"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical, encoding="utf-8")
    return None


def main(argv):
    fix = False
    output_json = False
    quiet = False
    for arg in argv:
        if arg == "--fix":
            fix = True
        elif arg == "--output=json":
            output_json = True
        elif arg == "--output=text":
            output_json = False
        elif arg == "--quiet":
            quiet = True
        elif arg in ("-h", "--help"):
            sys.stdout.write(__doc__.split("Usage:")[1].strip() + "\n")
            return 0
        else:
            sys.stderr.write(f"error: unknown option '{arg}'\n")
            return 2

    try:
        py_block = canonical_python()
        plan_text = canonical_plan()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"error: could not read a canonical library ({exc})\n")
        return 2

    problems = []
    checked = 0
    for piece in piece_dirs():
        validator = piece / "scripts" / "validate_manifest.py"
        if validator.is_file():
            checked += 1
            problem = sync_python(validator, py_block, fix)
            if problem:
                problems.append(problem)
        plan = piece / "scripts" / "lib" / "plan.mjs"
        if piece.name != "noru":
            checked += 1
            problem = sync_plan(plan, plan_text, fix)
            if problem:
                problems.append(problem)
        try:
            declared = taxonomy_files(piece)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"plugins/{piece.name}/piece.json: could not be read ({exc})")
            declared = []
        for rel, canonical in declared:
            checked += 1
            problem = sync_taxonomy(piece / rel, canonical, fix)
            if problem:
                problems.append(problem)

    ok = not problems
    if output_json:
        sys.stdout.write(
            json.dumps(
                {"ok": ok, "checked": checked, "fixed": fix, "problems": problems},
                indent=None if quiet else 2,
                sort_keys=True,
            )
            + "\n"
        )
    elif not quiet:
        for problem in problems:
            print(f"  DRIFT {problem}")
        print("OK: vendored libraries are in sync." if ok else f"\nFAILED: {len(problems)} drift(s).")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
