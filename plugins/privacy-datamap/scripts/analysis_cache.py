"""Proposal-local evidence reconciliation and independent broad discovery (stdlib only)."""
import ast
import copy
import json
import pathlib
import re

import dependencies

DOC_SUFFIXES = {".md", ".rst", ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg"}


def method_for(path):
    suffix = pathlib.PurePosixPath(path).suffix.lower()
    if suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}:
        return "typescript_ast"
    return "python_ast" if suffix == ".py" else "json" if suffix == ".json" else "text"


def discover(repo):
    """Inventory code/configuration beyond registered evidence; never infer privacy meaning."""
    sources = {}
    for path in dependencies.files(repo):
        full = repo / path
        if not full.is_file() or pathlib.PurePosixPath(path).suffix.lower() in DOC_SUFFIXES:
            continue
        spec = {"path": path, "method": method_for(path), "targets": ["discovery"]}
        try:
            observed = dependencies.observe(repo, spec)
            entry = {"fingerprint": observed["fingerprint"], "method": observed["method"]}
            text = full.read_text(encoding="utf-8")
            if pathlib.PurePosixPath(path).suffix.lower() == ".sql" and not any(marker in text for marker in ("$", "\\", "`", "[", "/*!", "/*+")):
                tokens = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*[\s\S]*?\*/|[A-Za-z_]\w*|\d+(?:\.\d+)?|[^\s]", text)
                entry = {"fingerprint": dependencies.digest([token for token in tokens if not token.startswith(("--", "/*"))]), "method": "sql_tokens"}
            elif spec["method"] == "typescript_ast":
                parser = dependencies.typescript.Parser(text)
                tree = parser.parse()
                declarations = {key: dependencies.digest(matches[0][0]) for key, matches in parser.declarations.items() if len(matches) == 1}
                named = {id(matches[0][0]) for matches in parser.declarations.values() if len(matches) == 1}
                module = [node for node in tree[1] if id(node) not in named and not (node[0] == "variables" and all(id(n) in named for n in node[1]))]
                entry.update(declarations=declarations, module_fingerprint=dependencies.digest(module))
            elif spec["method"] == "python_ast":
                tree = ast.parse(text)
                declared = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                entry.update(declarations={name: dependencies.digest(ast.dump(node, include_attributes=False)) for name, node in declared.items()},
                             module_fingerprint=dependencies.digest([ast.dump(n, include_attributes=False) for n in tree.body if n not in declared.values()]))
            sources[path] = entry
        except (OSError, ValueError, SyntaxError, UnicodeError) as error:
            # Unsupported syntax still participates in discovery conservatively, never as AST.
            try:
                fingerprint = dependencies.hashlib.sha256(full.read_bytes()).hexdigest()
            except OSError:
                fingerprint = None
            sources[path] = {"fingerprint": fingerprint, "method": "opaque", "coverage_gap": str(error)}
    return sources


def discovery_changes(previous, current, specs=()):
    changes = []
    removed, added = set(previous) - set(current), set(current) - set(previous)
    moved = set()
    for old in removed:
        matches = [new for new in added if current[new] == previous[old]]
        originals = [name for name in removed if previous[name] == previous[old]]
        if len(matches) == len(originals) == 1:
            moved.update([old, matches[0]])
    for path in sorted(set(previous) | set(current)):
        before, after = previous.get(path), current.get(path)
        if before == after or path in moved:
            continue
        covered = [spec for spec in specs if spec.get("path") == path]
        if before and after and before.get("method") == after.get("method"):
            if any(not spec.get("selector") and spec.get("method") == after["method"] for spec in covered) and set(before.get("declarations", {})) == set(after.get("declarations", {})):
                continue
            if before.get("module_fingerprint") and before.get("module_fingerprint") == after.get("module_fingerprint"):
                old, new = before.get("declarations", {}), after.get("declarations", {})
                changed = {key for key in set(old) | set(new) if old.get(key) != new.get(key)}
                if changed and changed <= {spec.get("selector") for spec in covered}:
                    continue
        changes.append({"path": path, "action": "added" if before is None else "removed" if after is None else "changed",
                        "previous": before, "current": after,
                        "question": "Does this code/configuration introduce or change a store, client or processing path?"})
    return changes


