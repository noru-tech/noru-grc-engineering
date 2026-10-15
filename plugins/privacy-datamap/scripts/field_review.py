#!/usr/bin/env python3
"""Evidence-backed classification in the existing canonical analysis document.

Compiler facts are observations. Agent decisions remain reviewable judgments; this
gate proves freshness, accounting and consistency, not semantic truth.
"""
import argparse
import collections
import copy
import hashlib
import json
import pathlib
import re
import subprocess
import sys

import analysis_storage
import dependencies
import shape_investigation
import gap_resolution
import uncertainty

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
VERSION = 2
POLICY = "evidence-classification-v3"
CONTENT_OUTCOMES = {"closed_by_runtime_validation", "bounded_content_contract", "reviewed_open_content", "accepted_certificate"}
TAXONOMY = {r["fides_key"]: r["description"] for r in json.loads((ROOT / "references/taxonomy/data_categories.json").read_text())}
SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".py", ".sql", ".prisma", ".go", ".rs", ".java", ".rb", ".php", ".json", ".yaml", ".yml", ".toml", ".tf", ".cs", ".csproj", ".props", ".targets", ".sln", ".slnx"}
EXCLUDED = {".git", ".noru", ".fides", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", "coverage", "bin", "obj"}
PERSONAL = {"yes", "no", "mixed", "unknown"}
ASSOCIATIONS = {"natural_person", "organization", "service", "mixed", "unknown", "none"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def normalized(name):
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def migration(path, exclusions=None):
    if path in (exclusions or {}).get("support_paths", []):
        return False
    parts = pathlib.PurePosixPath(path).parts
    if not parts:
        return False
    return any(re.search(r"(?:^|[-_.])migrations?(?:$|[-_.])", part, re.I) for part in parts[:-1]) or bool(re.search(
        r"(?:^|[-_.])migrations?\.sql$|\.migration\.[cm]?[jt]s$|(?:^|/)alembic/versions/|(?:^|/)drizzle/.*\.sql$|(?:^|/)(?:V\d[^/]*|R(?:\d[^/]*)?)__[^/]+\.sql$|(?:^|/)db/(?:changelog|migrate)/|(?:^|/)[0-9]{8,}[_-][^/]+\.(?:sql|[cm]?[jt]s|py|rb|php|cs)$", path, re.I))




def excluded_source(name, exclusions=None):
    exclusions = exclusions or {}
    return name in exclusions.get("paths", []) or any(name == root or name.startswith(root.rstrip("/") + "/") for root in exclusions.get("roots", []) if root)


def not_evidence(name, exclusions=None):
    """A migration, or a source the collector excluded as one: never evidence for anything."""
    return migration(name, exclusions) or excluded_source(name, exclusions)


def compact_type(value):
    result = {key: copy.deepcopy(value[key]) for key in ("kind", "name", "open", "optional", "additional_types", "key_type", "value_type") if key in value}
    if value.get("members"):
        result["members"] = [{"name": member["name"], "optional": member.get("optional", False)} for member in value["members"]]
    for key in ("variants", "items"):
        if value.get(key):
            result[key] = [{part: item.get(part) for part in ("kind", "name", "open") if part in item} for item in value[key]]
    return result

def source_inventory(repo, exclusions=None):
    """Include untracked nonignored source too; cached decisions cannot hide new code."""
    repo = pathlib.Path(repo).resolve()
    result = subprocess.run(["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard", "-z"], capture_output=True)
    candidates = result.stdout.decode().split("\0") if result.returncode == 0 else [str(p.relative_to(repo)) for p in repo.rglob("*")]
    rows = {}
    for name in sorted(set(candidates)):
        relative = pathlib.PurePosixPath(name)
        if not name or set(relative.parts) & EXCLUDED or relative.suffix.lower() not in SOURCE_SUFFIXES or not_evidence(name, exclusions):
            continue
        path = (repo / name).resolve()
        if not path.is_relative_to(repo) or not path.is_file():
            continue
        rows[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return rows


def all_rows(rows):
    for row in rows:
        yield row
        yield from all_rows(row.get("nested_members", []))


def observed_fields(derived):
    rows = []
    for dataset in derived.get("datasets", []):
        for collection in dataset.get("collections", []):
            prefix = collection.get("entity_id") or dataset["fides_key"] + "/" + collection["name"]
            for field in collection.get("fields", []):
                rows.append({**field, "entity_id": field.get("entity_id") or prefix + "/" + field["name"],
                             "dataset": dataset["fides_key"], "collection": collection["name"]})
    return rows


def structure_digest(derived):
    # Classification and annotations are deliberately absent from the snapshot.
    return digest([{k: row.get(k) for k in ("entity_id", "name", "shape", "refs", "fields")} for row in observed_fields(derived)])


def taxonomy_digest():
    paths = [path for path in (HERE / "field_review.py", HERE / "dotnet.py", HERE / "dotnet_syntax.py", HERE / "dotnet_model.py", HERE / "dotnet_diagnostics.py", HERE / "gap_resolution.py", HERE / "uncertainty.py", HERE / "migration_coverage.py", HERE / "dotnet_adapters.py", HERE / "dotnet_boundaries.py", HERE / "dotnet_contract_candidates.py", HERE / "system_review.py", ROOT / "references/dotnet-boundary-contracts.json", ROOT / "references/dotnet-framework-contracts.json", ROOT / "references/dotnet-diagnostics.json", HERE / "shape_investigation.py", HERE / "typescript_evidence.mjs", ROOT / "references/classification-guide.md", ROOT / "references/classification.json") if path.is_file()]
    return digest({"policy": POLICY, "categories": TAXONOMY, "implementation": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}})


def derived_for(document, repo=None):
    value = document.get("preview", {}).get("derived") or document.get("observations", {}).get("derived") or document.get("derived")
    if value is None and repo is not None:
        path = pathlib.Path(repo) / ".noru/.cache/privacy-datamap.derived.json"
        if path.is_file():
            value = json.loads(path.read_text())
            value.pop("_scan", None)
    return value


def evidence_errors(span, repo, exclusions=None):
    if not isinstance(span, dict):
        return ["evidence must contain a source span"]
    name = span.get("path")
    if not isinstance(name, str) or pathlib.PurePosixPath(name).is_absolute() or ".." in pathlib.PurePosixPath(name).parts or migration(name, exclusions) or excluded_source(name, exclusions):
        return ["evidence path must identify current repository source, never a migration"]
    path = (pathlib.Path(repo) / name).resolve()
    if not path.is_relative_to(pathlib.Path(repo).resolve()) or not path.is_file():
        return ["evidence source is missing or outside the repository: " + name]
    raw = path.read_bytes()
    if span.get("sha256") != hashlib.sha256(raw).hexdigest():
        return ["evidence changed: " + name]
    try:
        lines = raw.decode("utf-8").splitlines() or [""]
        start, end = span["start_line"], span["end_line"]
        left, right = span["start_column"], span["end_column"]
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end, left, right)) or not (1 <= start <= end <= len(lines)):
            raise ValueError()
        if not (1 <= left <= len(lines[start - 1]) + 1 and 1 <= right <= len(lines[end - 1]) + 1) or (start == end and right < left):
            raise ValueError()
    except (KeyError, ValueError, UnicodeError, TypeError):
        return ["invalid source span: " + name]
    if not isinstance(span.get("extractor"), str) or not span["extractor"]:
        return ["evidence lacks extractor identity: " + name]
    return []


def span_from_ref(repo, ref, exclusions=None):
    match = re.fullmatch(r"(.+):(\d+)", ref or "")
    if not match or not_evidence(match[1], exclusions):
        return None
    path = (pathlib.Path(repo) / match[1]).resolve()
    if not path.is_relative_to(pathlib.Path(repo).resolve()) or not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        lines = raw.decode().splitlines()
        line = int(match[2])
        if not 1 <= line <= len(lines):
            return None
        return {"path": match[1], "start_line": line, "end_line": line, "start_column": 1,
                "end_column": len(lines[line - 1]) + 1, "sha256": hashlib.sha256(raw).hexdigest(), "extractor": "structural-citation@2"}
    except (OSError, UnicodeError):
        return None


def register_evidence(contract, spans, repo):
    identities = []
    for span in spans or []:
        errors = evidence_errors(span, repo, contract.get("migration_exclusions"))
        if errors:
            raise ValueError("; ".join(errors))
        identity = "evidence:" + digest(span)
        contract["evidence"][identity] = copy.deepcopy(span)
        identities.append(identity)
    return sorted(set(identities))


def add_fact(contract, kind, subject, value, evidence_ids):
    fact = {"kind": kind, "subject": subject, "value": value, "evidence_ids": sorted(set(evidence_ids))}
    identity = "fact:" + digest(fact)
    contract["facts"][identity] = fact
    return identity


def compiler_facts(repo):
    adapter = HERE / "typescript_evidence.mjs"
    if not adapter.is_file():
        return {"adapter": {"name": "unavailable", "version": 0}, "tables": [], "facts": [], "gaps": [{"kind": "adapter_unavailable", "message": "Compiler evidence adapter is unavailable."}]}
    result = subprocess.run(["node", str(adapter), "--repo=" + str(repo)], capture_output=True, text=True)
    if result.returncode:
        raise ValueError("Compiler evidence extraction failed: " + result.stderr.strip()[:2000])
    return json.loads(result.stdout)


def primitive_array(type_info):
    """Scalar array values belong to their field; object arrays require member modeling."""
    def primitive(value):
        if value.get("open") or value.get("members"):
            return False
        kind = value.get("kind")
        if kind == "scalar":
            return value.get("name") not in {None, "unknown", "any"}
        if kind == "union":
            return bool(value.get("variants")) and all(primitive(part) for part in value["variants"])
        if kind == "array":
            return bool(value.get("items")) and all(primitive(part) for part in value["items"])
        return False
    return type_info.get("kind") == "array" and primitive(type_info)


