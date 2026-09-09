#!/usr/bin/env python3
"""Validate agent enrichment and render its review; never accept or export decisions."""

import json
import pathlib
import re
import sys

import reconcile as workflow


def requested_systems(candidate):
    return [system for system in candidate.get("system", []) if any(
        declaration.get("needs_review") for declaration in system.get("privacy_declarations", [])
    )]


def validate(document, result, candidate, repo):
    errors = workflow.analysis_cache.refresh(document, repo)
    current_discovery = result.get("discovery_sources", workflow.analysis_cache.discover(repo))
    pending_discovery = workflow.analysis_cache.refresh_discovery(document, current_discovery, candidate.get("evidence_dependencies", []))
    errors.extend(f"{row['path']}: investigate newly discovered code/configuration scope" for row in pending_discovery)
    vocabulary = workflow.VALIDATOR.load_vocabulary()

    def require(ok, message):
        if not ok:
            errors.append(message)

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    def summary(row, label):
        value = row.get("decision_summary")
        require(text(value) and len(value) <= 240 and "\n" not in value,
                f"{label}: give a single-line decision_summary of at most 240 characters")

    def uncertainty(row, label):
        has_question = text(row.get("unresolved_question")) or text(row.get("business_context_question"))
        if has_question:
            require(text(row.get("resolution_needed")), f"{label}: state what answer resolves the question")
            require(text(row.get("decision_impact")), f"{label}: explain which privacy decision the missing fact changes")
        else:
            require(not text(row.get("resolution_needed")) and not text(row.get("decision_impact")),
                    f"{label}: remove resolution and impact text when there is no question")

    def evidence(row, label):
        require(text(row.get("rationale")), f"{label}: explain the rationale")
        require(row.get("confidence") in ("low", "medium", "high"), f"{label}: set confidence")
        refs = row.get("refs")
        require(isinstance(refs, list) and bool(refs), f"{label}: cite repository evidence")
        for ref in refs if isinstance(refs, list) else []:
            match = re.fullmatch(r"(.+):(\d+)", ref) if isinstance(ref, str) else None
            valid = False
            if match:
                path = (repo / match[1]).resolve()
                if path.is_relative_to(repo.resolve()) and path.is_file():
                    try:
                        valid = 0 < int(match[2]) <= len(path.read_text(encoding="utf-8").splitlines())
                    except (OSError, UnicodeError):
                        pass
            require(valid, f"{label}: invalid source citation {ref!r}")

    def keys(row, key, vocabulary_key, label):
        values = row.get(key)
        require(isinstance(values, list) and all(isinstance(v, str) and v in vocabulary[vocabulary_key]
                for v in values), f"{label}: invalid taxonomy values in {key}")

    def indexed(key, expected):
        rows = document.get(key)
        require(isinstance(rows, list), f"{key}: supply the analysis queue")
        index = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not isinstance(row.get("entity_id"), str):
                errors.append(f"{key}: each entry needs an entity_id")
                continue
            identity = row["entity_id"]
            require(identity not in index, f"{key}: duplicate {identity}")
            index[identity] = row
        require(set(index) == set(expected), f"{key}: missing or unexpected queue items: "
                + ", ".join(sorted(set(index) ^ set(expected))))
        return index

    require(document.get("observation_digest") == result["observation_digest"],
            "proposals are stale: rerun collection and reconciliation")
    groups = document.get("reasoning_groups", {})
    require(isinstance(groups, dict), "reasoning_groups: supply a mapping")
    for identity, group in groups.items():
        evidence(group, f"reasoning group {identity}")
        require(text(group.get("summary")), f"reasoning group {identity}: name the shared meaning")
    decisions = document.get("decisions", {})
    require(isinstance(decisions, dict), "decisions: supply a mapping of decision IDs to summaries")
    for identity, decision in decisions.items():
        summary(decision, f"decision {identity}")
        uncertainty(decision, f"decision {identity}")
    decision_questions = {}
    expected = {row["entity_id"]: row for row in result["proposal_required"]}
    for identity, row in indexed("proposals", expected).items():
        label = identity
        group = groups.get(row.get("reasoning_group"), {})
        if row.get("reasoning_group"):
            require(bool(group), f"{label}: unknown reasoning group")
        effective = effective_proposal(row, document)
        evidence(effective, label)
        decision_id = row.get("decision_id")
        visible = row.get("proposal_kind") != "non_personal" or effective.get("unresolved_question")
        if visible:
            require(isinstance(decision_id, str) and decision_id in decisions, f"{label}: reference an explicit decision_id")
        if decision_id:
            require(decision_id in decisions, f"{label}: unknown decision_id")
            question_signature = tuple(effective.get(key) or "" for key in (
                "unresolved_question", "business_context_question", "resolution_needed", "decision_impact"))
            require(decision_questions.setdefault(decision_id, question_signature) == question_signature,
                    f"{label}: different questions need separate decision IDs")
        uncertainty(effective, label)
        require(row.get("observation_digest") == expected.get(identity, {}).get("observation_digest"),
                f"{label}: stale field observation")
        kind = row.get("proposal_kind")
        require(kind in ("personal", "non_personal", "ambiguous", "special_category"),
                f"{label}: supply a classification or explicit ambiguity")
        keys(row, "proposed_categories", "data_categories", label)
        categories = row.get("proposed_categories")
        if kind in ("personal", "special_category"):
            require(bool(categories), f"{label}: propose a category")
        if kind == "non_personal":
            require(categories == [], f"{label}: non-personal proposals cannot have categories")
        if isinstance(categories, list) and any(category in vocabulary["special_categories"]
                                              for category in categories if isinstance(category, str)):
            require(kind in ("special_category", "ambiguous"), f"{label}: surface possible special-category data")
        if kind == "ambiguous":
            require(text(effective.get("unresolved_question")), f"{label}: explain what prevents a decision")
        analysis = row.get("analysis") or group.get("analysis") or {}
        for boundary in ("schema", "relationships", "service_or_serialization"):
            require(isinstance(analysis, dict) and text(analysis.get(boundary)),
                    f"{label}: record {boundary} investigation or missing evidence")

    systems = {row["fides_key"]: row for row in requested_systems(candidate)}
    datasets = {row["fides_key"] for row in candidate.get("dataset", [])}
    for identity, row in indexed("system_proposals", systems).items():
        evidence(row, identity)
        activities = row.get("processing_activities")
        require(isinstance(activities, list) and bool(activities), f"{identity}: separate processing activities")
        activity_ids = set()
        for activity in activities if isinstance(activities, list) else []:
            label = f"{identity}/{activity.get('activity_id')}"
            require(text(activity.get("activity_id")) and activity.get("activity_id") not in activity_ids,
                    f"{identity}: activities need unique identifiers")
            activity_ids.add(activity.get("activity_id"))
            evidence(activity, label)
            for key, vocab in (("proposed_data_uses", "data_use"), ("proposed_data_subjects", "data_subjects")):
                keys(activity, key, vocab, label)
                require(bool(activity.get(key)) or text(activity.get("unresolved_question")),
                        f"{label}: propose {key} or explain missing evidence")
            require(text(activity.get("purpose")), f"{label}: describe this processing purpose")
            summary(activity, label)
            uncertainty(activity, label)
            relationships = activity.get("dataset_references")
            require(isinstance(relationships, list) and all(isinstance(v, str) and v in datasets
                    for v in relationships), f"{label}: invalid dataset references")
            require(text(activity.get("relationship_rationale")), f"{label}: explain datastore access or uncertainty")

    require(document.get("analysis_binding") == workflow.analysis_binding(repo, repo / ".noru" / "privacy-datamap.yml", result),
            "structure or accepted baseline changed since enrichment; rerun collection and reconciliation")

    expected_dependencies = {row["id"]: row for row in result.get("investigation_required", [])}
    for identity, row in indexed("dependency_proposals", expected_dependencies).items():
        evidence(row, identity)
        summary(row, identity)
        uncertainty(row, identity)
        require(row.get("outcome") in ("retain", "amend", "unresolved"), f"{identity}: propose retain, amend or unresolved")
        require(row.get("observation_digest") == workflow.sha256_json(expected_dependencies.get(identity, {}).get("current")),
                f"{identity}: evidence investigation is stale")
        if row.get("outcome") == "unresolved":
            require(text(row.get("unresolved_question")), f"{identity}: explain what prevents resolving the evidence change")

    for discovery in document.get("discovery_proposals", []):
        evidence(discovery, "discovery proposal")
        summary(discovery, "discovery proposal")
        uncertainty(discovery, "discovery proposal")
    proposed_mapping = document.get("relationship_proposal")
    if result.get("candidate_context"):
        require(proposed_mapping is not None, "Candidate review requires the collected relationship_proposal")
    if proposed_mapping is not None:
        errors.extend(workflow.relationships.validate_proposal(proposed_mapping, repo))
        require(bool(result.get("candidate_context")),
                "Preview the proposed mapping with collect.mjs --candidate, reconcile.py --candidate and review.py --candidate before requesting acceptance")
        if result.get("candidate_context"):
            require(workflow.dependencies.digest(proposed_mapping) == result["candidate_context"]["proposal_digest"],
                    "Preview proposal differs from collected mapping; update the source proposal and recollect")
    if result.get("accepted_current"):
        return errors

    coverage = document.get("store_investigation")
    require(isinstance(coverage, dict), "store_investigation: investigate stores outside supported schemas")
    if isinstance(coverage, dict):
        evidence(coverage, "store_investigation")
        require(text(coverage.get("search_scope")), "store_investigation: describe inspected integrations and payload contracts")
        findings = coverage.get("findings")
        require(isinstance(findings, list), "store_investigation: list findings (empty if none found)")
        for finding in findings if isinstance(findings, list) else []:
            if not isinstance(finding, dict):
                errors.append("store_investigation: invalid finding")
                continue
            evidence(finding, "store finding")
            require(text(finding.get("store")), "store finding: name the store")
            require(finding.get("status") in ("covered", "gap"), "store finding: use covered or gap")
            if finding.get("status") == "gap":
                require(text(finding.get("unresolved_question")), "store finding: explain missing structure evidence")
                uncertainty(finding, "store finding")
                summary(finding, "store finding")
            else:
                require(finding.get("dataset_reference") in datasets,
                        "store finding: covered stores must reference a collected dataset")
    return errors


