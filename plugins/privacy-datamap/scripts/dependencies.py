"""Offline fingerprints for explicitly declared decision evidence, never repository execution."""
import ast
import hashlib
import json
import pathlib
import subprocess
import sys

import typescript

MAX_BYTES = 2_000_000
EXCLUDED = {".git", ".noru", ".fides", "node_modules", ".venv", "venv", "__pycache__"}
METHODS = {"python_ast", "typescript_ast", "json", "text"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_specs(specs, targets):
    errors = []
    if not isinstance(specs, list):
        return ["evidence_dependencies must be a list"]
    seen = set()
    for spec in specs:
        if not isinstance(spec, dict):
            errors.append("each evidence dependency must be an object")
            continue
        identity = spec.get("id")
        if not isinstance(identity, str) or not identity or identity in seen:
            errors.append("evidence dependencies need unique non-empty IDs")
            continue
        seen.add(identity)
        if set(spec) - {"id", "path", "selector", "method", "targets", "fingerprint"}:
            errors.append(f"{identity}: unknown evidence dependency property")
        path = spec.get("path")
        if not isinstance(path, str) or not path or pathlib.PurePosixPath(path).is_absolute() or ".." in pathlib.PurePosixPath(path).parts:
            errors.append(f"{identity}: evidence path must be repository-relative")
        if spec.get("method") not in METHODS:
            errors.append(f"{identity}: method must be python_ast, typescript_ast, json or text")
        if spec.get("selector") and spec.get("method") not in {"python_ast", "typescript_ast"}:
            errors.append(f"{identity}: selectors require python_ast or typescript_ast")
        selected = spec.get("targets")
        if not isinstance(selected, list) or not selected or any(not isinstance(item, str) or item not in targets for item in selected):
            errors.append(f"{identity}: targets must identify existing fields, collections or system:<key>")
        fingerprint = spec.get("fingerprint", "")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            errors.append(f"{identity}: record the reviewed fingerprint from reconciliation")
    return errors


def files(repo):
    result = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True)
    if result.returncode == 0 and result.stdout:
        candidates = result.stdout.decode().split("\0")
    else:
        candidates = (str(path.relative_to(repo)) for path in repo.rglob("*"))
    return sorted({name for name in candidates if name and not (set(pathlib.PurePosixPath(name).parts) & EXCLUDED)})


def source_snapshot(repo):
    rows = []
    for name in files(repo):
        path = (repo / name).resolve()
        if not path.is_relative_to(repo.resolve()) or not path.is_file():
            continue
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                hasher.update(chunk)
        rows.append([name, hasher.hexdigest()])
    return digest(rows)


def observe(repo, spec):
    path = (repo / spec["path"]).resolve()
    if not path.is_relative_to(repo.resolve()):
        raise ValueError("evidence path escapes the repository")
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError("evidence exceeds the two-megabyte inspection limit")
    text = raw.decode("utf-8")
    line = 1
    method = spec["method"]
    if method == "python_ast":
        tree = ast.parse(text)
        if spec.get("selector"):
            for name in spec["selector"].split("."):
                matches = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name]
                if len(matches) != 1:
                    raise ValueError("selected Python symbol is missing or ambiguous")
                tree = matches[0]
            line = tree.lineno
        value = ast.dump(tree, include_attributes=False)
    elif method == "typescript_ast":
        value, line = typescript.fingerprint_tree(text, spec.get("selector", ""))
    elif method == "json":
        value = json.loads(text)
    elif method == "text":
        value = text.replace("\r\n", "\n")
    else:
        raise ValueError("unsupported evidence fingerprint method")
    return {"fingerprint": digest({"method": method, "selector": spec.get("selector", ""), "value": value}),
            "path": spec["path"], "refs": [f"{spec['path']}:{line}"], "method": method,
            "normalizer": f"python_ast:{sys.version_info.major}.{sys.version_info.minor}" if method == "python_ast" else f"{method}:v1",
            "selector": spec.get("selector", ""), "targets": sorted(spec["targets"])}


def reconcile(repo, specs, accepted):
    observations = {}
    changes = []
    tracked = None
    for spec in sorted(specs, key=lambda row: row["id"]):
        identity = spec["id"]
        previous = (accepted or {}).get(identity)
        try:
            current = observe(repo, spec)
            action = "unchanged" if current["fingerprint"] == spec.get("fingerprint") else "investigate"
            if previous and previous.get("normalizer") != current.get("normalizer"):
                action = "normalizer_changed"
            if previous and action == "unchanged" and previous.get("refs") != current["refs"]:
                action = "refresh_evidence"
        except (OSError, ValueError, SyntaxError, UnicodeError) as exc:
            current = {"path": spec["path"], "targets": sorted(spec["targets"]), "error": str(exc)}
            action = "missing_evidence"
            # Only exact content continuity, with one match, permits a mechanical move.
            if not (repo / spec["path"]).exists() and spec.get("fingerprint"):
                tracked = files(repo) if tracked is None else tracked
                matches = []
                for name in tracked:
                    if pathlib.PurePosixPath(name).suffix != pathlib.PurePosixPath(spec["path"]).suffix:
                        continue
                    try:
                        found = observe(repo, {**spec, "path": name})
                        if found["fingerprint"] == spec["fingerprint"]:
                            matches.append(found)
                    except (OSError, ValueError, SyntaxError, UnicodeError):
                        continue
                if len(matches) == 1:
                    current = matches[0]
                    action = "refresh_evidence"
                elif len(matches) > 1:
                    current["candidates"] = [row["path"] for row in matches]
                    action = "identity_ambiguity"
        observations[identity] = current
        if action != "unchanged":
            changes.append({"id": identity, "action": action, "previous": previous or {"fingerprint": spec.get("fingerprint")},
                            "current": current, "targets": spec["targets"],
                            "reason": "Decision evidence changed; investigate significance before changing privacy meaning." if action == "investigate"
                            else "Refresh the citation or resolve missing/ambiguous evidence without guessing.",
                            "resolution_needed": "Inspect the cited code and retain or amend the affected decision; record the reviewed fingerprint."})
    return observations, changes


def main():
    import argparse
    if "--discover" in sys.argv:
        import analysis_cache
        parser = argparse.ArgumentParser()
        parser.add_argument("--discover", action="store_true")
        parser.add_argument("--repo", type=pathlib.Path, required=True)
        args = parser.parse_args()
        sources = analysis_cache.discover(args.repo.resolve())
        print(json.dumps({"sources": sources, "digest": digest(sources)}, sort_keys=True))
        return 0
    parser = argparse.ArgumentParser(description="Inspect evidence without accepting it.")
    parser.add_argument("--repo", type=pathlib.Path, required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--method", choices=sorted(METHODS), required=True)
    parser.add_argument("--selector", default="")
    parser.add_argument("--target", action="append", required=True)
    args = parser.parse_args()
    spec = {"id": args.id, "path": args.path, "method": args.method, "selector": args.selector, "targets": args.target}
    try:
        observation = observe(args.repo.resolve(), spec)
        print(json.dumps({"proposal": {**spec, "fingerprint": observation["fingerprint"]}, "refs": observation["refs"]}, sort_keys=True))
        return 0
    except (OSError, ValueError, SyntaxError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