def expand_writer_members(extracted):
    """Model members actually constructed by writers; declared TS shapes are not exact runtime objects."""
    if not isinstance(extracted, dict) or extracted.get("_writer_members_expanded"):
        return extracted
    result = copy.deepcopy(extracted)

    def unique(rows):
        return list({digest(row): row for row in rows}.values())

    def merge(left, right):
        merged = {member["name"]: copy.deepcopy(member) for member in left}
        for incoming in right:
            name = incoming["name"]
            if name not in merged:
                merged[name] = copy.deepcopy(incoming)
                continue
            member = merged[name]
            member["evidence"] = unique(member.get("evidence", []) + incoming.get("evidence", []))
            member["writes"] = unique(member.get("writes", []) + incoming.get("writes", []))
            member["members"] = merge(member.get("members", []), incoming.get("members", []))
            if member.get("type", {}).get("kind") == "unknown" and incoming.get("type", {}).get("kind") != "unknown":
                member["type"] = copy.deepcopy(incoming["type"])
                member["shape"] = incoming.get("shape", member.get("shape", ""))
            if member["members"]:
                member.setdefault("type", {"kind": "object"})["members"] = member["members"]
        return list(merged.values())

    def describe(value, writer, depth=0):
        if not isinstance(value, dict) or depth >= 10:
            return {"kind": "unknown", "name": "unknown"}, [], True
        if isinstance(value.get("origin"), dict):
            return describe(value["origin"], writer, depth + 1)
        kind = value.get("kind")
        if kind == "object":
            members = []
            for observed in value.get("members", []):
                if not isinstance(observed, dict) or not isinstance(observed.get("name"), str):
                    continue
                content = observed.get("value", {})
                type_info, nested, uncertain = describe(content, writer, depth + 1)
                spans = observed.get("evidence") or writer.get("evidence", [])
                members.append({"name": observed["name"], "shape": type_info.get("name", type_info.get("kind", "unknown")),
                                "type": type_info, "members": nested, "optional": True, "evidence": spans,
                                "writes": [{"kind": writer.get("kind", "write"), "value": content, "evidence": spans}],
                                "_writer_shape_unresolved": uncertain and type_info.get("kind") in {"object", "array", "unknown_object"}})
            return {"kind": "object", "name": "observed object", "members": members}, members, False
        if kind in {"array", "union"}:
            children = value.get("items" if kind == "array" else "variants", [])
            descriptions = [describe(child, writer, depth + 1) for child in children]
            members = []
            for _shape, nested, _uncertain in descriptions:
                members = merge(members, nested)
            return {"kind": kind, "name": "observed " + kind, "members": members, "items" if kind == "array" else "variants": [item[0] for item in descriptions]}, members, any(item[2] for item in descriptions)
        if kind == "literal":
            return {"kind": "scalar", "name": value.get("literal_type", "unknown")}, [], False
        if kind == "call" and value.get("local_returns"):
            # Return shapes are useful observations, not proof that every control-flow path is represented.
            descriptions = [describe(child, writer, depth + 1) for child in value["local_returns"]]
            members = []
            for _shape, nested, _uncertain in descriptions:
                members = merge(members, nested)
            return {"kind": "object" if members else "unknown", "name": "bounded local return", "members": members}, members, True
        declared = value.get("type")
        if not declared and value.get("source_property") and isinstance(value.get("base"), dict):
            owner_type = value["base"].get("type", {})
            member = next((item for item in owner_type.get("members", []) if item.get("name") == value["source_property"]), None)
            declared = member.get("type") if member else None
        if isinstance(declared, dict):
            return copy.deepcopy(declared), copy.deepcopy(declared.get("members", [])), declared.get("kind") in {"object", "array", "union", "unknown"} and not primitive_array(declared)
        return {"kind": "unknown", "name": "unknown"}, [], True

    members = result.get("members", [])
    unknown_writers = []
    for writer in result.get("writes", []):
        observed_type, observed_members, uncertain = describe(writer.get("value", {}), writer)
        members = merge(members, observed_members)
        is_container = bool(members) or result.get("type", {}).get("kind") in {"object", "array", "record", "unknown_object"} or bool(re.search(r"\bjsonb?\s*\(|\[\]|\b(?:Array|Record)<", result.get("shape", "")))
        if is_container and uncertain:
            unknown_writers.append(writer)
        if observed_members and result.get("type", {}).get("kind") == "unknown":
            result["type"] = observed_type
    result["members"] = [expand_writer_members(member) for member in members]
    if members:
        result.setdefault("type", {"kind": "object"})["members"] = result["members"]
    result["_writer_members_expanded"] = True
    result["_writer_shape_unresolved"] = bool(result.get("_writer_shape_unresolved") or unknown_writers)
    result["_unresolved_writers"] = unknown_writers
    return result