def structural_blockers(document, result, derived):
    blockers = []
    for error in (derived.get("relationship_mapping") or {}).get("errors", []):
        blockers.append({"kind": "relationship_graph", "detail": error,
                         "resolution_needed": "Correct the connection graph and its evidence dependencies, then recollect."})
    for key in ("schema_conflicts", "migration_gaps"):
        for item in derived.get("coverage", {}).get(key, []):
            blockers.append({"kind": key, "detail": item,
                             "resolution_needed": "Correct the structural source or collector and rerun collection and reconciliation."})
    for item in result.get("identity_ambiguities", []):
        blockers.append({"kind": "identity_ambiguity", "detail": item,
                         "resolution_needed": "Establish the datastore identity from repository evidence and reconcile again."})
    for item in result.get("control_changes", []):
        if item["kind"] in ("taxonomy_changed", "baseline_upgrade", "invalid_dependencies", "evidence_normalizer_changed"):
            blockers.append({"kind": item["kind"], "detail": item["reason"], "resolution_needed": "Review this baseline/tooling change separately; do not reclassify unchanged code."})
    blockers.extend(document.get("structural_errors", []))
    return blockers


def effective_proposal(row, document):
    group = document.get("reasoning_groups", {}).get(row.get("reasoning_group"), {})
    decision = document.get("decisions", {}).get(row.get("decision_id"), {})
    return {**group, **decision, **{k: v for k, v in row.items() if v not in (None, "", {})}}