def rows(document):
    for row in document.get("proposals", []):
        yield row["entity_id"], row
    for name, row in document.get("reasoning_groups", {}).items():
        yield "group:" + name, row
    for system in document.get("system_proposals", []):
        key = system["entity_id"]
        yield "system:" + key, system
        for activity in system.get("processing_activities", []):
            yield "activity:" + key + "/" + activity.get("activity_id", ""), activity
    for row in document.get("dependency_proposals", []):
        yield "investigation:" + row["entity_id"], row
    mapping = document.get("relationship_proposal")
    if isinstance(mapping, dict) and isinstance(mapping.get("graph"), dict):
        for edge in mapping["graph"].get("edges", []):
            yield "relationship:" + edge["id"], edge
    if isinstance(document.get("store_investigation"), dict):
        yield "store_investigation", document["store_investigation"]


def merge(prior, fresh):
    """Retain compatible analysis; structural changes get a new queue plus the prior evidence."""
    fresh.setdefault("retired_analysis", {})
    if not isinstance(prior, dict):
        return fresh
    document = copy.deepcopy(fresh)
    for key in ("decisions", "reasoning_groups", "evidence_dependencies", "evidence_state", "relationship_proposal",
                "discovery_state", "discovery_proposals", "retired_analysis", "structural_errors"):
        if key in prior:
            document[key] = copy.deepcopy(prior[key])
    retired = document.setdefault("retired_analysis", {})
    for key in ("proposals", "system_proposals", "dependency_proposals"):
        old = {row["entity_id"]: row for row in prior.get(key, [])}
        for index, row in enumerate(document[key]):
            previous = old.pop(row["entity_id"], None)
            if previous is None:
                continue
            if key == "system_proposals" or previous.get("observation_digest") == row.get("observation_digest"):
                document[key][index] = copy.deepcopy(previous)
            else:
                retired[row["entity_id"]] = previous
        retired.update(old)
    if "store_investigation" in prior:
        document["store_investigation"] = copy.deepcopy(prior["store_investigation"])
    used_groups = {row.get("reasoning_group") for row in document["proposals"]}
    document["reasoning_groups"] = {key: row for key, row in document.get("reasoning_groups", {}).items() if key in used_groups}
    return document


def refresh(document, repo):
    """Refresh evidence independently for each analysis target; never alter proposed meanings."""
    prior = document.get("evidence_state", {})
    registry = list(document.get("evidence_dependencies", [])) + (document.get("relationship_proposal") or {}).get("evidence_dependencies", [])
    entries = dict(rows(document))
    allowed = set(entries) | set(document.get("retired_analysis", {}))
    errors = dependencies.validate_specs(registry, allowed)
    if errors:
        return errors
    states = {}
    for identity, row in entries.items():
        group_key = "group:" + row.get("reasoning_group", "")
        effective_targets = {identity, group_key}
        selected = [copy.deepcopy(spec) for spec in registry if effective_targets.intersection(spec["targets"])]
        previous = prior.get(identity, {}).get("observations", {})
        refs = list(row.get("refs", []))
        if group_key in entries:
            refs.extend(entries[group_key].get("refs", []))
        explicit_paths = {spec["path"] for spec in selected}
        for ref in sorted(set(refs)):
            match = re.fullmatch(r"(.+):(\d+)", ref)
            if not match or match[1] in explicit_paths:
                continue
            path = match[1]
            spec_id = "citation:" + path
            if any(spec["id"] == spec_id for spec in selected):
                continue
            spec = {"id": spec_id, "path": path, "method": method_for(path), "targets": [identity]}
            old = previous.get(spec_id, {})
            if "fingerprint" in old:
                spec["fingerprint"] = old["fingerprint"]
            else:
                try:
                    observed = dependencies.observe(repo, spec)
                    spec["fingerprint"] = observed["fingerprint"]
                except (OSError, ValueError, SyntaxError, UnicodeError):
                    # A raw-text fallback is explicit in the observation's method. It is never
                    # advertised as syntax-safe; callers can supply a supported scoped dependency.
                    spec["method"] = "text"
                    try:
                        spec["fingerprint"] = dependencies.observe(repo, spec)["fingerprint"]
                    except (OSError, ValueError, SyntaxError, UnicodeError):
                        spec["fingerprint"] = "0" * 64
            if old.get("method"):
                spec["method"] = old["method"]
            selected.append(spec)
        observations, changes = dependencies.reconcile(repo, selected, previous)
        issues = [change for change in changes if change["action"] != "refresh_evidence"]
        # Keep the original evidence anchor while an issue is unresolved. Rerunning cannot clear it.
        saved = {key: (previous.get(key, value) if any(c["id"] == key for c in issues) else value)
                 for key, value in observations.items()}
        for change in changes:
            if change["action"] != "refresh_evidence":
                continue
            old_path = change["previous"].get("path") or next(s["path"] for s in selected if s["id"] == change["id"])
            new_ref = change["current"]["refs"][0]
            if "refs" in row:
                row["refs"] = [new_ref if ref.rsplit(":", 1)[0] == old_path else ref for ref in row["refs"]]
            for spec in registry:
                if spec["id"] == change["id"]:
                    spec["path"] = change["current"]["path"]
        states[identity] = {"observations": saved, "status": "unresolved" if any(c["action"] in {"missing_evidence", "identity_ambiguity"} for c in issues)
                            else "investigate" if issues else "current", "issues": issues}
    document["evidence_state"] = states
    return [f"{identity}: {state['status']} — {issue['reason']}" for identity, state in states.items() for issue in state["issues"]]


