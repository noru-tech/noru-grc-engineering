"""Validate exact, fingerprinted source-role declarations; never execute source files."""
import json
import pathlib
import re
import sys


def errors(rows):
    if not isinstance(rows, list):
        return ["migration_support must be an array"]
    issues, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "rationale"}:
            issues.append("migration_support entries require path, sha256 and rationale only")
            continue
        path = row["path"]
        if (not isinstance(path, str) or not path or "\\" in path
                or pathlib.PurePosixPath(path).is_absolute()
                or any(part in ("", ".", "..") for part in path.split("/"))
                or any(c in path for c in "*?[]{}")):
            issues.append("migration_support paths must be exact repository-relative files")
            continue
        if path in seen:
            issues.append("duplicate migration_support path: " + path)
        seen.add(path)
        if not isinstance(row["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            issues.append("migration_support requires a SHA-256 fingerprint: " + path)
        if not isinstance(row["rationale"], str) or not row["rationale"].strip():
            issues.append("migration_support requires an evidence-based rationale: " + path)
    return issues


if __name__ == "__main__":
    from validate_manifest import load_yaml
    try:
        path = pathlib.Path(sys.argv[1]) / ".noru/privacy-datamap.yml"
        document = load_yaml(path.read_text())[0] if path.is_file() else {}
        rows = (document or {}).get("migration_support", [])
        issues = errors(rows)
        if issues:
            raise ValueError("; ".join(issues))
        print(json.dumps(rows))
    except (ValueError, OSError, TypeError, AttributeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
