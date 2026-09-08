#!/usr/bin/env python3
"""Framework-independent relationship contracts; never import or execute repository code."""
import hashlib
import json
import pathlib
import re
import sys

KINDS = {"runtime", "client", "connection", "datastore", "schema", "payload"}
PAIRS = {("runtime", "client"), ("client", "connection"),
         ("connection", "datastore"), ("client", "schema"), ("client", "payload")}
IDENTITY = re.compile(r"^[a-z][a-z0-9_]*$")


def validate(graph, specs):
    errors = []
    if not isinstance(graph, dict) or set(graph) != {"nodes", "edges"}:
        return ["relationship_graph requires nodes and edges only"]
    if not isinstance(graph["nodes"], list) or not isinstance(graph["edges"], list):
        return ["relationship_graph nodes and edges must be arrays"]
    nodes, edges = {}, {}
    for row in graph["nodes"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            errors.append("relationship node requires a string id")
            continue
        key = row["id"]
        if not IDENTITY.fullmatch(key) or key in nodes:
            errors.append(f"{key}: node id must be unique and use lowercase letters, digits and underscores")
        nodes[key] = row
        if set(row) - {"id", "kind", "key", "paths", "unresolved_question", "resolution_needed"} or not isinstance(row.get("kind"), str) or row.get("kind") not in KINDS:
            errors.append(f"{key}: invalid node properties or kind")
        kind = row.get("kind") if isinstance(row.get("kind"), str) else ""
        if kind in {"runtime", "datastore"}:
            if not isinstance(row.get("key"), str) or not IDENTITY.fullmatch(row["key"]):
                errors.append(f"{key}: runtime/datastore requires a stable manifest key")
        elif "key" in row:
            errors.append(f"{key}: only runtime/datastore nodes have manifest keys")
        if kind in {"schema", "payload"}:
            paths = row.get("paths")
            if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not p or pathlib.PurePosixPath(p).is_absolute() or ".." in pathlib.PurePosixPath(p).parts or "\\" in p for p in paths):
                errors.append(f"{key}: schema/payload requires repository-relative paths")
        elif "paths" in row:
            errors.append(f"{key}: only schema/payload nodes have paths")
        if "unresolved_question" in row or "resolution_needed" in row:
            if kind != "connection" or any(not isinstance(row.get(k), str) or not row[k].strip() for k in ("unresolved_question", "resolution_needed")):
                errors.append(f"{key}: unresolved connection requires a question and resolution_needed")
    if errors:
        return errors
    deps = {s["id"]: s for s in specs if isinstance(s, dict) and isinstance(s.get("id"), str)} if isinstance(specs, list) else {}
    for row in graph["edges"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            errors.append("relationship edge requires a string id")
            continue
        key = row["id"]
        if not IDENTITY.fullmatch(key) or key in edges:
            errors.append(f"{key}: edge id must be unique and use lowercase letters, digits and underscores")
        edges[key] = row
        if set(row) != {"id", "source", "target", "dependencies", "rationale", "confidence"}:
            errors.append(f"{key}: edge requires source, target, dependencies, rationale and confidence")
        source, target = row.get("source"), row.get("target")
        if not isinstance(source, str) or not isinstance(target, str) or source not in nodes or target not in nodes:
            errors.append(f"{key}: relationship endpoint is unknown")
        elif (nodes[source].get("kind"), nodes[target].get("kind")) not in PAIRS:
            errors.append(f"{key}: invalid relationship direction")
        if not isinstance(row.get("confidence"), str) or row.get("confidence") not in {"low", "medium", "high"} or not isinstance(row.get("rationale"), str) or not row["rationale"].strip():
            errors.append(f"{key}: evidence requires rationale and confidence")
        required = row.get("dependencies")
        if not isinstance(required, list) or not required or any(not isinstance(d, str) or d not in deps or "relationship:" + key not in deps[d].get("targets", []) for d in required):
            errors.append(f"{key}: register evidence dependencies targeting relationship:{key}")
    if errors:
        return errors
    keys = [n["key"] for n in nodes.values() if n["kind"] == "datastore"]
    if len(keys) != len(set(keys)):
        errors.append("datastore nodes must have distinct keys; represent an evidenced shared destination with one node")
    if any("connection_" + n["id"] in keys for n in nodes.values() if n["kind"] == "connection" and n.get("unresolved_question")):
        errors.append("unresolved connection identity collides with a datastore key")
    for key, node in nodes.items():
        outgoing = [nodes[e["target"]] for e in edges.values() if e["source"] == key]
        if node["kind"] == "client":
            if len([n for n in outgoing if n["kind"] == "connection"]) != 1:
                errors.append(f"{key}: client requires exactly one connection identity")
            if not any(e["target"] == key and nodes[e["source"]]["kind"] == "runtime" for e in edges.values()):
                errors.append(f"{key}: establish the runtime using this client")
        if node["kind"] == "connection":
            if len(outgoing) > 1:
                errors.append(f"{key}: connection has multiple destinations; retain separate connection identities")
            if not outgoing and not node.get("unresolved_question"):
                errors.append(f"{key}: unresolved destination requires a question and resolution_needed")
            if outgoing and node.get("unresolved_question"):
                errors.append(f"{key}: choose a resolved destination or an unresolved question")
        if node["kind"] in {"schema", "payload"} and not any(e["target"] == key for e in edges.values()):
            errors.append(f"{key}: schema/payload needs its actual client binding")
    return errors


def targets(graph):
    if not isinstance(graph, dict) or not isinstance(graph.get("edges"), list):
        return set()
    return {"relationship:" + e["id"] for e in graph["edges"] if isinstance(e, dict) and isinstance(e.get("id"), str)}


def project(graph):
    """Project explicit bindings; shared schemas never imply shared connections."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    outgoing = {key: [] for key in nodes}
    for edge in graph["edges"]:
        outgoing[edge["source"]].append(edge["target"])
    bindings, flows, questions = {}, {}, []
    for key, node in nodes.items():
        if node["kind"] == "connection" and node.get("unresolved_question"):
            questions.append({"id": key, "unresolved_question": node["unresolved_question"], "resolution_needed": node["resolution_needed"]})
        if node["kind"] != "client":
            continue
        connection = next(n for n in outgoing[key] if nodes[n]["kind"] == "connection")
        destinations = outgoing[connection]
        # Unknown destinations retain the connection's own stable identity.
        boundary = nodes[destinations[0]]["key"] if destinations else "connection_" + connection
        runtimes = [nodes[e["source"]]["key"] for e in graph["edges"] if e["target"] == key and nodes[e["source"]]["kind"] == "runtime"]
        flows.setdefault(boundary, set()).update(runtimes)
        for target in outgoing[key]:
            if nodes[target]["kind"] in {"schema", "payload"}:
                for path in nodes[target]["paths"]:
                    bindings.setdefault(path, set()).add(boundary)
    return {"bindings": {k: sorted(v) for k, v in sorted(bindings.items())},
            "flows": {k: sorted(v) for k, v in sorted(flows.items())},
            "questions": sorted(questions, key=lambda q: q["id"])}


def validate_proposal(proposal, repo):
    """Validate graph structure and current evidence without accepting either."""
    import dependencies
    if not isinstance(proposal, dict):
        return ["relationship_proposal must be an object"]
    summary = proposal.get("decision_summary")
    errors = []
    if set(proposal) != {"graph", "evidence_dependencies", "decision_summary"}:
        errors.append("relationship_proposal requires graph, evidence_dependencies and decision_summary only")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 240 or "\n" in summary:
        errors.append("relationship_proposal requires a single-line decision_summary of at most 240 characters")
    graph, specs = proposal.get("graph"), proposal.get("evidence_dependencies", [])
    errors.extend(dependencies.validate_specs(specs, targets(graph)))
    if errors:
        return errors
    errors.extend(validate(graph, specs))
    if errors:
        return errors
    for spec in specs:
        try:
            observation = dependencies.observe(repo, spec)
            if observation["fingerprint"] != spec["fingerprint"]:
                errors.append(f"{spec['id']}: relationship evidence changed; investigate and refresh the proposal")
        except (OSError, ValueError, SyntaxError, UnicodeError) as error:
            errors.append(f"{spec['id']}: relationship evidence unavailable: {error}")
    return errors


def candidate_mapping(repo):
    import dependencies
    import analysis_cache
    repo = pathlib.Path(repo).resolve()
    path = repo / ".noru" / ".cache" / "privacy-datamap.proposals.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    proposal = document.get("relationship_proposal") if isinstance(document, dict) else None
    errors = validate_proposal(proposal, repo)
    if errors:
        raise ValueError("Cannot preview mapping: " + "; ".join(errors))
    manifest = repo / ".noru" / "privacy-datamap.yml"
    # Cache artifacts are excluded by source_snapshot, so previewing cannot invalidate itself.
    lock = repo / ".noru" / "privacy-datamap.lock.json"
    context = {"proposal_digest": dependencies.digest(proposal),
               "lock_digest": dependencies.digest(lock.read_text()) if lock.is_file() else None,
               "manifest_digest": dependencies.digest(manifest.read_text()) if manifest.is_file() else None,
               "discovery_digest": dependencies.digest(analysis_cache.discover(repo))}
    return {**project(proposal["graph"]), "graph": proposal["graph"], "errors": [],
            "proposal": proposal, "candidate_context": context}


def verify_candidate(repo, derived, scan):
    try:
        current = candidate_mapping(repo)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError(f"Cannot verify candidate preview: {error}") from error
    artifact = pathlib.Path(repo) / ".noru" / ".cache" / "privacy-datamap-preview" / "privacy-datamap.derived.json"
    if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != scan.get("candidate_artifact_sha256"):
        raise ValueError("Candidate observations changed; rerun collect.mjs --candidate")
    context = current["candidate_context"]
    if scan.get("candidate_context") != context or derived.get("relationship_mapping") != current:
        raise ValueError("Candidate mapping, evidence or baseline changed; rerun collect.mjs --candidate and reconcile.py --candidate")
    return current


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--candidate":
        try:
            print(json.dumps(candidate_mapping(pathlib.Path(sys.argv[2])), sort_keys=True))
            return 0
        except (OSError, ValueError, TypeError, KeyError) as error:
            print(str(error), file=sys.stderr)
            return 1
    import validate_manifest
    path = pathlib.Path(sys.argv[1])
    try:
        document, _ = validate_manifest.load_yaml(path.read_text())
    except (ValueError, OSError, UnicodeError) as error:
        print(json.dumps({"bindings": {}, "flows": {}, "questions": [], "errors": [f"Cannot read relationship manifest: {error}"]}))
        return
    graph = document.get("relationship_graph") if isinstance(document, dict) else None
    if graph is None:
        print(json.dumps({"bindings": {}, "flows": {}, "questions": [], "errors": []}))
        return
    errors = validate(graph, document.get("evidence_dependencies", []))
    result = {"bindings": {}, "flows": {}, "questions": []} if errors else project(graph)
    print(json.dumps({**result, "graph": graph, "errors": errors}, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
