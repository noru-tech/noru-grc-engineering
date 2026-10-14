"""Evidence-bound human disposition of uncertainty, separate from resolved facts."""
import copy
import datetime

def template(scope):
    return {"scope": scope, "accepted_by": "", "accepted_at": "", "rationale": "",
            "residual_uncertainty": "", "map_impact": "", "evidence_ids": []}

def errors(record, scope, cited):
    if not isinstance(record, dict):
        return ["accepted uncertainty requires a scoped human acceptance record"]
    result = []
    for key in ("scope", "accepted_by", "accepted_at", "rationale", "residual_uncertainty", "map_impact"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            result.append("accepted uncertainty requires " + key)
    if record.get("scope") != scope:
        result.append("accepted uncertainty scope differs from the finding")
    try:
        if not isinstance(record.get("accepted_at"), str):
            raise ValueError()
        stamp = datetime.datetime.fromisoformat(record["accepted_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp > datetime.datetime.now(datetime.timezone.utc):
            raise ValueError()
    except (TypeError, ValueError):
        result.append("accepted_at must be a past or current timestamp with timezone")
    ids = record.get("evidence_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids) or not set(ids) <= set(cited):
        result.append("accepted uncertainty requires current cited investigation evidence")
    return result

def field_entry(row):
    record = row.get("accepted_uncertainty")
    if not record:
        return None
    return {"kind": "field", "target_id": row["entity_id"], "personal_data": row["personal_data"],
            "acceptance": copy.deepcopy(record)}

def gap_entry(gap):
    decision = gap.get("resolution", {})
    if decision.get("outcome") not in {"accepted_uncertainty", "accepted_certificate"} or not decision.get("accepted_uncertainty"):
        return None
    return {"kind": "extraction_gap", "target_id": gap["gap_id"],
            "acceptance": copy.deepcopy(decision["accepted_uncertainty"]),
            "covered_occurrence_ids": copy.deepcopy(decision["covered_occurrence_ids"])}

def disclosure(entry):
    record = entry["acceptance"]
    return "Accepted uncertainty: " + record["residual_uncertainty"] + " Map impact: " + record["map_impact"]