def build_contract(document, repo, compiler_report=None, fresh=False, derived=None):
    repo = pathlib.Path(repo).resolve()
    derived = derived or derived_for(document, repo)
    if not isinstance(derived, dict):
        raise ValueError("Collect and reconcile current structures before classification")
    report = compiler_report if compiler_report is not None else compiler_facts(repo)
    prior = document.get("classification_review", {}) if not fresh else {}
    exclusions = {"paths": report.get("coverage", {}).get("migration_sources", []), "roots": report.get("coverage", {}).get("migration_roots", []), "support_paths": report.get("coverage", {}).get("migration_support", [])}
    contract = {"migration_exclusions": exclusions, "version": VERSION, "policy": POLICY, "mode": "fresh" if fresh else prior.get("mode", "incremental"),
                "prior_field_decisions_used": False, "adapter": report.get("adapter", {}),
                "binding": {"observation_digest": document.get("observation_digest"), "structural_digest": structure_digest(derived),
                            "taxonomy_digest": taxonomy_digest(), "source_inventory": source_inventory(repo, exclusions)},
                "evidence": {}, "facts": {}, "fields": [], "gaps": []}
    audit_prior = document.get("classification_review", {})
    contract["uncertainty_history"] = copy.deepcopy(audit_prior.get("uncertainty_history", []))
    for old in all_rows(audit_prior.get("fields", [])):
        if old.get("accepted_uncertainty"):
            record = {"entity_id": old["entity_id"], "observation_digest": old["observation_digest"],
                      "acceptance": copy.deepcopy(old["accepted_uncertainty"])}
            if record not in contract["uncertainty_history"]:
                contract["uncertainty_history"].append(record)
    shapes = prior.get("shape_observations", {})
    if not prior and not fresh:
        lock_path = repo / ".noru/privacy-datamap.lock.json"
        if lock_path.is_file():
            try:
                lock = json.loads(lock_path.read_text())
                # Certificate shapes are still bound to current source and policy.
                shapes = lock.get("classification_review", {}).get("shape_observations", {})
            except (ValueError, OSError):
                pass
    contract["shape_observations"] = shape_investigation.current(shapes, contract["binding"])
    contract["runtime_definitions"] = {}
    for system in derived.get("systems", []):
        refs = system.get("refs", []) or system.get("meta", {}).get("noru", {}).get("refs", [])
        spans = [span for ref in refs if ref.rsplit(":", 1)[0] in contract["binding"]["source_inventory"]
                 and (span := span_from_ref(repo, ref, exclusions))]
        contract["runtime_definitions"][system["fides_key"]] = {
            "definition": copy.deepcopy(system), "observation_digest": digest(system),
            "evidence_ids": register_evidence(contract, spans, repo)}
    table_fields = []
    for table in report.get("tables", []) + derived.get("dotnet", {}).get("tables", []):
        for field in table.get("fields", []):
            table_fields.append((table, field))
    old_rows = {r["entity_id"]: r for r in all_rows(prior.get("fields", []))}

    def make_row(observed, extracted=None, parent=None):
        identity = observed["entity_id"]
        extracted = expand_writer_members(extracted or {})
        refinement = contract["shape_observations"].get(identity)
        if refinement:
            extracted = shape_investigation.merge(extracted, refinement)
        spans = extracted.get("evidence", []) or [span for ref in observed.get("refs", []) if (span := span_from_ref(repo, ref, exclusions))]
        evidence_ids = register_evidence(contract, spans, repo)
        shape = observed.get("shape") or extracted.get("shape", "")
        type_info = compact_type(extracted.get("type") or {"kind": "unknown", "name": shape})
        definition = add_fact(contract, "field_definition", identity, {"shape": shape, "type": type_info, "source_symbol": extracted.get("field_id")}, evidence_ids)
        fact_ids = [definition]
        if extracted.get("clr_type") or extracted.get("framework_contract"):
            fact_ids.append(add_fact(contract, "persistence_representation", identity,
                {key: extracted.get(key) for key in ("clr_type", "storage_type", "serialization", "framework_contract")}, evidence_ids))
        if extracted.get("_shape_observation"):
            fact_ids.append(add_fact(contract, "investigated_shape", identity,
                {"rationale": extracted["_shape_observation"]["rationale"], "original_type": extracted.get("_original_type")}, evidence_ids))
        if extracted.get("foreign_key"):
            value = extracted["foreign_key"]
            cited = register_evidence(contract, value.get("evidence", []), repo)
            fact_ids.append(add_fact(contract, "foreign_key", identity, {k: v for k, v in value.items() if k != "evidence"}, cited))
            evidence_ids = sorted(set(evidence_ids + cited))
        for writer in extracted.get("writes", []):
            cited = register_evidence(contract, writer.get("evidence", []), repo)
            fact_ids.append(add_fact(contract, "write", identity, {k: v for k, v in writer.items() if k != "evidence"}, cited))
            evidence_ids = sorted(set(evidence_ids + cited))
        members = extracted.get("members", [])
        scalar_array = primitive_array(extracted.get("type", {})) and not members and (extracted.get("_investigated_shape") or not extracted.get("_writer_shape_unresolved"))
        composite = not scalar_array and (bool(members) or type_info.get("kind") in {"object", "array", "record", "unknown_object"} or any(v.get("kind") in {"object", "array", "record"} for v in type_info.get("variants", [])) or bool(re.search(r"\bjsonb?\s*\(|\[\]|\b(?:Array|Record)<", shape)))
        row = {"entity_id": identity, "name": observed["name"], "parent_id": parent, "shape": shape, "type": type_info,
               "meaning": "Meaning is not yet established from current source.", "personal_data": "unknown", "categories": [],
               "identification_basis": "unestablished", "person_association": {"kind": "unknown", "target": None, "rationale": "", "evidence_ids": []},
               "category_fit": [], "security_sensitivity": "not_established", "rationale": "", "investigation": [],
               "evidence": sorted({contract["evidence"][key]["path"] + ":" + str(contract["evidence"][key]["start_line"]) for key in evidence_ids}),
               "evidence_ids": evidence_ids, "supporting_fact_ids": fact_ids, "nested_members": [], "composite": composite, "scalar_array": scalar_array, "branch_variants": extracted.get("branch_variants", []),
               "open_content": bool(type_info.get("open") or (not extracted.get("_investigated_shape") and (extracted.get("_writer_shape_unresolved") or type_info.get("kind") in {"record", "unknown_object"} or (composite and not members)))),
               "status": "needs_investigation", "uncertainty": "nested_modeling" if composite else "meaning", "resolution_owner": "agent",
               "confidence": "low", "evidence_strength": "unresolved", "decision_origin": "unresolved", "decision_binding": None}
        if re.search(r"token|secret|password|credential|verifier|code_challenge", normalized(row["name"])):
            row["security_sensitivity"] = "potential_credential_or_protocol_value"
        for member in members:
            child = {"name": member["name"], "entity_id": identity + "." + member["name"], "shape": member.get("shape", "")}
            row["nested_members"].append(make_row(child, member, identity))
        for writer in extracted.get("_unresolved_writers", []):
            spans = writer.get("evidence", [])
            gap = {"kind": "runtime_object_shape", "subject": identity,
                   "message": "An object writer has an unresolved runtime shape; declared TypeScript members do not establish all stored keys.",
                   "evidence_ids": register_evidence(contract, spans, repo), "status": "unresolved"}
            gap["gap_id"] = "gap:" + digest(gap)
            if not any(item["gap_id"] == gap["gap_id"] for item in contract["gaps"]):
                contract["gaps"].append(gap)
        row["observation_digest"] = digest({"definition": {"entity_id": identity, "shape": shape, "type": type_info}, "facts": fact_ids,
                                             "members": [child["observation_digest"] for child in row["nested_members"]]})
        prior_row = old_rows.get(identity)
        if prior_row and prior_row.get("user_feedback"):
            row["user_feedback"] = copy.deepcopy(prior_row["user_feedback"])
        content = (prior_row or {}).get('open_content_resolution') or {}
        if (prior.get('version') == VERSION and prior_row and row.get('open_content')
                and prior.get('binding', {}).get('source_inventory') == contract['binding']['source_inventory']
                and prior_row.get('observation_digest') == row['observation_digest']
                and prior_row.get('decision_binding') == decision_binding(prior_row, prior)
                and content.get('outcome') in CONTENT_OUTCOMES - {'accepted_certificate'}
                and content.get('rationale') and content.get('evidence_ids')
                and (content.get('outcome') != 'reviewed_open_content' or isinstance(content.get('residual_uncertainty'), str) and content['residual_uncertainty'].strip())
                and set(content['evidence_ids']) <= set(prior_row.get('evidence_ids', []))
                and all(key in prior.get('evidence', {}) and not evidence_errors(prior['evidence'][key], repo, exclusions)
                        for key in prior_row.get('evidence_ids', []))):
            # Structure and its evidenced openness do not become unknown when taxonomy policy changes.
            # This carries no categories, personal association, acceptance or proposed status.
            row['open_content_resolution'] = copy.deepcopy(content)
            row['evidence_ids'] = sorted(set(row['evidence_ids']) | set(content['evidence_ids']))
            for key in content['evidence_ids']:
                contract['evidence'][key] = copy.deepcopy(prior['evidence'][key])
            row['evidence'] = sorted({contract['evidence'][key]['path'] + ':' + str(contract['evidence'][key]['start_line']) for key in row['evidence_ids']})
        if prior.get("version") == VERSION and prior.get("binding", {}).get("taxonomy_digest") == taxonomy_digest() and prior.get("binding", {}).get("source_inventory") == contract["binding"]["source_inventory"] and prior_row and prior_row.get("observation_digest") == row["observation_digest"]:
            used = prior_row.get("evidence_ids", [])
            if all(key in prior.get("evidence", {}) and not evidence_errors(prior["evidence"][key], repo) for key in used):
                # A fresh extraction still owns structure and children; carry only the judgment.
                for key in DECISION_KEYS:
                    if key in prior_row:
                        row[key] = copy.deepcopy(prior_row[key])
                for key in used:
                    contract["evidence"][key] = copy.deepcopy(prior["evidence"][key])
                for key in prior_row.get("supporting_fact_ids", []):
                    if key in prior.get("facts", {}):
                        contract["facts"][key] = copy.deepcopy(prior["facts"][key])
                contract["prior_field_decisions_used"] = contract["prior_field_decisions_used"] or row["status"] == "proposed"
        return row

    observations = observed_fields(derived)
    observation_ids = {item["entity_id"] for item in observations}
    def structural_members(observed):
        prefix = observed["entity_id"] + "."
        children = [item for item in observations if item["entity_id"].startswith(prefix) and "." not in item["entity_id"][len(prefix):]]
        if not children:
            return None
        return {"name": observed["name"].rsplit(".", 1)[-1], "shape": observed.get("shape", ""),
                "type": {"kind": "object", "members": [{"name": child["name"].rsplit(".", 1)[-1]} for child in children]},
                "evidence": [span for ref in observed.get("refs", []) if (span := span_from_ref(repo, ref, exclusions))],
                "members": [structural_members(child) or {"name": child["name"].rsplit(".", 1)[-1], "shape": child.get("shape", ""),
                    "type": {"kind": "unknown", "name": child.get("shape", "")}, "evidence": [span for ref in child.get("refs", []) if (span := span_from_ref(repo, ref, exclusions))]} for child in children]}
    for observed in observations:
        parent = observed["entity_id"].rsplit(".", 1)[0] if "." in observed["entity_id"].rsplit("/", 1)[-1] else None
        if parent in observation_ids:
            continue
        paths = {ref.rsplit(":", 1)[0] for ref in observed.get("refs", [])}
        matches = [field for table, field in table_fields if table.get("name") == observed["collection"] and field.get("name") == observed["name"]
                   and (not table.get("dataset") or table["dataset"] == observed["entity_id"].split("/")[0])
                   and any(span.get("path") in paths for span in field.get("evidence", []))]
        contract["fields"].append(make_row(observed, matches[0] if len(matches) == 1 else structural_members(observed)))
        if len(matches) > 1:
            contract["gaps"].append({"kind": "identity_ambiguity", "subject": observed["entity_id"], "message": "More than one source field matches this observation.", "status": "unresolved", "gap_id": "gap:" + digest(observed["entity_id"])})
    # Adapter gaps stay explicit; resolving the associated field must account for them.
    prior_gaps = {gap.get("gap_id"): gap for gap in prior.get("gaps", [])}
    dotnet_gaps = [{"kind": "dotnet_extraction", "subject": gap["ref"], "message": gap["reason"],
                    "evidence": gap.get("evidence") or ([span] if (span := span_from_ref(repo, gap["ref"], exclusions)) else []),
                    **{key: gap[key] for key in ("diagnostic_id", "code", "category", "scope", "resolution_requirement", "occurrences", "details") if key in gap}}
                   for gap in derived.get("dotnet", {}).get("gaps", [])]
    for gap in report.get("gaps", []) + dotnet_gaps:
        spans = [span for span in gap.get("evidence", []) if not migration(span.get("path", ""))]
        contract["gaps"].append({**gap, "evidence_ids": register_evidence(contract, spans, repo), "status": "unresolved", "gap_id": gap_resolution.identity(gap)})
    # Every collector coverage finding enters the same decision registry. Preserve the raw
    # collector payload separately; classification never erases an observed diagnostic.
    known = {gap_resolution.identity(gap) for gap in contract["gaps"]}
    for gap in gap_resolution.observations(derived, document):
        if gap["gap_id"] in known:
            for existing in contract["gaps"]:
                if gap_resolution.identity(existing) == gap["gap_id"]:
                    existing["coverage_key"] = gap["coverage_key"]
                    existing["collector_observation_digest"] = gap["collector_observation_digest"]
            continue
        spans = gap.get("evidence") or ([span] if (span := span_from_ref(repo, gap.get("ref"), exclusions)) else [])
        gap["evidence_ids"] = register_evidence(contract, [span for span in spans if not not_evidence(span.get("path", ""), exclusions)], repo)
        contract["gaps"].append(gap)
    contract["resolution_contract_version"] = gap_resolution.VERSION
    contract["diagnostic_history"] = copy.deepcopy(prior.get("diagnostic_history", []))
    current_ids = {gap["gap_id"] for gap in contract["gaps"]}
    for old in prior.get("gaps", []):
        if old.get("gap_id") not in current_ids and old not in contract["diagnostic_history"]:
            contract["diagnostic_history"].append(copy.deepcopy(old))
    if prior.get("binding") == contract["binding"] and prior.get("accepted_certificate"):
        contract["accepted_certificate"] = prior["accepted_certificate"]
    for gap in contract["gaps"]:
        previous = prior_gaps.get(gap.get("gap_id"), {})
        if previous.get("resolution"):
            for key in ("status", "resolution", "resolution_binding", "resolution_history", "reopened_reason"):
                if key in previous: gap[key] = copy.deepcopy(previous[key])
            for key in gap.get("resolution", {}).get("evidence_ids", []):
                if key in prior.get("evidence", {}) and not evidence_errors(prior["evidence"][key], repo, exclusions):
                    contract["evidence"][key] = copy.deepcopy(prior["evidence"][key])
            # Upgrade old global bindings only when both the old signature and affected scope
            # are unchanged. Normal resolution validation still checks all cited evidence.
            if (previous.get("resolution_binding") == gap_resolution.binding(previous, prior, legacy=True)
                    and gap_resolution.binding(previous, prior) == gap_resolution.binding(gap, contract)):
                gap["resolution_binding"] = gap_resolution.binding(gap, contract)
            if gap.get("status") == "resolved" and gap_resolution.resolution_errors(gap, contract, repo):
                gap.setdefault("resolution_history", []).append(copy.deepcopy(previous["resolution"]))
                gap["status"] = "unresolved"
                gap["reopened_reason"] = "Resolution evidence, occurrences, scope or represented structure changed; reinvestigate."
    if not fresh and not prior:
        restore_accepted_decisions(contract, derived, repo)
    aggregate_members(contract)
    contract["inventory_digest"] = digest([(row["entity_id"], row["observation_digest"]) for row in all_rows(contract["fields"])])
    contract["counts"] = counts(contract)
    document["classification_review"] = contract
    return contract


DECISION_KEYS = {"meaning", "personal_data", "categories", "identification_basis", "person_association", "category_fit", "security_sensitivity",
                 "rationale", "investigation", "semantic_review", "evidence_ids", "supporting_fact_ids", "status", "uncertainty", "resolution_owner", "confidence",
                 "evidence_strength", "decision_origin", "decision_binding", "business_context_question", "resolution_needed", "decision_impact", "open_content_resolution", "decision_id", "decision_summary", "user_feedback", "accepted_uncertainty"}