def build_outputs(document, result, derived, candidate, errors):
    blockers = structural_blockers(document, result, derived)
    status = "enrichment incomplete" if errors or blockers else "accepted" if result.get("accepted_current") and not document.get("relationship_proposal") else "ready for human review"
    proposals = {row["entity_id"]: effective_proposal(row, document) for row in document.get("proposals", [])}
    _, _, candidate_fields, _ = workflow.manifest_indexes(candidate)
    actions = {row["entity_id"]: row["action"] for row in result["actions"]}
    inventory = []
    for observed in workflow.field_rows(derived):
        identity = observed["entity_id"]
        proposal = proposals.get(identity)
        field = candidate_fields.get(identity, {})
        inventory.append({**observed, "classification": (proposal or {}).get("proposed_categories", field.get("data_categories", [])),
                          "decision_state": "proposed" if proposal else (
                              "accepted" if actions.get(identity) in ("carry_forward", "refresh_evidence", "identity_migration") else "deterministic proposal"),
                          "proposal": proposal, "candidate_decision": field})
    activities = []
    proposed_systems = {row["entity_id"] for row in document.get("system_proposals", [])}
    for system in document.get("system_proposals", []):
        activities.extend({**activity, "system": system["entity_id"], "decision_state": "proposed"}
                          for activity in system.get("processing_activities", []))
    for system in candidate.get("system", []):
        if system["fides_key"] not in proposed_systems:
            for declaration in system.get("privacy_declarations", []):
                activities.append({"system": system["fides_key"], "purpose": declaration.get("name"),
                    "proposed_data_uses": [declaration.get("data_use")],
                    "proposed_data_subjects": declaration.get("data_subjects", []),
                    "dataset_references": system.get("dataset_references", []), "decision_state": "accepted",
                    "evidence": declaration})
    stores = []
    for dataset in derived.get("datasets", []):
        fields = [row for row in inventory if row["dataset_key"] == dataset["fides_key"]]
        stores.append({"id": dataset["fides_key"], "name": dataset.get("name"),
                       "collections": [row["name"] for row in dataset.get("collections", [])],
                       "data_categories": sorted({key for row in fields for key in row["classification"]}),
                       "field_count": len(fields)})
    groups = {}
    technical = {}
    for field in inventory:
        proposal = field["proposal"]
        if not proposal:
            continue
        collection = field["entity_id"].rsplit("/", 1)[0]
        if proposal.get("proposal_kind") == "non_personal" and not proposal.get("unresolved_question"):
            technical[collection] = technical.get(collection, 0) + 1
            continue
        key = proposal.get("decision_id") or field["entity_id"]
        group = groups.setdefault(key, {"decision_id": key, "collections": [],
            "summary": proposal.get("decision_summary") or "Analysis required",
            "proposal_kinds": [], "data_categories": [],
            "unresolved_question": proposal.get("unresolved_question"),
            "business_context_question": proposal.get("business_context_question"),
            "resolution_needed": proposal.get("resolution_needed"), "decision_impact": proposal.get("decision_impact"), "field_ids": []})
        group["field_ids"].append(field["entity_id"])
        group["collections"] = sorted(set(group["collections"]) | {collection})
        group["proposal_kinds"] = sorted(set(group["proposal_kinds"]) | {proposal.get("proposal_kind") or "requested"})
        group["data_categories"] = sorted(set(group["data_categories"]) | set(field["classification"]))
    return {"version": "1.0.0", "status": status, "observation_digest": result["observation_digest"],
            "collection_mode": "candidate" if result.get("candidate_context") else "manifest",
            "data_map": {"stores": stores, "processing_activities": activities, "monitoring_scope": result.get("monitoring_scope", {}), "connection_questions": (derived.get("relationship_mapping") or {}).get("questions", []), "relationship_proposal": document.get("relationship_proposal")},
            "review_queue": {"structural_blockers": blockers, "analysis_errors": errors,
                             "field_groups": [] if blockers else [groups[key] for key in sorted(groups)],
                             "technical_coverage": technical,
                             "processing_decisions": [] if blockers else [row for row in activities if row["decision_state"] == "proposed"],
                             "changes": result["actions"], "collection_decisions": result["collection_review_required"],
                             "dependency_decisions": document.get("dependency_proposals", []),
                             "control_changes": result.get("control_changes", []), "system_changes": result.get("system_changes", [])},
            "evidence_record": {"fields": inventory, "reasoning_groups": document.get("reasoning_groups", {}),
                                "agent_analysis": document, "reconciliation": result, "candidate_manifest": candidate, "coverage": derived.get("coverage", {}),
                                "special_category_refs": result.get("special_category_refs", []), "relationship_mapping": derived.get("relationship_mapping", {})}}


