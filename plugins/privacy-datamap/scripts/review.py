#!/usr/bin/env python3
"""Validate agent enrichment and render its review; never accept or export decisions."""

import json
import analysis_storage
import uncertainty
import field_review
import gap_resolution
import pathlib
import re
import sys

import reconcile as workflow


def requested_systems(candidate):
    return [system for system in candidate.get("system", []) if (not system.get("privacy_declarations") and (not system.get('meta', {}).get('non_personal_processing') or system.get('meta', {}).get('non_personal_processing', {}).get('needs_review'))) or any(
        declaration.get("needs_review") for declaration in system.get("privacy_declarations", [])
    )]


def validate(document, result, candidate, repo):
    requested = requested_systems(candidate)
    lock = workflow.load_json(repo / ".noru" / "privacy-datamap.lock.json")
    workflow.add_semantic_review(result, lock, candidate, document)
    errors = workflow.analysis_cache.refresh(document, repo)
    derived = field_review.derived_for(document, repo)
    if not derived:
        errors.append("Classification observations are unavailable; recollect before review.")
    else:
        field_review.project_candidate(document, candidate, derived=derived, repo=repo)
        errors.extend(field_review.validate_repository(document, derived, repo, require_complete="agent", candidate=candidate))

    errors.extend(workflow.VALIDATOR.dataset_inputs.dependency_gaps(candidate))
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
    # Canonical field/member decisions own classification. The older proposal list
    # remains historical investigation data and never bypasses the recursive gate.

    systems = {row["fides_key"]: row for row in requested}
    datasets = {row["fides_key"] for row in candidate.get("dataset", [])}
    for identity, row in indexed("system_proposals", systems).items():
        evidence(row, identity)
        require(text(row.get("system_type")), f"{identity}: propose the architectural system type")
        investigation = row.get("investigation", {})
        for scope in ("runtime_processing", "external_recipients", "storage", "infrastructure"):
            require(isinstance(investigation, dict) and text(investigation.get(scope)),
                    f"{identity}: record {scope} investigation, including evidence for absence or missing evidence")
        activities = row.get("processing_activities")
        if row.get('personal_data') == 'no':
            require(activities == [], f'{identity}: a non-personal system must not declare personal processing activities')
            require(text(row.get('non_personal_rationale')), f'{identity}: explain the evidenced absence of personal processing')
            linked = set(systems[identity].get('dataset_references', []))
            for field in field_review.all_rows(document.get('classification_review', {}).get('fields', [])):
                if field['entity_id'].split('/')[0] in linked:
                    require(field.get('personal_data') == 'no' and field.get('status') == 'proposed',
                            f'{identity}: non-personal outcome conflicts with linked personal or unresolved data')
            continue
        require(isinstance(activities, list) and bool(activities), f"{identity}: separate processing activities")
        activity_ids = set()
        declaration_names = set()
        for activity in activities if isinstance(activities, list) else []:
            from processing_declarations import expanded_names
            for name, _use in expanded_names(activity):
                require(name not in declaration_names,
                    f'{identity}: privacy declaration names must be unique; split processing into single-purpose activities with distinct names')
                declaration_names.add(name)
            label = f"{identity}/{activity.get('activity_id')}"
            require(text(activity.get("activity_id")) and activity.get("activity_id") not in activity_ids,
                    f"{identity}: activities need unique identifiers")
            activity_ids.add(activity.get("activity_id"))
            evidence(activity, label)
            for key, vocab in (("proposed_data_uses", "data_use"), ("proposed_data_subjects", "data_subjects"), ("proposed_categories", "data_categories")):
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
            mode = activity.get("processing_mode")
            require(mode in ("stored", "transient", "unknown"), f"{label}: identify stored, transient or unknown processing")
            require(mode != "stored" or bool(relationships), f"{label}: stored processing needs a dataset")
            require(mode != "unknown" or text(activity.get("unresolved_question")), f"{label}: explain unknown processing scope")
            recipients = activity.get("recipient_systems")
            known_systems = {system["fides_key"] for system in candidate.get("system", [])}
            require(isinstance(recipients, list) and all(isinstance(v, str) and v in known_systems for v in recipients),
                    f"{label}: recipient_systems must reference represented systems")
            require(text(activity.get("recipient_rationale")), f"{label}: explain recipients, evidence for no sharing, or unresolved destination")

    errors.extend(validate_removals(document, result, candidate, repo))

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
        require(proposed_mapping is not None or document.get("structural_proposal"), "Candidate review requires the collected relationship_proposal or structural_proposal")
    if document.get("structural_proposal"):
        require(bool(result.get("candidate_context")), "Preview structural proposals with --candidate before review")
        require(workflow.dependencies.digest(document["structural_proposal"]) == result.get("candidate_context", {}).get("structural_digest"), "Structural proposal differs from collected candidate")
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
                require(text(finding.get("structure_rationale")),
                        "store finding: explain the modeled payload boundary and verify nested structure against its evidence")
                require(finding.get("dataset_reference") in datasets,
                        "store finding: covered stores must reference a collected dataset")
    return errors