def decision_binding(row, contract):
    return digest({"observation": row["observation_digest"], "policy": contract["binding"]["taxonomy_digest"],
                   "evidence": {key: contract["evidence"].get(key) for key in row.get("evidence_ids", [])},
                   "facts": {key: contract["facts"].get(key) for key in row.get("supporting_fact_ids", [])},
                   "decision": {key: row.get(key) for key in sorted(DECISION_KEYS - {"decision_binding"})}})


SEMANTIC_REVIEW_QUESTIONS = {
    "producers_and_consumers": "Trace schemas, producers, transformations, consumers and alternative writers; state absent paths.",
    "association_and_identifiers": "Identify the subject independently of meaning; inspect record lookup, correlation and attribution without requiring a foreign key.",
    "taxonomy_and_uncertainty": "Compare the most specific supported taxonomy definitions; preserve personal association when category fit is unresolved.",
    "security_and_transformations": "Inspect hash inputs and uses; assess credential/security sensitivity independently of person association.",
    "behavior_and_scope": "Separate individual actions from outcomes and bookkeeping; distinguish modeled semantics from implemented storage/transmission, including mocks and constants.",
    "counterevidence_and_gaps": "Inspect recursive members, conflicting branches and missing facts; complete code-resolvable work before requesting business context.",
}


def semantic_review_errors(row):
    review = row.get("semantic_review")
    if not isinstance(review, dict):
        return ["record the semantic review dimensions before submitting a decision"]
    errors = []
    for dimension in SEMANTIC_REVIEW_QUESTIONS:
        entry = review.get(dimension)
        if not isinstance(entry, dict) or not isinstance(entry.get("finding"), str) or not entry["finding"].strip():
            errors.append("semantic review requires a finding for " + dimension)
            continue
        evidence = entry.get("evidence_ids")
        if not isinstance(evidence, list) or not evidence or any(not isinstance(key, str) for key in evidence) or not set(evidence) <= set(row.get("evidence_ids", [])):
            errors.append("semantic review requires current evidence for " + dimension)
    return errors


def decision_template(row):
    return {"entity_id": row["entity_id"], "observation_digest": row["observation_digest"],
            **{key: copy.deepcopy(row.get(key)) for key in sorted(DECISION_KEYS - {"decision_binding", "decision_origin", "evidence_strength", "status"})},
            "additional_evidence": [],
            "semantic_review": copy.deepcopy(row.get("semantic_review")) or {key: {"finding": "", "evidence_ids": []} for key in SEMANTIC_REVIEW_QUESTIONS}}


def category_meaning_errors(row):
    """Flag a narrow contradiction in the reviewed meaning, never infer from a field name.

    This is a consistency check on the agent's claim, not a semantic classifier.
    Unknown meanings still need source investigation rather than a default category.
    """
    meaning = row.get("meaning", "")
    if not isinstance(meaning, str):
        return []
    states = re.finditer(r"\b(?:delivery|queue|queued|sending|dispatch)(?:[- ](?:audience|membership|processing|message|email))*[- ](?:state|status)\b", meaning, re.I)
    # A reviewer may contrast a supported profile with a rejected state interpretation.
    state = any(not re.search(r"\b(?:not|rather than|instead of)\s+(?:(?:a|an|the|email|message)\s+)*$",
                              meaning[:match.start()], re.I) for match in states)
    demographic = any(c == "user.demographic" or c.startswith("user.demographic.")
                      for c in row.get("categories", []))
    if state and demographic:
        return ["category/meaning mismatch: declared delivery or queue state does not establish "
                "demographic information; revisit producer/consumer evidence and category fit, "
                "or leave the classification unresolved (no replacement category inferred)"]
    return []


def row_errors(row, contract, structural_only=False):
    if not isinstance(row, dict):
        return ["Classification field must be an object."]
    errors = []
    identity = row.get("entity_id", "?")
    feedback = row.get("user_feedback")
    if feedback is not None and (not isinstance(feedback, list) or any(not isinstance(item, dict)
            or any(not isinstance(item.get(key), str) or not item[key].strip() for key in ("statement", "interpretation"))
            for item in feedback)):
        errors.append(str(identity) + ": user_feedback must contain statements and classification interpretations")
    for key in ("categories", "evidence_ids", "supporting_fact_ids", "category_fit", "investigation", "nested_members"):
        if not isinstance(row.get(key), list):
            errors.append(str(identity) + ": " + key + " must be a list")
    if not isinstance(row.get("person_association"), dict):
        errors.append(str(identity) + ": person_association must be an object")
    if errors:
        return errors
    if any(not isinstance(value, str) for key in ("evidence_ids", "supporting_fact_ids") for value in row[key]):
        return [str(identity) + ": evidence and fact IDs must be strings"]
    if any(not isinstance(entry, dict) for key in ("category_fit", "investigation", "nested_members") for entry in row[key]):
        return [str(identity) + ": category fit, investigation and members must contain objects"]
    if not isinstance(row.get("personal_data"), str) or row["personal_data"] not in PERSONAL:
        return [str(identity) + ": set personal_data to yes, no, mixed or unknown explicitly"]
    if any(not isinstance(category, str) for category in row["categories"]):
        return [str(identity) + ": categories must contain strings"]
    if structural_only:
        return []
    accepted = bool(row.get("accepted_uncertainty"))
    if accepted:
        errors.extend(uncertainty.errors(row["accepted_uncertainty"], identity, row.get("evidence_ids", [])))
    categories = row.get("categories")
    if row.get("personal_data") not in PERSONAL:
        errors.append("set personal_data to yes, no, mixed or unknown explicitly")
    if not isinstance(categories, list) or any(not isinstance(c, str) or c not in TAXONOMY for c in categories):
        errors.append("use supported taxonomy categories")
        categories = []
    if row.get("personal_data") in {"no", "unknown"} and categories:
        errors.append("non-personal or unknown values cannot carry resolved personal categories")
    reviewed_open = row.get("open_content") and (accepted or (row.get("open_content_resolution") or {}).get("outcome") in {"reviewed_open_content", "accepted_certificate"})
    if row.get("composite") and categories and not reviewed_open:
        errors.append("composite categories belong on evidenced members, not their parent")
    if row.get("personal_data") in {"yes", "mixed"} and not row.get("composite") and not categories and row.get("status") == "proposed" and not accepted:
        errors.append("known personal data without category fit remains unresolved")
    for key in ("evidence_ids", "supporting_fact_ids"):
        values = row.get(key)
        registry = contract["evidence"] if key == "evidence_ids" else contract["facts"]
        if not isinstance(values, list) or not values or any(v not in registry for v in values):
            errors.append("supply current " + key)
    summary = row.get("decision_summary")
    if summary is not None and (not isinstance(summary, str) or len(summary) > 240 or "\n" in summary):
        errors.append("decision_summary must be a single line of at most 240 characters")
    association = row.get("person_association", {})
    if association.get("kind") not in ASSOCIATIONS:
        errors.append("record explicit person association independently of field meaning")
    if row.get("personal_data") in {"no", "unknown"} and association.get("kind") in {"natural_person", "mixed"}:
        errors.append("preserve established personal association even when category fit is unresolved")
    if row.get("decision_origin") == "agent-evidence-review":
        errors.extend(semantic_review_errors(row))
    if row.get("personal_data") in {"yes", "mixed"} and association.get("kind") in {"organization", "service", "none", "unknown"}:
        errors.append("personal decision contradicts or lacks natural-person association")
    if "user.unique_id" in categories and re.search(r"\b(?:boolean|bool)\b", row.get("shape", ""), re.I):
        errors.append("boolean state is not a person identifier")
    facts = [contract["facts"].get(key, {}) for key in row.get("supporting_fact_ids", [])]
    hashed = any(fact.get("kind") == "write" and re.search(r"(?:\bhash\w*|\bsha256\b|\bsha512\b|\bdigest\b|\bbcrypt\b|\bscrypt\b)", str(fact.get("value", {}).get("value", {}).get("expression", "")), re.I) for fact in facts)
    if hashed and any(category.startswith("user.contact.email") for category in categories):
        errors.append("a hash transform does not establish storage of a raw email address")
    if row.get("identification_basis") == "relational" and row.get("personal_data") in {"yes", "mixed"}:
        targets = [fact.get("value", {}) for fact in facts if fact.get("kind") == "foreign_key"]
        for target in targets:
            target_symbol = target.get("table_symbol_id", "") + "." + target.get("target_property", target.get("column", ""))
            linked = [other for other in all_rows(contract.get("fields", [])) if any(contract["facts"].get(key, {}).get("value", {}).get("source_symbol") == target_symbol for key in other.get("supporting_fact_ids", []))]
            if any(other.get("person_association", {}).get("kind") in {"organization", "service", "none"} and other.get("status") == "proposed" for other in linked):
                errors.append("person identifier claim contradicts the classified foreign-key target")
    if row.get("status") == "proposed":
        errors.extend(category_meaning_errors(row))
        if row.get("resolution_owner") or (not accepted and (row.get("uncertainty") or row.get("personal_data") == "unknown")):
            errors.append("unresolved evidence cannot be marked proposed")
        if not row.get("meaning") or not row.get("rationale") or not row.get("investigation"):
            errors.append("resolved decisions require meaning, rationale and recorded investigation")
        if not association.get("rationale") or not association.get("evidence_ids") or not set(association["evidence_ids"]) <= set(row.get("evidence_ids", [])):
            errors.append("person association requires cited evidence and rationale")
        fit = row.get("category_fit", [])
        if not isinstance(fit, list) or {entry.get("category") for entry in fit if isinstance(entry, dict)} != set(categories):
            errors.append("explain every chosen category against its taxonomy definition")
        elif any(not entry.get("rationale") or not entry.get("evidence_ids") or not set(entry["evidence_ids"]) <= set(row.get("evidence_ids", [])) for entry in fit):
            errors.append("category fit requires specific evidence and rationale")
        if row.get("decision_binding") != decision_binding(row, contract):
            errors.append("classification decision is unbound or modified; submit it through plugin investigation")
        content = row.get("open_content_resolution")
        if row.get("open_content") and not accepted and (not isinstance(content, dict) or not content.get("rationale") or not content.get("evidence_ids") or not set(content["evidence_ids"]) <= set(row.get("evidence_ids", [])) or content.get("outcome") not in CONTENT_OUTCOMES):
            errors.append("open or unmodeled content requires an evidenced content assessment: submit open_content_resolution on this parent; resolving known members does not account for additional open content")
        if content and content.get("outcome") == "reviewed_open_content" and (not isinstance(content.get("residual_uncertainty"), str) or not content["residual_uncertainty"].strip()):
            errors.append("reviewed open content must describe its residual uncertainty")
        if content and content.get("outcome") == "accepted_certificate" and (row.get("decision_origin") != "accepted-certificate" or content.get("certificate_digest") != contract.get("accepted_certificate") or not contract.get("accepted_certificate")):
            errors.append("restored content coverage requires its current accepted certificate")
        if row.get("decision_origin") == "accepted-certificate" and not contract.get("accepted_certificate"):
            errors.append("restored decisions require the sealed accepted certificate")
    elif row.get("status") != "needs_investigation" or not row.get("uncertainty") or row.get("resolution_owner") not in {"agent", "human"}:
        errors.append("unresolved decisions need a specific question and owner")
    descendants = list(all_rows(row.get("nested_members", [])))
    if row.get("status") == "proposed" and row.get("composite") and row.get("personal_data") in {"yes", "mixed"} and not accepted and not (reviewed_open and categories) and not any(child.get("personal_data") in {"yes", "mixed"} and child.get("categories") for child in descendants):
        errors.append("personal composite requires categorized personal members; unresolved content cannot disappear from export")
    if row.get("personal_data") == "no" and any(child.get("personal_data") in {"yes", "mixed"} for child in descendants):
        errors.append("non-personal parent contradicts personal members")
    if row.get("status") == "proposed" and any(child.get("status") != "proposed" for child in descendants):
        errors.append("resolved parent cannot hide unresolved members")
    typed_members = row.get("type", {}).get("members", [])
    if typed_members and {member.get("name") for member in typed_members} != {member.get("name") for member in row.get("nested_members", [])}:
        errors.append("member inventory differs from its compiler type evidence")
    if row.get("resolution_owner") == "human":
        if not row.get("investigation") or not all(row.get(key) for key in ("business_context_question", "resolution_needed", "decision_impact")):
            errors.append("human context requires attempted investigation, a concrete unavailable fact and its decision impact")
    return [identity + ": " + error for error in errors]