def question(lines, row):
    unknowns = list(dict.fromkeys(value for value in (row.get("unresolved_question"), row.get("business_context_question")) if value))
    for unknown in unknowns:
        lines.append(f"  Unknown: {unknown}")
    if unknowns:
        lines.append(f"  To resolve: {row.get('resolution_needed') or 'Specify the evidence or answer required.'}")
        if row.get("decision_impact"):
            lines.append(f"  Affects: {row['decision_impact']}")


def render_outputs(outputs):
    status = outputs["status"]
    data_map = ["# Privacy data map", "", f"Status: **{status}**." + (" Proposed meanings require human acceptance." if status != "accepted" else " No changes within the monitored scope."), ""]
    if outputs["evidence_record"]["reconciliation"].get("candidate_context"):
        data_map.extend(["Candidate collection: stores and fields below use the proposed relationship graph. Accepted files are unchanged.", ""])
    scope = outputs["data_map"].get("monitoring_scope", {})
    data_map.extend([f"Monitoring: collected structure and {scope.get('registered_dependencies', 0)} registered code dependencies. Unregistered processing code is outside this baseline.", ""])
    for store in outputs["data_map"]["stores"]:
        data_map.extend([f"## {store['name']}", "", f"Collections: {', '.join(store['collections'])}",
                         f"Categories: {', '.join(store['data_categories']) or 'None established'}", ""])
    data_map.extend(["## Processing and flows", ""])
    for activity in outputs["data_map"]["processing_activities"]:
        data_map.extend([f"- **{activity['system']}: {activity.get('purpose')}** ({activity['decision_state']})",
                         f"  Subjects: {', '.join(activity.get('proposed_data_subjects', [])) or 'Unresolved'}; uses: {', '.join(activity.get('proposed_data_uses', []))}",
                         f"  Stores: {', '.join(activity.get('dataset_references', [])) or 'Unresolved datastore'}; flow: {activity.get('relationship_rationale') or 'See accepted declaration evidence'}"])
    for row in outputs["data_map"].get("connection_questions", []):
        data_map.append(f"- Connection **{row['id']}**: destination unresolved.")
    queue = outputs["review_queue"]
    discovery = outputs["evidence_record"]["agent_analysis"].get("discovery_required", [])
    review = ["# Privacy decision review", "", f"Status: **{status}**", "",
              "[Data map](privacy-datamap.map.md) · [Full evidence](privacy-datamap.evidence.json)", ""]
    proposed_mapping = outputs["data_map"].get("relationship_proposal")
    if isinstance(proposed_mapping, dict):
        review.append(f"- **{proposed_mapping.get('decision_summary', 'Review relationship mapping')}**")
        if not workflow.relationships.validate(proposed_mapping.get("graph"), proposed_mapping.get("evidence_dependencies", [])):
            for row in workflow.relationships.project(proposed_mapping["graph"])["questions"]:
                question(review, {**row, "decision_impact": "Proposed destination identity and datastore flow"})
    for row in outputs["data_map"].get("connection_questions", []):
        review.append(f"- **Connection {row['id']}**")
        question(review, {**row, "decision_impact": "Destination identity and datastore flow"})
    if queue["structural_blockers"]:
        review.extend(["## Fix structure before privacy review", ""])
        for blocker in queue["structural_blockers"]:
            review.extend([f"- {blocker.get('kind', 'structural error')}: {blocker.get('detail')}",
                           f"  To resolve: {blocker.get('resolution_needed')}"])
        review.append("Privacy decisions are deferred. Independent analysis remains in the evidence record.")
    else:
        technical = queue["technical_coverage"]
        if queue["field_groups"] or queue["collection_decisions"]:
            review.extend(["## Changes requiring decisions", ""])
        for group in queue["field_groups"]:
            review.extend([f"- **{group['summary']}**",
                           f"  Scope: {', '.join(group['collections'])} ({len(group['field_ids'])} fields; {', '.join(group['proposal_kinds'])})."])
            if group["data_categories"]:
                review.append(f"  Categories: {', '.join(group['data_categories'])}")
            question(review, group)
        if queue["collection_decisions"] or queue["processing_decisions"]:
            review.extend(["", "Accept or amend the proposals and record the accountable owner once for the affected scope."])
        if queue["collection_decisions"]:
            review.append(f"Collections: {', '.join(queue['collection_decisions'])}.")
        if technical:
            review.extend(["", "<details><summary>Technical field coverage</summary>", ""])
            review.extend(f"- {collection}: {count} proposed non-personal fields; included in collection acceptance." for collection, count in sorted(technical.items()))
            review.extend(["", "</details>"])
        removed = [row for row in queue["changes"] if row["action"] == "remove"]
        if removed:
            review.extend(["", f"{len(removed)} removed field(s); exact identities are in the evidence record."])
        if queue["dependency_decisions"]:
            review.extend(["", "## Evidence investigations", ""])
            for decision in queue["dependency_decisions"]:
                review.append(f"- **{decision.get('decision_summary') or 'Investigate changed evidence'}** ({decision.get('outcome') or 'analysis required'})")
                question(review, decision)
        if queue["system_changes"]:
            review.extend(["", "## Observed runtime changes", ""])
            for change in queue["system_changes"]:
                review.append(f"- {change['id']}: {change['previous']} → {change['current']}. {change['reason']}")
        if queue["processing_decisions"]:
            review.extend(["", "## Systems and processing decisions", ""])
        for activity in queue["processing_decisions"]:
            review.append(f"- **{activity['system']}: {activity.get('decision_summary') or 'Analysis required'}**")
            question(review, activity)
        findings = outputs["evidence_record"]["agent_analysis"].get("store_investigation", {}).get("findings", [])
        gaps = outputs["evidence_record"]["coverage"].get("unparsed_candidates", [])
        if gaps or any(finding.get("status") == "gap" for finding in findings):
            review.extend(["", "## Coverage and identity questions", ""])
        for finding in findings:
            if finding.get("status") == "gap":
                review.append(f"- {finding.get('store')}: {finding.get('decision_summary') or 'Coverage unresolved'}")
                question(review, finding)
        for gap in gaps:
            review.append(f"- Unparsed structure: {gap}. To resolve: supply a supported schema or an evidence-backed supplemental store.")
        special_keys = workflow.VALIDATOR.load_vocabulary()["special_categories"]
        special = [row["entity_id"] for row in outputs["evidence_record"]["fields"] if any(
            key in special_keys for key in row["classification"]) or (row["proposal"] or {}).get("proposal_kind") == "special_category"]
        review.extend(["", "## Possible Article 9 or Article 10 data", ""])
        review.extend([f"- {identity}" for identity in special] or ["None identified."])
    if discovery:
        review.extend(["", "## New scope to investigate", ""])
        for item in discovery:
            review.append(f"- {item['path']}: {item['question']}")
    if queue["analysis_errors"]:
        review.extend(["", "## Outstanding analysis", ""] + [f"- {error}" for error in queue["analysis_errors"]])
    return "\n".join(data_map) + "\n", "\n".join(review) + "\n"