def discovery_answer_valid(answer, change, repo=None):
    if answer.get("observation_digest") != dependencies.digest(change) or answer.get("outcome") not in {"no_new_scope", "analysed", "unresolved"}:
        return False
    summary = answer.get("decision_summary", "")
    if not answer.get("rationale") or answer.get("confidence") not in {"low", "medium", "high"} or not summary or len(summary) > 240 or "\n" in summary or not answer.get("refs"):
        return False
    if answer["outcome"] == "unresolved" and (not answer.get("unresolved_question") or not answer.get("resolution_needed") or not answer.get("decision_impact")):
        return False
    if repo is not None:
        for ref in answer["refs"]:
            match = re.fullmatch(r"(.+):(\d+)", ref)
            if not match:
                return False
            path = (repo / match[1]).resolve()
            if not path.is_relative_to(repo.resolve()) or not path.is_file():
                return False
            try:
                if not 0 < int(match[2]) <= len(path.read_text().splitlines()):
                    return False
            except (OSError, UnicodeError):
                return False
    return True


def check_accepted_discovery(repo, specs, allow_review=False):
    lock_path = repo / ".noru" / "privacy-datamap.lock.json"
    if not lock_path.is_file():
        return []
    lock = json.loads(lock_path.read_text())
    if "discovery_sources" not in lock:
        return []
    changes = discovery_changes(lock["discovery_sources"], discover(repo), specs)
    if allow_review and changes:
        proposal_path = repo / ".noru" / ".cache" / "privacy-datamap.proposals.json"
        document = json.loads(proposal_path.read_text()) if proposal_path.is_file() else {}
        answers = {row.get("path"): row for row in document.get("discovery_proposals", [])}
        changes = [change for change in changes if not discovery_answer_valid(answers.get(change["path"], {}), change, repo)]
    return changes


def refresh_discovery(document, current, registered=()):
    previous = document.get("discovery_state")
    if previous is None:
        document["discovery_state"] = current
        document.setdefault("discovery_proposals", [])
        document["discovery_required"] = []
        return []
    specs = list(registered) + list(document.get("evidence_dependencies", [])) + (document.get("relationship_proposal") or {}).get("evidence_dependencies", [])
    changes = discovery_changes(previous, current, specs)
    answers = {row.get("path"): row for row in document.get("discovery_proposals", [])}
    pending = []
    for change in changes:
        answer = answers.get(change["path"], {})
        if not discovery_answer_valid(answer, change):
            pending.append(change)
    document["discovery_required"] = pending
    return pending