def counts(contract):
    rows = list(all_rows(contract.get("fields", [])))
    return {"observed_fields": len(contract.get("fields", [])), "all_fields_and_members": len(rows),
            "status": dict(collections.Counter(row["status"] for row in rows)),
            "personal_data": dict(collections.Counter(row["personal_data"] for row in rows))}


def validate_contract(document, derived, repo, require_complete=True, candidate=None):
    contract = document.get("classification_review")
    if not isinstance(contract, dict) or contract.get("version") != VERSION:
        return ["Run the plugin classification investigation: canonical evidence/decision review is missing or obsolete."]
    errors = []
    if not all(isinstance(contract.get(key), dict) for key in ("binding", "evidence", "facts")) or not isinstance(contract.get("fields"), list) or not isinstance(contract.get("gaps"), list):
        return ["Canonical classification contract has invalid registry or inventory types."]
    def malformed(rows):
        return any(not isinstance(row, dict) or not isinstance(row.get("nested_members"), list) or malformed(row["nested_members"]) for row in rows)
    if malformed(contract["fields"]):
        return ["Canonical classification member tree is malformed."]
    if any(not isinstance(fact, dict) or not isinstance(fact.get("evidence_ids"), list) or any(not isinstance(ref, str) for ref in fact["evidence_ids"]) for fact in contract["facts"].values()):
        return ["Canonical classification fact registry is malformed."]
    binding = contract.get("binding", {})
    if binding.get("observation_digest") != document.get("observation_digest") or binding.get("structural_digest") != structure_digest(derived):
        errors.append("Classification observations changed; rerun plugin investigation.")
    if binding.get("taxonomy_digest") != taxonomy_digest():
        errors.append("Classification policy/taxonomy changed; review affected decisions.")
    if binding.get("source_inventory") != source_inventory(repo, contract.get("migration_exclusions")):
        errors.append("Classification source inventory changed, including newly added code; rerun discovery and evidence extraction.")
    declared = {row["entity_id"] for row in observed_fields(derived)}
    expected = {identity for identity in declared if not ("." in identity.rsplit("/", 1)[-1] and identity.rsplit(".", 1)[0] in declared)}
    top = [row.get("entity_id") for row in contract.get("fields", [])]
    rows = list(all_rows(contract.get("fields", [])))
    identifiers = [row.get("entity_id") for row in rows]
    if set(top) != expected or len(top) != len(expected) or not declared <= set(identifiers) or len(identifiers) != len(set(identifiers)):
        errors.append("Classification inventory has missing, duplicate or unexpected fields/members.")
    if contract.get("inventory_digest") != digest([(row.get("entity_id"), row.get("observation_digest")) for row in rows]):
        errors.append("Classification member inventory changed; rerun evidence extraction.")
    errors.extend(shape_investigation.validate(contract.get("shape_observations", {}), contract, repo))
    for key, span in contract.get("evidence", {}).items():
        if key != "evidence:" + digest(span):
            errors.append("Classification evidence identity is invalid: " + key)
        errors.extend(evidence_errors(span, repo, contract.get("migration_exclusions")))
    for key, fact in contract.get("facts", {}).items():
        if key != "fact:" + digest(fact) or any(ref not in contract.get("evidence", {}) for ref in fact.get("evidence_ids", [])):
            errors.append("Classification fact is invalid or lacks source evidence: " + key)
    for row in rows:
        errors.extend(row_errors(row, contract))
        for child in row.get("nested_members", []):
            if child.get("parent_id") != row["entity_id"] or not child.get("entity_id", "").startswith(row["entity_id"] + "."):
                errors.append("Classification member has an invalid parent: " + str(child.get("entity_id")))
    for gap in contract.get("gaps", []):
        if gap.get("resolution", {}).get("outcome") == "accepted_certificate" and (not contract.get("accepted_certificate") or gap["resolution"].get("certificate_digest") != contract.get("accepted_certificate")):
            errors.append("Restored extraction gap requires its sealed accepted certificate: " + str(gap.get("gap_id")))
        if gap.get("status") == "resolved" and gap_resolution.resolution_errors(gap, contract, repo):
            errors.append("Extraction gap resolution is unbound: " + str(gap.get("gap_id")))
    if contract.get("accepted_certificate"):
        errors.extend(restored_certificate_errors(contract, derived, repo))
    if require_complete:
        pending = [row for row in rows if row.get("status") != "proposed" and (require_complete != "agent" or row.get("resolution_owner") != "human")]
        if pending:
            errors.append(str(len(pending)) + " fields/members need plugin investigation; no acceptance or export yet.")
        partition = gap_resolution.calculate(document, derived, repo)
        unresolved_gaps = gap_resolution.agent_blockers(partition) if require_complete == "agent" else partition["active_blockers"]
        if unresolved_gaps:
            errors.append(str(len(unresolved_gaps)) + " evidence extraction gaps remain; resolve them through plugin investigation.")
    if candidate is not None:
        errors.extend(validate_projection(contract, candidate))
    return sorted(set(errors))


def apply_decisions(document, decisions, repo):
    """Apply an evidence-bound batch atomically; callers persist only on success."""
    updated = copy.deepcopy(document)
    contract = updated.get("classification_review", {})
    derived = derived_for(updated, repo)
    if not contract or not derived:
        return ["Run current plugin classification evidence extraction first."]
    errors = validate_contract(updated, derived, repo, require_complete=False)
    if errors:
        return errors
    rows = {row["entity_id"]: row for row in all_rows(contract["fields"])}
    seen = set()
    if not isinstance(decisions, list) or any(not isinstance(decision, dict) for decision in decisions):
        return ["Decision batch must be a list of objects."]
    for decision in decisions:
        identity = decision.get("entity_id")
        if identity not in rows or identity in seen:
            errors.append("Decision targets an unknown or duplicate field: " + str(identity))
            continue
        seen.add(identity)
        row = rows[identity]
        if any(item not in (decision.get("user_feedback") or []) for item in row.get("user_feedback") or []) and "user_feedback" in decision:
            errors.append(identity + ": preserve recorded user feedback when submitting a revised decision")
            continue
        if decision.get("observation_digest") != row["observation_digest"]:
            errors.append(identity + ": decision evidence is stale")
            continue
        try:
            extra = register_evidence(contract, decision.get("additional_evidence", []), repo)
        except ValueError as error:
            errors.append(identity + ": " + str(error))
            continue
        for key in DECISION_KEYS - {"decision_binding", "decision_origin", "evidence_strength", "status"}:
            if key in decision:
                row[key] = copy.deepcopy(decision[key])
        if not isinstance(row.get("evidence_ids"), list) or any(not isinstance(key, str) for key in row["evidence_ids"]):
            errors.append(str(identity) + ": evidence_ids must be a list of strings")
            continue
        row["evidence_ids"] = sorted(set(row.get("evidence_ids", []) + extra))
        row["decision_origin"] = "agent-evidence-review"
        unresolved = not row.get("accepted_uncertainty") and (row.get("personal_data") == "unknown" or bool(row.get("uncertainty")))
        row["status"] = "needs_investigation" if unresolved else "proposed"
        row["evidence_strength"] = "unresolved" if unresolved else "source_supported"
        if not unresolved:
            row["resolution_owner"] = None
        row["decision_binding"] = decision_binding(row, contract)
        # Validate safe input shapes first. Cross-field meaning and descendant
        # completeness must see the complete batch after recursive aggregation.
        errors.extend(row_errors(row, contract, structural_only=True))
    if errors:
        return sorted(set(errors))
    aggregate_members(contract)
    errors = [error for current in all_rows(contract["fields"]) for error in row_errors(current, contract)]
    if errors:
        return sorted(set(errors))
    contract["counts"] = counts(contract)
    document["classification_review"] = contract
    return []


def gap_binding(gap, contract):
    return gap_resolution.binding(gap, contract)