def validate_removals(document, result, candidate, repo, accepting=False):
    """Removed observations need explanations independently of the candidate's remaining flags."""
    expected = {r["entity_id"]: r for r in result.get("removal_required", [])}
    rows = document.get("removal_proposals", [])
    errors = []
    if not isinstance(rows, list):
        return ["removal_proposals: supply the investigation queue"]
    index = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("entity_id"), str):
            errors.append("removal_proposals: each entry needs an entity_id")
            continue
        identity = row["entity_id"]
        if identity in index:
            errors.append(f"{identity}: duplicate removal proposal")
        index[identity] = row
    if set(index) != set(expected):
        errors.append("removal_proposals: missing or unexpected queue items: " + ", ".join(sorted(set(index) ^ set(expected))))
    references = {r["fides_key"] for key in ("dataset", "system") for r in candidate.get(key, [])}
    for identity, row in index.items():
        for key in ("rationale", "decision_summary"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                errors.append(f"{identity}: supply {key}")
        decision_summary = row.get("decision_summary")
        if isinstance(decision_summary, str) and (len(decision_summary) > 240 or "\n" in decision_summary):
            errors.append(f"{identity}: use a concise single-line decision summary")
        if row.get("confidence") not in ("low", "medium", "high"):
            errors.append(f"{identity}: set confidence")
        refs = row.get("refs")
        if not isinstance(refs, list) or not refs:
            errors.append(f"{identity}: cite current evidence for the removal")
        for ref in refs if isinstance(refs, list) else []:
            match = re.fullmatch(r"(.+):(\d+)", ref) if isinstance(ref, str) else None
            try:
                path = (repo / match[1]).resolve() if match else None
                valid = path and path.is_relative_to(repo.resolve()) and path.is_file() and 0 < int(match[2]) <= len(path.read_text().splitlines())
            except (OSError, UnicodeError):
                valid = False
            if not valid:
                errors.append(f"{identity}: invalid current removal citation {ref!r}")
        if row.get("observation_digest") != expected.get(identity, {}).get("observation_digest"):
            errors.append(f"{identity}: stale removal investigation")
        outcome = row.get("outcome")
        if outcome not in ("retired", "replaced", "amended", "gap"):
            errors.append(f"{identity}: explain retirement, replacement, amendment or coverage gap")
        question = row.get("unresolved_question") or row.get("business_context_question")
        if outcome == "gap" and not question:
            errors.append(f"{identity}: explain missing coverage")
        if question and not all(isinstance(row.get(k), str) and row[k].strip() for k in ("resolution_needed", "decision_impact")):
            errors.append(f"{identity}: state what resolves the question and its decision impact")
        if accepting and (outcome == "gap" or question):
            errors.append(f"{identity}: resolve the disappearance before accepting the new baseline")
        if outcome == "replaced" and row.get("replacement_reference") not in references:
            errors.append(f"{identity}: reference the represented replacement dataset or system")
    return errors


def structural_blockers(document, result, derived, repo=None):
    blockers = []
    for error in (derived.get("relationship_mapping") or {}).get("errors", []):
        blockers.append({"kind": "relationship_graph", "detail": error,
                         "resolution_needed": "Correct the connection graph and its evidence dependencies, then recollect."})
    for item in field_review.gap_resolution.agent_blockers(field_review.gap_partition(document, derived, repo)):
        blockers.append({"kind": item.get("kind"), "detail": item,
                         "resolution_needed": item.get("resolution_requirement", "Investigate and record a current evidence-bound gap disposition.")})
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


def scan_progress(document, result, derived, errors, blockers, repo=None):
    contract = document.get("classification_review", {})
    pending = [row for row in field_review.all_rows(contract.get("fields", []))
               if row.get("status") != "proposed"]
    human = [row for row in pending if row.get("resolution_owner") == "human"]
    agent = [row for row in pending if row.get("resolution_owner") != "human"]
    partition = field_review.gap_partition(document, derived, repo)
    gaps = field_review.gap_resolution.agent_blockers(partition)
    questions = []
    for row in human + document.get("removal_proposals", []):
        for item in [row] + row.get("processing_activities", []):
            question_text = item.get("business_context_question") or item.get("unresolved_question")
            if question_text:
                questions.append(question_text)
    accounted_processing = {(gap.get("scope", {}).get("system"), gap.get("scope", {}).get("activity"))
                            for gap in partition["resolved_diagnostics"] if gap.get("kind") == "processing_uncertainty"}
    for system in document.get("system_proposals", []):
        for item in [system] + system.get("processing_activities", []):
            question = item.get("business_context_question") or item.get("unresolved_question")
            if question and (system.get("entity_id"), item.get("activity_id")) not in accounted_processing:
                questions.append(question)
    technical_questions = []
    technical_questions.extend(gap.get("message", "Contract unavailable") for gap in gaps)
    questions = list(dict.fromkeys(questions))
    incomplete = bool(agent or gaps or errors or blockers or technical_questions or document.get("discovery_required"))
    accepted = bool(result.get("accepted_current") and not document.get("relationship_proposal")
                    and not incomplete and not questions and not human)
    actions = []
    if incomplete:
        actions.append("Agent: investigate source and framework evidence, attempt supplemental structures and relationships, and record gap resolutions; report the exact missing evidence, investigation performed, next evidence-gathering action and map impact. The agent retains investigation ownership through extractor limitations. A reviewer may explicitly accept documented uncertainty; this does not establish missing facts.")
    if questions or human:
        actions.append("User: answer the specific missing-fact questions below; supported proposals remain available for review.")
    if not accepted and not incomplete and not questions and not human:
        actions.append("User: Accept or amend proposals; name the accountable owner.")
    accepted_uncertainties = [uncertainty.field_entry(row) for row in field_review.all_rows(contract.get("fields", [])) if row.get("accepted_uncertainty")]
    accepted_uncertainties += [entry for gap in partition["resolved_diagnostics"] if (entry := uncertainty.gap_entry(gap))]
    return {"accepted_uncertainties": accepted_uncertainties, "accepted_uncertainty_count": len(accepted_uncertainties), "collection": "complete", "agent_investigation": "incomplete" if incomplete else "complete",
            "agent_pending_fields": len(agent), "extraction_gaps": len(gaps),
            "source_information": "missing" if questions or human else "no open questions",
            "missing_facts": questions, "technical_questions": list(dict.fromkeys(technical_questions)),
            "human_approval": "recorded" if accepted else "deferred" if incomplete or questions or human else "pending",
            "publication": "eligible for validation" if accepted else "blocked",
            "next_actions": actions}


def member_coverage(row):
    """Describe member coverage without copying child categories onto their container."""
    if not row.get("composite"):
        return None
    members = list(field_review.all_rows(row.get("nested_members", [])))
    def content_pending(item):
        resolution = item.get("open_content_resolution") or {}
        return item.get("open_content") and not item.get("accepted_uncertainty") and resolution.get("outcome") not in field_review.CONTENT_OUTCOMES
    pending = [child["entity_id"] for child in members
               if child.get("status") != "proposed" or (child.get("personal_data") == "unknown" and not child.get("accepted_uncertainty")) or content_pending(child)
               or (child.get("personal_data") in {"yes", "mixed"}
                   and not child.get("composite") and not child.get("categories") and not child.get("accepted_uncertainty"))]
    complete = ((bool(members) or row.get("accepted_uncertainty") or (row.get("open_content_resolution") or {}).get("outcome") in {"reviewed_open_content", "accepted_certificate"}) and not pending and row.get("status") == "proposed"
                and not content_pending(row))
    return {"state": "reviewed members" if complete else "member review incomplete",
            "categories": sorted({c for child in members for c in child.get("categories", [])}),
            "pending_members": pending, "member_count": len(members)}


def build_outputs(document, result, derived, candidate, errors, repo=None):
    blockers = structural_blockers(document, result, derived, repo)
    status = "enrichment incomplete" if errors or blockers else "accepted" if result.get("accepted_current") and not document.get("relationship_proposal") else "ready for human review"
    proposals = {row["entity_id"]: effective_proposal(row, document) for row in document.get("proposals", [])}
    _, _, candidate_fields, _ = workflow.manifest_indexes(candidate)
    actions = {row["entity_id"]: row["action"] for row in result["actions"]}
    inventory = []
    canonical = document.get("classification_review", {})
    top_observed = {row["entity_id"]: row for row in workflow.field_rows(derived)}
    def observed_classifications(rows, parent_observed=None):
        # Literal field names can contain dots. Follow canonical parents, retaining
        # exact observed-member metadata before inheriting the nearest ancestor.
        for row in rows:
            observed = top_observed.get(row["entity_id"], parent_observed)
            if observed is None:
                raise ValueError(f"{row['entity_id']}: canonical field has no matching observed field for review")
            yield row, observed
            yield from observed_classifications(row.get("nested_members", []), observed)

    for row, observed in observed_classifications(canonical.get("fields", [])):
        identity = row["entity_id"]
        historical = proposals.get(identity, {})
        compatible = sorted(historical.get("proposed_categories", [])) == sorted(row["categories"])
        presentation = historical if compatible else {}
        decision_id = row.get("decision_id") or presentation.get("decision_id") or identity
        group_decision = document.get("decisions", {}).get(decision_id, {})
        summary = row.get("decision_summary") or group_decision.get("decision_summary") or presentation.get("decision_summary") or row["meaning"].split("\n", 1)[0][:240]
        proposal = {"proposal_kind": "non_personal" if row["personal_data"] == "no" else "personal" if row["personal_data"] in {"yes", "mixed"} else "ambiguous",
                    "proposed_categories": row["categories"], "decision_id": decision_id, "decision_summary": summary,
                    "unresolved_question": "" if row.get("accepted_uncertainty") else row.get("business_context_question") or (row.get("uncertainty") if row.get("resolution_owner") != "human" else ""),
                    "business_context_question": row.get("business_context_question"), "resolution_needed": row.get("resolution_needed"),
                    "decision_impact": row.get("decision_impact"), "resolution_owner": row.get("resolution_owner"),
                    "rationale": row["rationale"], "refs": row["evidence"]}
        inventory.append({**observed, "entity_id": identity, "classification": row["categories"], "personal_data": row["personal_data"],
                          "decision_state": row["status"], "proposal": proposal, "candidate_decision": candidate_fields.get(identity, {}),
                          "canonical_decision": row, "member_coverage": member_coverage(row)})
    activities = []
    proposed_systems = {row["entity_id"] for row in document.get("system_proposals", [])}
    for system in document.get("system_proposals", []):
        activities.extend({**activity, "system": system["entity_id"], "system_type": system.get("system_type"), "decision_state": "proposed"}
                          for activity in system.get("processing_activities", []))
    for system in candidate.get("system", []):
        if system["fides_key"] not in proposed_systems:
            for declaration in system.get("privacy_declarations", []):
                activities.append({"system": system["fides_key"], "purpose": declaration.get("name"),
                    "proposed_data_uses": [declaration.get("data_use")],
                    "proposed_data_subjects": declaration.get("data_subjects", []),
                    "proposed_categories": declaration.get("data_categories", []),
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
    group_questions = {}
    technical = {}
    for field in inventory:
        proposal = field["proposal"]
        if not proposal or proposal.get("resolution_owner") == "agent":
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
        questions = group_questions.setdefault(key, {name: [] for name in
            ("unresolved_question", "business_context_question", "resolution_needed", "decision_impact")})
        for name, values in questions.items():
            value = proposal.get(name)
            if isinstance(value, str) and value.strip() and value not in values:
                values.append(value)
            group[name] = "\n".join(values)
        group["field_ids"].append(field["entity_id"])
        group["collections"] = sorted(set(group["collections"]) | {collection})
        group["proposal_kinds"] = sorted(set(group["proposal_kinds"]) | {proposal.get("proposal_kind") or "requested"})
        group["data_categories"] = sorted(set(group["data_categories"]) | set(field["classification"]))
    progress = scan_progress(document, result, derived, errors, blockers, repo)
    if progress["agent_investigation"] == "incomplete":
        status = "enrichment incomplete"
    return {"version": "1.0.0", "status": status,
            "progress": progress,
            "observation_digest": result["observation_digest"],
            "collection_mode": "candidate" if result.get("candidate_context") else "manifest",
            "data_map": {"dotnet": {"diagnostic_summary": derived.get("dotnet", {}).get("diagnostic_summary", {}), **{key: derived.get("dotnet", {}).get(key, []) for key in ("projects", "solutions", "technologies", "relationships", "runtime_bindings", "entity_relationships", "gaps")}}, "system_flows": [{"system": s["fides_key"], "direction": k, "resources": s[k]} for s in candidate.get("system", []) for k in ("ingress", "egress") if s.get(k)], "stores": stores, "processing_activities": activities, "monitoring_scope": result.get("monitoring_scope", {}), "connection_questions": (derived.get("relationship_mapping") or {}).get("questions", []), "relationship_proposal": document.get("relationship_proposal")},
            "review_queue": {"structural_blockers": blockers, "analysis_errors": errors,
                             "field_groups": [] if blockers else [groups[key] for key in sorted(groups)],
                             "technical_coverage": technical,
                             "processing_decisions": [] if blockers else [row for row in activities if row["decision_state"] == "proposed"],
                             "changes": result["actions"], "collection_decisions": result["collection_review_required"],
                             "dependency_decisions": document.get("dependency_proposals", []),
                             "control_changes": result.get("control_changes", []), "system_changes": result.get("system_changes", [])},
            "evidence_record": {"extraction_diagnostics": field_review.gap_partition(document, derived, repo), "fields": inventory, "reasoning_groups": document.get("reasoning_groups", {}),
                                "agent_analysis": {k: v for k, v in document.items() if k not in {"evidence", "preview", "candidate", "reconciliation"}}, "reconciliation": result, "candidate_manifest": candidate, "coverage": derived.get("coverage", {}),
                                "special_category_refs": result.get("special_category_refs", []), "relationship_mapping": derived.get("relationship_mapping", {})}}


def question(lines, row):
    unknowns = list(dict.fromkeys(line for value in (row.get("unresolved_question"), row.get("business_context_question"))
                                 if value for line in value.splitlines() if line.strip()))
    for unknown in unknowns:
        lines.append(f"  Unknown: {unknown}")
    if unknowns:
        proposal = row.get("decision_summary") or row.get("summary")
        if proposal and proposal != "Analysis required":
            lines.append(f"  Current proposal (not an answer or approval): {proposal}")
        for resolution in (row.get("resolution_needed") or "Specify the evidence or answer required.").splitlines():
            lines.append(f"  To resolve: {resolution}")
        for impact in (row.get("decision_impact") or "").splitlines():
            lines.append(f"  Affects: {impact}")


def review_scope(values):
    values = sorted(set(values))
    if len(values) <= 3:
        return ", ".join(values)
    return f"{', '.join(values[:3])} and {len(values) - 3} other collections (full scope in evidence)"


def render_outputs(outputs):
    status = outputs["status"]
    data_map = ["# Privacy review", "", f"Status: **{status}**." + (" Proposed meanings require human acceptance." if status != "accepted" else " No changes within the monitored scope."), ""]
    progress = outputs.get("progress")
    if progress:
        data_map[2] = f"Collection complete; {status}."
        for key, label in (("agent_investigation", "Agent investigation"), ("source_information", "Source information"),
                           ("human_approval", "Human approval"), ("publication", "Publication")):
            data_map.append(f"- {label}: **{progress[key]}**.")
        data_map.extend(["", "Approval pending is not scan failure.", ""])
        data_map.extend(f"- {action}" for action in progress["next_actions"])
        data_map.extend(f"- Agent investigation: {question}" for question in progress.get("technical_questions", []))
        data_map.append("")
    if outputs["evidence_record"]["reconciliation"].get("candidate_context"):
        data_map.extend(["Candidate collection: stores and fields below use the proposed relationship graph. Accepted files are unchanged.", ""])
    if status == "enrichment incomplete":
        data_map.extend(["Investigation is unfinished. Supported privacy proposals follow; remaining work is summarized below.", ""])
    data_map.extend(["Validation checks consistency, not semantic truth; acceptance cannot close gaps.", ""])
    for store in outputs["data_map"]["stores"]:
        data_map.extend([f"## {store['name']}", "", f"Scope: {len(store['collections'])} collections",
                         f"Categories: {', '.join(store['data_categories']) or 'None established'}", ""])
    dotnet = outputs["data_map"].get("dotnet", {})
    if dotnet.get("projects") or dotnet.get("technologies") or dotnet.get("relationships"):
        data_map.extend(["## .NET discovery and modeled relationships", "",
                         "Dependencies and source signals are evidence for investigation. Modeled references do not establish physical database identity or deployed storage.", ""])
        diagnostic_counts = dotnet.get("diagnostic_summary", {})
        if diagnostic_counts:
            data_map.append(f"Extraction diagnostics: {diagnostic_counts.get('diagnostics', 0)} grouped items from {diagnostic_counts.get('occurrences', 0)} source occurrences. These are separate from field-classification and approval work.")
            for category, count in diagnostic_counts.get("by_category", {}).items():
                data_map.append(f"- {category}: {count}")
        for diagnostic in dotnet.get("gaps", []):
            data_map.append(f"- {diagnostic.get('diagnostic_id', diagnostic['ref'])} [{diagnostic.get('category', 'unsupported_semantics')}]: {diagnostic['reason']} Resolution: {diagnostic.get('resolution_requirement', 'Investigate the cited source.')} Evidence: {', '.join(o['ref'] for o in diagnostic.get('occurrences', [diagnostic]))}")
        for item in dotnet.get("technologies", []):
            data_map.append(f"- {item['technology']} ({item['evidence_kind']}): {item['ref']}")
        for edge in dotnet.get("relationships", []):
            data_map.append(f"- {edge['model']} → {edge['dataset']}/{edge['collection']} ({edge['kind']}; physical identity unverified). Evidence: {', '.join(edge['refs'])}")
        data_map.append("")
    containers = [row for row in outputs["evidence_record"]["fields"] if row.get("member_coverage")]
    if containers:
        data_map.extend(["## Nested field coverage", "",
                         "Containers keep categories on their members; an empty parent category list does not mean uncategorized content.", ""])
        for row in containers:
            coverage = row["member_coverage"]
            data_map.append(f"- **{row['entity_id']}**: {coverage['state']} ({coverage['member_count']} members). "
                            f"Member categories: {', '.join(coverage['categories']) or 'None established'}.")
            if coverage["pending_members"]:
                data_map.append("  Pending members: " + ", ".join(coverage["pending_members"]))
        data_map.append("")
    data_map.extend(["## Processing and flows", ""])
    for activity in outputs["data_map"]["processing_activities"]:
        data_map.extend([f"- **{activity['system']}: {activity.get('purpose')}** ({activity['decision_state']})",
                         f"  Categories: {', '.join(activity.get('proposed_categories', [])) or 'Unresolved'}",
                         f"  Subjects: {', '.join(activity.get('proposed_data_subjects', [])) or 'Unresolved'}; uses: {', '.join(activity.get('proposed_data_uses', []))}",
                         f"  Stores: {', '.join(activity.get('dataset_references', [])) or ('Transient processing' if activity.get('processing_mode') == 'transient' else 'Unresolved datastore')}; flow: {activity.get('relationship_rationale') or 'See accepted declaration evidence'}"])
        data_map.append(f"  Mode: {activity.get('processing_mode') or 'not recorded'}; recipients: {activity.get('recipient_rationale') or 'not recorded'}")
        if activity.get("recipient_systems"):
            data_map.append(f"  Recipients: {', '.join(activity['recipient_systems'])}")
    if outputs["data_map"].get("system_flows"):
        data_map.extend(["", "## External and internal system flows", ""])
        for flow in outputs["data_map"]["system_flows"]:
            for resource in flow["resources"]:
                source, target = (flow["system"], resource["fides_key"]) if flow["direction"] == "egress" else (resource["fides_key"], flow["system"])
                data_map.append(f"- {source} → {target}; categories: {', '.join(resource.get('data_categories', [])) or 'Unresolved'}")
    for row in outputs["data_map"].get("connection_questions", []):
        data_map.append(f"- Connection **{row['id']}**: destination unresolved.")
    queue = outputs["review_queue"]
    discovery = outputs["evidence_record"]["agent_analysis"].get("discovery_required", [])
    review = ["# Privacy decisions", "",
              "[Analysis and evidence](privacy-datamap.analysis.json)", ""]
    feedback_rows = [item for item in outputs["evidence_record"]["fields"] if item.get("canonical_decision", {}).get("user_feedback")]
    if feedback_rows:
        review.extend(["## Recorded user corrections", ""])
    for item in feedback_rows:
        review.append(f"- **{item['entity_id']}** ({item['decision_state']})")
        for feedback in item["canonical_decision"]["user_feedback"]:
            review.extend([f"  User: {feedback['statement']}", f"  Classification interpretation: {feedback['interpretation']}"])
    composite_rows = [item["canonical_decision"] for item in outputs["evidence_record"]["fields"]
                      if item.get("canonical_decision", {}).get("composite") or item.get("canonical_decision", {}).get("open_content")]
    if composite_rows:
        review.extend(["## JSON and nested content", ""])
    for row in composite_rows:
        children = row.get("nested_members", [])
        known = ", ".join(child["name"] for child in children) or "none extracted"
        review.append(f"- **{row['entity_id']}** — known members: {known}.")
        review.append("  Additional content: " + ("open or unresolved; known members remain separately classified." if row.get("open_content") else "closed in the recorded shape evidence."))
        if row.get("status") != "proposed":
            review.append("  Blocker: " + ("missing source/business contract; answer the recorded fact question." if row.get("resolution_owner") == "human"
                          else "agent investigation or extraction limitation; inspect the contract and writers before asking the user."))
        if row.get("open_content_resolution"):
            review.append(f"  Content assessment: {row['open_content_resolution']}")
    for gap in outputs["evidence_record"]["agent_analysis"].get("classification_review", {}).get("gaps", []):
        if gap.get("status") != "resolved":
            review.append(f"- Extraction limitation **{gap.get('subject') or gap.get('kind')}**: {gap.get('message', 'Inspect extraction evidence')}")
    for entry in outputs.get("progress", {}).get("accepted_uncertainties", []):
        review.append("- " + entry["target_id"] + ": " + uncertainty.disclosure(entry))
    for change in outputs["evidence_record"]["reconciliation"].get("semantic_changes", []):
        if change["kind"] != "personal_to_non_personal_or_removed":
            review.append(f"- **{change['kind'].replace('_', ' ')}**: {change['entity_id'].removeprefix('semantic:')}")
    for removal in outputs["evidence_record"]["agent_analysis"].get("removal_proposals", []):
        review.append(f"- **{removal.get('decision_summary') or removal['entity_id']}**: {removal.get('outcome') or 'Investigation required'}")
        question(review, removal)
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
        meaningful_groups = [g for g in queue["field_groups"] if g["summary"] != "Analysis required"]
        if meaningful_groups or queue["collection_decisions"]:
            review.extend(["## Changes requiring decisions", ""])
        for group in meaningful_groups:
            review.extend([f"- **{group['summary']}**",
                           f"  Scope: {review_scope(group['collections'])} ({', '.join(group['proposal_kinds'])})."])
            if group["data_categories"]:
                review.append(f"  Categories: {', '.join(group['data_categories'])}")
            question(review, group)
        if not progress and (queue["collection_decisions"] or queue["processing_decisions"]):
            review.extend(["", "Accept or amend the proposals and record the accountable owner once for the affected scope."])
        if queue["collection_decisions"]:
            review.append(f"Acceptance scope: {review_scope(queue['collection_decisions'])}.")
        if technical:
            review.extend(["", f"Technical coverage: {sum(technical.values())} proposed non-personal fields across {len(technical)} collections; details remain in evidence."])
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
                review.append(f"- **{change['id']}**: {change['reason']} Review the affected processing and sharing relationships.")
        if queue["processing_decisions"]:
            review.extend(["", "## Systems and processing decisions", ""])
        for activity in queue["processing_decisions"]:
            review.append(f"- **{activity['system']}: {activity.get('decision_summary') or 'Analysis required'}**")
            question(review, activity)
        findings = outputs["evidence_record"]["agent_analysis"].get("store_investigation", {}).get("findings", [])
        gaps = outputs["evidence_record"].get("extraction_diagnostics", {}).get("active_blockers", [])
        active_stores = {gap.get("scope", {}).get("store") for gap in gaps if gap.get("kind") == "enrichment_coverage"}
        if gaps:
            review.extend(["", "## Coverage and identity questions", ""])
        for finding in findings:
            if finding.get("status") == "gap" and finding.get("store") in active_stores:
                review.append(f"- {finding.get('store')}: {finding.get('decision_summary') or 'Coverage unresolved'}")
                question(review, finding)
        for gap in gaps:
            review.append(f"- Coverage gap at {gap.get('ref', 'unknown source')}: {gap.get('reason', 'structure not established')}. To resolve: establish the stored payload and its privacy meaning.")
    special_keys = workflow.VALIDATOR.load_vocabulary()["special_categories"]
    special = {}
    for row in outputs["evidence_record"]["fields"]:
        categories = {key for key in row["classification"] if key in special_keys}
        if categories or (row["proposal"] or {}).get("proposal_kind") == "special_category":
            collection = row["entity_id"].rsplit("/", 1)[0]
            item = special.setdefault(collection, {"count": 0, "categories": set()})
            item["count"] += 1
            item["categories"].update(categories)
    review.extend(["", "## Possible Article 9 or Article 10 data", ""])
    review.extend([f"- {collection}: {', '.join(sorted(item['categories'])) or 'classification unresolved'} ({item['count']} fields; citations in evidence)."
                   for collection, item in sorted(special.items())] or ["None identified."])
    if discovery:
        review.extend(["", "## New scope to investigate", "",
                       f"{len(discovery)} code/configuration changes need investigation for new data, purposes or sharing. Source-level work items remain in evidence."])
    if queue["analysis_errors"]:
        review.extend(["", "## Outstanding analysis", "",
                       f"{len(queue['analysis_errors'])} validation issues remain. The agent must resolve missing analysis and invalid proposals before requesting acceptance."])
        # Report investigation scope, not one diagnostic per field. Full errors remain available
        # through the machine-readable result and explicit evidence view.
        pending = sum(len(g["field_ids"]) for g in queue["field_groups"] if g["summary"] == "Analysis required")
        if pending:
            review.append(f"{pending} fields still lack a supported privacy decision; this is agent investigation work.")
    return "\n".join(data_map) + "\n", "\n".join(review) + "\n"


def main(argv):
    try:
        opts = workflow.parse_args(argv)
        if opts["seal"]:
            raise ValueError("review.py cannot seal decisions")
        if opts.get("help"):
            print("usage: review.py --repo=<path> [--candidate] [--output=json|evidence]")
            return 0
        repo = opts["repo"]
        cache = workflow.cache_directory(repo, opts["candidate"])
        derived, scan = workflow.load_observations(repo, opts["candidate"])
        path = repo / ".noru" / "privacy-datamap.yml"
        manifest = workflow.load_manifest(path)
        lock = workflow.load_json(repo / ".noru" / "privacy-datamap.lock.json")
        mapping = workflow.validate_scan_mode(repo, derived, scan, opts["candidate"])
        result = workflow.reconcile(derived, scan, manifest, lock, path)
        result["status"] = "structure collected"
        candidate = workflow.build_candidate(derived, scan, {} if result["mode"] == "bootstrap" else manifest,
                    workflow.build_lock(derived, scan, path) if result["mode"] == "migration" else lock)
        candidate = workflow.apply_candidate_mapping(workflow.refresh_candidate_evidence(candidate, result), mapping)
        document = analysis_storage.load(cache / "privacy-datamap.analysis.json")
        if document is None:
            raise ValueError("Run reconciliation before enrichment")
        errors = validate(document, result, candidate, repo)
        outputs = build_outputs(document, result, derived, candidate, errors, repo)
        data_map, report = render_outputs(outputs)
        report = data_map + "\n" + report
        analysis_storage.save(cache / "privacy-datamap.analysis.json", document)
        if opts.get("evidence"):
            print(json.dumps(outputs))
            return 1 if outputs["status"] == "enrichment incomplete" else 0
        (cache / "privacy-datamap.review.md").write_text(report, encoding="utf-8")
        output = {"status": outputs["status"], "progress": outputs["progress"], "errors": errors,
                  "structural_blockers": outputs["review_queue"]["structural_blockers"]}
        print(json.dumps(output) if opts["json"] else report)
        return 1 if outputs["status"] == "enrichment incomplete" else 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        print(f"error: invalid enrichment input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
