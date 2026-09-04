#!/usr/bin/env python3
"""Generate the trusted whole-repository enforcement registry from piece declarations."""

import json
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "actions" / "enforce" / "registry.json"


def build_registry():
    pieces = []
    for path in sorted((ROOT / "plugins").glob("*/piece.json")):
        declaration = json.loads(path.read_text(encoding="utf-8"))
        ci = declaration["ci"]
        pieces.append(
            {
                "name": declaration["piece"],
                "artifact": declaration["artifact"],
                "validate": ci["validate"],
                "drift_check": ci["drift_check"],
                "watch_paths": ci["watch_paths"],
                "generated_outputs": [
                    output["path"] for output in declaration.get("outputs", [])
                ],
            }
        )
    return {"version": 1, "generated_from": "plugins/*/piece.json", "pieces": pieces}


def rendered():
    return json.dumps(build_registry(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main(argv):
    check = False
    quiet = False
    for arg in argv:
        if arg == "--check":
            check = True
        elif arg == "--quiet":
            quiet = True
        elif arg in {"-h", "--help"}:
            print("usage: generate_enforcement_registry.py [--check] [--quiet]")
            return 0
        else:
            print(f"error: unknown option '{arg}'", file=sys.stderr)
            return 2

    expected = rendered()
    if check:
        actual = OUTPUT.read_text(encoding="utf-8") if OUTPUT.is_file() else ""
        if actual != expected:
            print(
                "actions/enforce/registry.json has drifted from plugins/*/piece.json; "
                "run python3 scripts/generate_enforcement_registry.py",
                file=sys.stderr,
            )
            return 1
        if not quiet:
            print(f"OK: enforcement registry contains {len(build_registry()['pieces'])} pieces.")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(expected, encoding="utf-8")
    if not quiet:
        print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