def apply_gap_decisions(document, decisions, repo):
    """Resolve extraction gaps through recorded investigation, never silent dismissal."""
    updated = copy.deepcopy(document)
    contract = updated.get("classification_review", {})
    derived = derived_for(updated, repo)
    if not derived:
        return ["Run plugin evidence extraction first."]
    errors = validate_contract(updated, derived, repo, require_complete=False)
    if errors:
        return errors
    gaps = {}
    for gap in contract.get("gaps", []):
        gaps.setdefault(gap.get("gap_id"), []).append(gap)
    rows = {row["entity_id"]: row for row in all_rows(contract["fields"])}
    seen = set()
    if not isinstance(decisions, list) or any(not isinstance(decision, dict) for decision in decisions):
        return ["Gap decision batch must be a list of objects."]
    for decision in decisions:
        identity = decision.get("gap_id")
        if identity not in gaps or identity in seen:
            errors.append("Unknown or duplicate gap: " + str(identity))
            continue
        seen.add(identity)
        matches = gaps[identity]
        # Adapter reports can repeat an observation. A single disposition must
        # cover every copy, but a reused ID must not hide a different source gap.
        observation = lambda gap: {key: value for key, value in gap.items()
                                   if key not in gap_resolution.MUTABLE}
        if any(observation(gap) != observation(matches[0]) for gap in matches[1:]):
            errors.append(str(identity) + ": duplicate gap ID has conflicting observations")
            continue
        try:
            extra = register_evidence(contract, decision.get("additional_evidence", []), repo)
        except ValueError as error:
            errors.append(str(error))
            continue
        cited = sorted(set(decision.get("evidence_ids", []) + extra))
        if not decision.get("rationale") or not decision.get("investigation") or not cited or any(key not in contract["evidence"] for key in cited):
            errors.append(str(identity) + ": gap resolution requires rationale, attempted investigation and current evidence")
            continue
        if decision.get("outcome") not in {"modeled", "not_applicable", "accepted_uncertainty", "unresolved"}:
            errors.append(str(identity) + ": use modeled, not_applicable, accepted_uncertainty or unresolved")
            continue
        if decision.get("outcome") == "unresolved" and decision.get("fallback_review") is not None:
            fallback_errors = gap_resolution.fallback_errors(decision["fallback_review"], cited)
            if fallback_errors:
                errors.extend(str(identity) + ": " + error for error in fallback_errors)
                continue
        expected_occurrences = gap_resolution.occurrence_ids(matches[0])
        if len(expected_occurrences) > 1 and decision.get("covered_occurrence_ids") != expected_occurrences:
            errors.append(str(identity) + ": explicitly cover every grouped occurrence")
            continue
        decision = copy.deepcopy(decision)
        decision.setdefault("covered_occurrence_ids", expected_occurrences)
        provenance = decision.setdefault("provenance", {"method": "plugin-gap-investigation"})
        if not isinstance(provenance, dict) or not isinstance(provenance.get("method"), str) or not provenance["method"].strip():
            errors.append(str(identity) + ": resolution provenance requires an investigation method")
            continue
        provenance.update(contract_version=gap_resolution.VERSION,
                          observation_digest=gap_resolution.digest(observation(matches[0])),
                          structural_digest=contract["binding"]["structural_digest"],
                          evidence={key:copy.deepcopy(contract["evidence"][key]) for key in cited})
        targets = decision.get("field_ids", [])
        def modeled(target):
            row = rows.get(target)
            # Structure may be complete while privacy meaning is still under investigation.
            return bool(row and row.get("evidence_ids") and row.get("observation_digest"))
        if decision["outcome"] == "modeled" and (not (targets or decision.get("system_ids")) or any(not modeled(target) for target in targets)):
            errors.append(str(identity) + ": modeled gaps must reference evidenced canonical fields/members; open structure remains unresolved")
            continue
        if decision["outcome"] == "not_applicable" and not decision.get("scope_rationale"):
            errors.append(str(identity) + ": explain why this extraction limitation does not affect represented runtime data")
            continue
        for gap in matches:
            if gap.get("resolution"):
                gap.setdefault("resolution_history", []).append(copy.deepcopy(gap["resolution"]))
            gap.pop("reopened_reason", None)
            gap["resolution"] = {key: copy.deepcopy(value) for key, value in decision.items() if key not in {"additional_evidence", "gap_id"}}
            gap["resolution"]["evidence_ids"] = list(cited)
            gap["status"] = "unresolved" if decision["outcome"] == "unresolved" else "resolved"
            gap["resolution_binding"] = gap_binding(gap, contract)
            if gap["status"] == "resolved":
                errors.extend(str(identity) + ": " + error for error in gap_resolution.resolution_errors(gap, contract, repo))
    if errors:
        return sorted(set(errors))
    document["classification_review"] = contract
    return []


def aggregate_members(contract):
    """Account for known children without pretending an open object has no other values."""
    def visit(row):
        for child in row.get("nested_members", []):
            visit(child)
        children = row.get("nested_members", [])
        if not children or row.get("open_content") or row.get("decision_origin") == "agent-evidence-review":
            return
        personal = any(child["personal_data"] in {"yes", "mixed"} for child in children)
        unknown = any(child["status"] != "proposed" or child["personal_data"] == "unknown" for child in children)
        nonpersonal = any(child["personal_data"] == "no" for child in children)
        if unknown:
            row["personal_data"] = "mixed" if personal else "unknown"
            row["person_association"] = {"kind": "mixed" if personal else "unknown", "target": None,
                                         "rationale": "Some members remain unresolved.", "evidence_ids": row["evidence_ids"]}
            row["status"] = "needs_investigation"
            row["uncertainty"] = "member_investigation"
            unresolved = [child for child in children if child["status"] != "proposed"]
            human_only = bool(unresolved) and all(child.get("resolution_owner") == "human" for child in unresolved)
            row["resolution_owner"] = "human" if human_only else "agent"
            if human_only:
                row["business_context_question"] = " ".join(dict.fromkeys(child.get("business_context_question", "") for child in unresolved))
                row["resolution_needed"] = "Resolve the unavailable context identified for the member fields."
                row["decision_impact"] = "Determines the classification of the remaining members; known personal members remain represented."
                row["investigation"] = [{"question": "Which member decisions remain?", "checked_fact_ids": row["supporting_fact_ids"], "outcome": "All remaining member investigations require the explicitly recorded unavailable business context."}]
                row["decision_summary"] = "Remaining member content requires business context."
                row["decision_id"] = unresolved[0].get("decision_id")
            return
        cited = sorted({key for member in [row] + children for key in member["evidence_ids"]})
        facts = sorted({key for member in [row] + children for key in member["supporting_fact_ids"]})
        row.update(personal_data="mixed" if personal and nonpersonal else "yes" if personal else "no",
                   meaning="Structured value; privacy meaning is recorded on each member.", categories=[],
                   person_association={"kind": "mixed" if personal and nonpersonal else "natural_person" if personal else "none", "target": None,
                                       "rationale": "Aggregate of complete, separately evidenced member decisions.", "evidence_ids": cited},
                   identification_basis="content" if personal else "operational", rationale="Every declared member has a complete decision; no parent category is inherited.",
                   evidence_ids=cited, supporting_fact_ids=facts, status="proposed", uncertainty=None, resolution_owner=None,
                   category_fit=[], decision_origin="member-aggregation-v1", confidence="medium", evidence_strength="source_supported",
                   investigation=[{"question": "Are all known members accounted for?", "checked_fact_ids": facts, "outcome": "All declared members have current classifications."}])
        row["decision_binding"] = decision_binding(row, contract)
    for row in contract.get("fields", []):
        visit(row)


def pending_requests(contract):
    requests = []
    for row in all_rows(contract.get("fields", [])):
        if row.get("status") == "proposed":
            continue
        kind = row.get("uncertainty", "meaning")
        requests.append({"entity_id": row["entity_id"], "observation_digest": row["observation_digest"], "priority": 1 if row.get("composite") else 2,
                         "question": kind, "owner": row.get("resolution_owner", "agent"), "resolution_owner": row.get("resolution_owner", "agent"), "evidence_ids": row.get("evidence_ids", []),
                         "supporting_fact_ids": row.get("supporting_fact_ids", []), "next_steps": ["inspect definition and every collected writer", "trace identity references", "test category meaning against taxonomy and contrary evidence"],
                         "decision_template": decision_template(row)})
    return sorted(requests, key=lambda row: (row["priority"], row["entity_id"]))


def projected_fields(rows, existing=None, supplemental=False):
    result = []
    old = {field["name"]: field for field in existing or []}
    for row in rows:
        field = copy.deepcopy(old.get(row["name"], {}))
        field.update(name=row["name"], data_categories=row["categories"], refs=row["evidence"], description=row["meaning"])
        content = row.get("open_content_resolution") or {}
        if content.get("outcome") == "reviewed_open_content":
            field["description"] += " Open content: " + content["residual_uncertainty"]
        entry = uncertainty.field_entry(row)
        meta = field.setdefault("meta", {})
        if supplemental and not meta.get('noru'):
            # Newly investigated members have no prior manifest node to copy provenance from.
            shape = row.get('shape') or row.get('type', {}).get('name') or row.get('type', {}).get('kind')
            meta['noru'] = {'shape': shape, 'evidence_kind': 'typed_contract'}
        meta.pop("accepted_uncertainties", None)
        if entry:
            meta["accepted_uncertainties"] = [entry]
            field["description"] += " " + uncertainty.disclosure(entry)
        if not meta:
            field.pop("meta", None)
        field.pop("needs_review", None)
        if row["status"] != "proposed":
            field["needs_review"] = True
        if row.get("nested_members"):
            field["fields"] = projected_fields(row["nested_members"], field.get("fields", []), supplemental)
        result.append(field)
    return result