def main(argv):
    try:
        opts = workflow.parse_args(argv)
        if opts["seal"]:
            raise ValueError("review.py cannot seal decisions")
        if opts.get("help"):
            print("usage: review.py --repo=<path> [--candidate] [--output=json]")
            return 0
        repo = opts["repo"]
        cache = workflow.cache_directory(repo, opts["candidate"])
        derived = workflow.load_json(cache / "privacy-datamap.derived.json", required=True)
        scan = workflow.load_json(cache / "privacy-datamap.scan.json", required=True)
        path = repo / ".noru" / "privacy-datamap.yml"
        manifest = workflow.load_manifest(path)
        lock = workflow.load_json(repo / ".noru" / "privacy-datamap.lock.json")
        mapping = workflow.validate_scan_mode(repo, derived, scan, opts["candidate"])
        result = workflow.reconcile(derived, scan, manifest, lock, path)
        candidate = workflow.build_candidate(derived, scan, {} if result["mode"] == "bootstrap" else manifest,
                    workflow.build_lock(derived, scan, path) if result["mode"] == "migration" else lock)
        candidate = workflow.apply_candidate_mapping(workflow.refresh_candidate_evidence(candidate, result), mapping)
        document = workflow.load_json(cache / "privacy-datamap.proposals.json", required=True)
        errors = validate(document, result, candidate, repo)
        if not errors:
            (cache / "privacy-datamap.proposals.json").write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        outputs = build_outputs(document, result, derived, candidate, errors)
        data_map, report = render_outputs(outputs)
        (cache / "privacy-datamap.map.md").write_text(data_map, encoding="utf-8")
        (cache / "privacy-datamap.review.md").write_text(report, encoding="utf-8")
        (cache / "privacy-datamap.evidence.json").write_text(json.dumps(outputs, indent=2) + "\n", encoding="utf-8")
        output = {"status": outputs["status"], "errors": errors,
                  "structural_blockers": outputs["review_queue"]["structural_blockers"]}
        print(json.dumps(output) if opts["json"] else report)
        return 1 if outputs["status"] == "enrichment incomplete" else 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        print(f"error: invalid enrichment input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