def projected_dependencies(contract, rows, specs, repo):
    """Register only current, bound field citations; never refresh existing fingerprints."""
    missing = collections.defaultdict(set)
    checked = set()
    for row, collection_id in rows:
        spans = {contract["evidence"][key]["path"] + ":" + str(contract["evidence"][key]["start_line"]): key
                 for key in row.get("evidence_ids", []) if key in contract.get("evidence", {})}
        for ref in row.get("evidence", []):
            path = ref.rsplit(":", 1)[0]
            if any(spec.get("path") == path and (row["entity_id"] in spec.get("targets", []) or collection_id in spec.get("targets", [])) for spec in specs):
                continue
            key = spans.get(ref)
            if key is None:
                raise ValueError("Cannot register an unbound classification citation: " + ref)
            if key not in checked:
                span = contract["evidence"][key]
                if key != "evidence:" + digest(span):
                    raise ValueError("Cannot register invalid classification evidence: " + ref)
                errors = evidence_errors(span, repo, contract.get("migration_exclusions"))
                if errors:
                    raise ValueError("Cannot register classification dependency: " + "; ".join(errors))
                checked.add(key)
            missing[path].add(row["entity_id"])
    additions = []
    existing_ids = {spec.get("id") for spec in specs}
    for path, targets in sorted(missing.items()):
        # Whole-file text is deliberately conservative: a source span does not establish
        # that an AST selector covers every declaration on which the decision relies.
        spec = {"id": "classification-evidence:" + digest({"path": path, "targets": sorted(targets)}),
                "path": path, "method": "text", "targets": sorted(targets)}
        if spec["id"] in existing_ids:
            raise ValueError("Classification evidence dependency ID conflicts with an existing dependency")
        spec["fingerprint"] = dependencies.observe(pathlib.Path(repo), spec)["fingerprint"]
        additions.append(spec)
    return additions


def project_candidate(document, candidate, derived=None, repo=None):
    contract = document.get("classification_review", {})
    if contract.get("version") != VERSION or contract.get("binding", {}).get("observation_digest") != document.get("observation_digest") or (derived is not None and contract.get("binding", {}).get("structural_digest") != structure_digest(derived)):
        return candidate
    index = {row["entity_id"]: row for row in contract["fields"]}
    system_proposals = {row['entity_id']: row for row in document.get('system_proposals', [])}
    for system in candidate.get('system', []):
        proposal = system_proposals.get(system['fides_key'])
        if not proposal:
            continue
        if proposal.get('system_type'):
            system['system_type'] = proposal['system_type']
        if proposal.get('personal_data') == 'no':
            system['privacy_declarations'] = []
            record = {'rationale': proposal.get('non_personal_rationale', ''), 'refs': proposal.get('refs', []),
                      'investigation': copy.deepcopy(proposal.get('investigation', {})),
                      'needs_review': not bool(proposal.get('interpretation'))}
            if proposal.get('interpretation'): record['interpretation'] = copy.deepcopy(proposal['interpretation'])
            if repo is not None:
                import system_review
                record['evidence_binding'] = system_review.binding(system, record['refs'], repo)
            system.setdefault('meta', {})['non_personal_processing'] = record
        elif proposal.get('processing_activities'):
            declarations = []
            for activity in proposal['processing_activities']:
                from processing_declarations import expanded_names
                for name, use in expanded_names(activity):
                    declaration = {'name': name, 'data_use': use,
                        'data_subjects': activity.get('proposed_data_subjects', []),
                        'data_categories': activity.get('proposed_categories', []), 'refs': activity.get('refs', []),
                        'needs_review': not bool(activity.get('interpretation'))}
                    if activity.get('interpretation'): declaration['interpretation'] = copy.deepcopy(activity['interpretation'])
                    declarations.append(declaration)
            system['privacy_declarations'] = declarations
    dependency_rows = []
    for dataset in candidate.get("dataset", []):
        for collection in dataset.get("collections", []):
            prefix = dataset["fides_key"] + "/" + collection["name"] + "/"
            rows = [row for identity, row in index.items() if identity.startswith(prefix)]
            if rows:
                prior_compact = set(collection.get("non_personal_fields", []))
                compact = {row["name"] for row in rows if row["name"] in prior_compact and row["personal_data"] == "no" and row["status"] == "proposed" and not row.get("nested_members") and not row.get("open_content") and not row.get("accepted_uncertainty")}
                represented = [row for row in rows if row["name"] not in compact]
                collection["fields"] = projected_fields(represented, collection.get("fields", []),
                    supplemental=(dataset.get('meta') or {}).get('noru', {}).get('origin') == 'supplemental')
                if (dataset.get("meta") or {}).get("noru", {}).get("origin") == "supplemental":
                    dependency_rows.extend((row, prefix[:-1]) for row in all_rows(represented))
                if compact:
                    collection["non_personal_fields"] = sorted(compact)
                else:
                    collection.pop("non_personal_fields", None)
                # Projection can introduce nested members after the initial candidate was built.
                def names(fields, prefix=''):
                    for field in fields:
                        name = prefix + field['name']
                        yield name
                        yield from names(field.get('fields', []), name + '.')
                signature = set(names(collection['fields'])) | set(collection.get('non_personal_fields', []))
                collection['structure_digest'] = hashlib.sha256('\n'.join(sorted(signature)).encode()).hexdigest()
    export_nodes = {"dataset:" + node["fides_key"]: node for node in candidate.get("dataset", [])}
    export_nodes.update({"system:" + node["fides_key"]: node for node in candidate.get("system", [])})
    for node in export_nodes.values():
        node.setdefault("meta", {}).pop("accepted_uncertainties", None)
    for gap in contract.get("gaps", []):
        entry = uncertainty.gap_entry(gap)
        if entry:
            for target in gap["resolution"].get("export_targets", []):
                if target in export_nodes:
                    export_nodes[target]["meta"].setdefault("accepted_uncertainties", []).append(entry)
    for node in export_nodes.values():
        if not node.get("meta"):
            node.pop("meta", None)
    if repo is not None and dependency_rows:
        additions = projected_dependencies(contract, dependency_rows, candidate.get("evidence_dependencies", []), repo)
        if additions:
            candidate.setdefault("evidence_dependencies", []).extend(additions)
    return candidate


def validate_projection(contract, manifest):
    represented = {}
    descriptions = {}
    disclosures = {}
    def walk(fields, prefix):
        for field in fields:
            identity = prefix + field["name"]
            represented[identity] = field.get("data_categories", [])
            descriptions[identity] = field.get("description", "")
            disclosures[identity] = field.get("meta", {}).get("accepted_uncertainties", [])
            walk(field.get("fields", []), identity + ".")
    for dataset in manifest.get("dataset", []):
        for collection in dataset.get("collections", []):
            prefix = dataset["fides_key"] + "/" + collection["name"] + "/"
            walk(collection.get("fields", []), prefix)
            for name in collection.get("non_personal_fields", []):
                represented[prefix + name] = []
    rows = {row["entity_id"]: row for row in all_rows(contract.get("fields", []))}
    errors = []
    if set(represented) != set(rows):
        errors.append("Manifest field/member coverage differs from canonical classification.")
    export_nodes = {"dataset:" + node["fides_key"]: node for node in manifest.get("dataset", [])}
    export_nodes.update({"system:" + node["fides_key"]: node for node in manifest.get("system", [])})
    expected = {target: [] for target in export_nodes}
    for gap in contract.get("gaps", []):
        entry = uncertainty.gap_entry(gap)
        if entry:
            for target in gap["resolution"].get("export_targets", []):
                if target not in expected:
                    errors.append("Accepted uncertainty export target is missing: " + target)
                else:
                    expected[target].append(entry)
    for target, entries in expected.items():
        if export_nodes[target].get("meta", {}).get("accepted_uncertainties", []) != entries:
            errors.append(target + ": accepted uncertainty disclosure differs from canonical decisions")
    for identity in represented.keys() & rows.keys():
        entry = uncertainty.field_entry(rows[identity])
        if disclosures.get(identity, []) != ([entry] if entry else []):
            errors.append(identity + ": accepted uncertainty disclosure differs from canonical decision")
        if entry and uncertainty.disclosure(entry) not in descriptions.get(identity, ""):
            errors.append(identity + ": manifest omits accepted uncertainty")
        content = rows[identity].get("open_content_resolution") or {}
        if content.get("outcome") == "reviewed_open_content" and content.get("residual_uncertainty") not in descriptions.get(identity, ""):
            errors.append(identity + ": manifest omits reviewed open-content uncertainty")
        if sorted(represented[identity]) != sorted(rows[identity].get("categories", [])):
            errors.append(identity + ": manifest category differs from the canonical decision")
    return errors


def manifest_semantics(manifest):
    """Bind actual represented fields and processing, including explicit empty categories."""
    return {"dataset": manifest.get("dataset", []), "system": manifest.get("system", []),
            "relationship_graph": manifest.get("relationship_graph"), "evidence_dependencies": manifest.get("evidence_dependencies", [])}


def classification_certificate(document, derived, repo, manifest):
    errors = validate_contract(document, derived, repo, require_complete=True, candidate=manifest)
    if errors:
        raise ValueError("Cannot certify incomplete classification: " + "; ".join(errors))
    contract = document["classification_review"]
    return {"version": VERSION, "policy_digest": taxonomy_digest(), "structural_digest": structure_digest(derived),
            "source_inventory_digest": digest(source_inventory(repo, contract.get("migration_exclusions"))), "inventory_digest": contract["inventory_digest"], "migration_exclusions": contract.get("migration_exclusions", {}),
            "manifest_semantics_digest": digest(manifest_semantics(manifest)), "gap_inventory_digest": gap_inventory_digest(contract),
            "coverage_digest": coverage_digest(derived),
            "runtime_digest": digest(derived.get("systems", [])),
            "shape_observations": copy.deepcopy(contract.get("shape_observations", {})),
            "gap_resolutions": copy.deepcopy(contract.get("gaps", [])),
            "uncertainty_fields": {row["entity_id"]: copy.deepcopy(row) for row in all_rows(contract["fields"]) if row.get("accepted_uncertainty")},
            "uncertainty_history": copy.deepcopy(contract.get("uncertainty_history", [])),
            "uncertainty_evidence": {key: copy.deepcopy(contract["evidence"][key]) for row in all_rows(contract["fields"]) if row.get("accepted_uncertainty") for key in row["evidence_ids"]},
            "diagnostic_history": copy.deepcopy(contract.get("diagnostic_history", [])),
            "gap_evidence": {key:copy.deepcopy(contract["evidence"][key]) for gap in contract.get("gaps", []) for key in gap.get("resolution", {}).get("evidence_ids", [])}}


def coverage_digest(derived):
    return digest({key:derived.get("coverage", {}).get(key, []) for key in (*gap_resolution.COVERAGE_KEYS, "migration_coverage")})


def accepted_certificate_errors(derived, repo, candidate=None):
    """A deleted cache may be restored only from a current, sealed manifest certificate."""
    repo = pathlib.Path(repo)
    lock_path = repo / ".noru/privacy-datamap.lock.json"
    manifest_path = repo / ".noru/privacy-datamap.yml"
    if not lock_path.is_file() or not manifest_path.is_file():
        return ["No sealed classification certificate is available."]
    try:
        lock = json.loads(lock_path.read_text())
        certificate = lock.get("classification_review", {})
        from validate_manifest import load_yaml
        manifest, _loader = load_yaml(manifest_path.read_text())
        expected = {"version": VERSION, "policy_digest": taxonomy_digest(), "structural_digest": structure_digest(derived),
                    "source_inventory_digest": digest(source_inventory(repo, certificate.get("migration_exclusions"))), "manifest_semantics_digest": digest(manifest_semantics(manifest))}
        errors = []
        if certificate.get("runtime_digest") is not None and certificate["runtime_digest"] != digest(derived.get("systems", [])):
            errors.append("Sealed runtime definitions or flows changed.")
        for observation in certificate.get("gap_resolutions", []) + derived.get("coverage", {}).get("migration_coverage", {}).get("records", []):
            errors.extend(gap_resolution.observation_errors(observation, repo))
        if certificate.get("coverage_digest") != coverage_digest(derived) and (certificate.get("coverage_digest") or any(derived.get("coverage", {}).get(key) for key in gap_resolution.COVERAGE_KEYS)):
            errors.append("Sealed extraction coverage changed or requires an audited resolution contract.")
        if lock.get("accepted_manifest_sha256") != hashlib.sha256(manifest_path.read_bytes()).hexdigest():
            errors.append("Accepted classification manifest changed after sealing.")
        if any(certificate.get(key) != value for key, value in expected.items()) or not certificate.get("inventory_digest") or not certificate.get("gap_inventory_digest"):
            errors.append("Sealed classification certificate is missing or stale; run plugin investigation.")
        if candidate is not None and digest(manifest_semantics(candidate)) != certificate.get("manifest_semantics_digest"):
            errors.append("Proposed classification differs from the sealed certificate.")
        return errors
    except (OSError, ValueError, TypeError, KeyError) as error:
        return ["Cannot verify sealed classification certificate: " + str(error)]


def gap_partition(document, derived, repo=None):
    # Cache deletion may use the sealed, audited dispositions, only while the whole
    # accepted certificate remains current. A present unfinished review takes precedence.
    if repo is not None and not document.get("classification_review") and not accepted_certificate_errors(derived, repo):
        certificate = json.loads((pathlib.Path(repo) / ".noru/privacy-datamap.lock.json").read_text())["classification_review"]
        contract = {"binding": {"structural_digest": structure_digest(derived),
                    "source_inventory": source_inventory(repo, certificate.get("migration_exclusions"))},
                    "migration_exclusions": certificate.get("migration_exclusions", {}),
                    "gaps": copy.deepcopy(certificate.get("gap_resolutions", [])),
                    "evidence": certificate.get("gap_evidence", {}), "fields": []}
        # The complete snapshot binds both target definitions and recorded dispositions.
        for gap in contract["gaps"]:
            gap.setdefault("resolution_history", []).append(copy.deepcopy(gap.get("resolution", {})))
            gap["resolution"] = {**{key: copy.deepcopy(gap["resolution"][key]) for key in ("accepted_uncertainty", "export_targets", "covered_occurrence_ids") if key in gap["resolution"]}, "outcome": "accepted_certificate", "certificate_digest": digest(certificate)}
            contract["accepted_certificate"] = digest(certificate)
            gap["resolution_binding"] = gap_binding(gap, contract)
        return gap_resolution.calculate({**document, "classification_review":contract}, derived, repo)
    return gap_resolution.calculate(document, derived, repo)


def validate_repository(document, derived, repo, require_complete=True, candidate=None):
    errors = validate_contract(document, derived, repo, require_complete=require_complete, candidate=candidate)
    if errors and require_complete and not document.get("classification_review") and not accepted_certificate_errors(derived, repo, candidate) and not gap_partition(document, derived, repo)["active_blockers"]:
        return []
    return errors


def gap_inventory_digest(contract):
    return digest(sorted((gap.get("gap_id") for gap in contract.get("gaps", [])), key=str))


def restored_certificate_errors(contract, derived, repo):
    errors = accepted_certificate_errors(derived, repo)
    if errors:
        return errors
    certificate = json.loads((pathlib.Path(repo) / ".noru/privacy-datamap.lock.json").read_text())["classification_review"]
    inventory = digest([(row["entity_id"], row["observation_digest"]) for row in all_rows(contract["fields"])])
    if contract.get("accepted_certificate") != digest(certificate) or inventory != certificate.get("inventory_digest") or gap_inventory_digest(contract) != certificate.get("gap_inventory_digest"):
        errors.append("Rebuilt classification coverage differs from the sealed accepted certificate.")
    return errors


def restore_accepted_decisions(contract, derived, repo):
    if accepted_certificate_errors(derived, repo):
        return
    from validate_manifest import load_yaml
    manifest, _loader = load_yaml((pathlib.Path(repo) / ".noru/privacy-datamap.yml").read_text())
    certificate = json.loads((pathlib.Path(repo) / ".noru/privacy-datamap.lock.json").read_text())["classification_review"]
    represented = {}
    def visit(items, prefix):
        for field in items:
            identity = prefix + field["name"]
            represented[identity] = field
            visit(field.get("fields", []), identity + ".")
    for dataset in manifest.get("dataset", []):
        for collection in dataset.get("collections", []):
            prefix = dataset["fides_key"] + "/" + collection["name"] + "/"
            visit(collection.get("fields", []), prefix)
            for name in collection.get("non_personal_fields", []):
                represented[prefix + name] = {"name": name, "data_categories": []}
    rows = list(all_rows(contract["fields"]))
    inventory = digest([(row["entity_id"], row["observation_digest"]) for row in rows])
    if set(represented) != {row["entity_id"] for row in rows} or inventory != certificate.get("inventory_digest") or gap_inventory_digest(contract) != certificate.get("gap_inventory_digest"):
        return
    contract["accepted_certificate"] = digest(certificate)
    contract["evidence"].update(copy.deepcopy(certificate.get("uncertainty_evidence", {})))
    contract["diagnostic_history"] = copy.deepcopy(certificate.get("diagnostic_history", []))
    contract["uncertainty_history"] = copy.deepcopy(certificate.get("uncertainty_history", []))
    # The certificate attests prior completion; do not invent the discarded investigation or
    # claim that a particular runtime validator was inspected during this cache reconstruction.
    for row in reversed(rows):
        preserved = certificate.get("uncertainty_fields", {}).get(row["entity_id"])
        if preserved:
            for key in DECISION_KEYS:
                if key in preserved:
                    row[key] = copy.deepcopy(preserved[key])
            row["decision_binding"] = decision_binding(row, contract)
            continue
        field = represented[row["entity_id"]]
        categories = field.get("data_categories", [])
        children = row.get("nested_members", [])
        personal = bool(categories) or any(child["personal_data"] in {"yes", "mixed"} for child in children)
        nonpersonal = any(child["personal_data"] in {"no", "mixed"} for child in children)
        row.update(personal_data="mixed" if personal and nonpersonal else "yes" if personal else "no", categories=categories,
                   meaning=field.get("description") or "Previously accepted field meaning, bound to unchanged source and policy.",
                   identification_basis="content" if personal else "operational",
                   person_association={"kind": "mixed" if personal and nonpersonal else "natural_person" if personal else "none", "target": None,
                                       "rationale": "The sealed accepted classification certificate binds this decision to unchanged evidence.", "evidence_ids": row["evidence_ids"]},
                   category_fit=[{"category": category, "rationale": "Previously accepted meaning under the same taxonomy and evidence.", "evidence_ids": row["evidence_ids"]} for category in categories],
                   rationale="Restored from the current accepted manifest; the lock certificate verifies source, structure, policy and represented semantics.",
                   investigation=[{"question": "Has the accepted evidence changed?", "checked_fact_ids": row["supporting_fact_ids"], "outcome": "The sealed certificate matches current source and manifest."}],
                   status="proposed", uncertainty=None, resolution_owner=None, decision_origin="accepted-certificate", evidence_strength="accepted_current", confidence="medium")
        if row.get("open_content"):
            row["open_content_resolution"] = {"outcome": "accepted_certificate", "certificate_digest": digest(certificate),
                "rationale": "Prior complete content coverage is attested by the unchanged sealed snapshot; original investigation details were in the deleted cache.", "evidence_ids": row["evidence_ids"]}
        row["decision_binding"] = decision_binding(row, contract)
    for gap in contract.get("gaps", []):
        audited = next((old for old in certificate.get("gap_resolutions", []) if old.get("gap_id")==gap.get("gap_id")), None)
        if audited:
            gap["resolution_history"] = copy.deepcopy(audited.get("resolution_history", [])) + [copy.deepcopy(audited["resolution"])]
        gap["status"] = "resolved"
        gap["resolution"] = {**{key: copy.deepcopy(audited["resolution"][key]) for key in ("accepted_uncertainty", "export_targets", "covered_occurrence_ids") if audited and key in audited["resolution"]}, "outcome": "accepted_certificate", "certificate_digest": digest(certificate),
            "rationale": "Prior resolution of this exact extraction gap is attested by the unchanged sealed snapshot; original investigation details were in the deleted cache."}
        gap["resolution_binding"] = gap_binding(gap, contract)
    contract["prior_field_decisions_used"] = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, required=True)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument('--candidate', action='store_true', help='Require candidate mode; otherwise follows the current cache collection mode')
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    path = repo / ".noru/.cache/privacy-datamap.analysis.json"
    document = analysis_storage.load(path) or {}
    import investigation_status
    investigation_status.mode(document, args.candidate)
    derived = derived_for(document, repo)
    if not derived:
        observed = repo / ".noru/.cache/privacy-datamap.derived.json"
        if observed.is_file():
            derived = json.loads(observed.read_text())
    if not derived:
        raise ValueError("Collect and reconcile current structures first")
    if not args.check:
        build_contract(document, repo, fresh=args.fresh, derived=derived)
        analysis_storage.save(path, document)
    errors = validate_contract(document, derived, repo, require_complete=args.check)
    progress = investigation_status.progress(document, repo, path)
    print(investigation_status.line(progress), file=sys.stderr)
    print(json.dumps({"mode": progress['mode'], "progress": progress, "status": "classification incomplete" if errors or pending_requests(document.get("classification_review", {})) else "classification ready for review",
                      "counts": document.get("classification_review", {}).get("counts", {}), "errors": errors}))
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError) as error:
        print("error: " + str(error), file=sys.stderr)
        raise SystemExit(2)
