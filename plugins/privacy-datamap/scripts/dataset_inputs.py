"""Read evidenced Fides dataset/system inputs without treating declarations as acceptance."""
import copy
import json
import analysis_storage
import pathlib
import re
import sys

import dependencies

MANIFEST = ".noru/privacy-datamap.yml"


def metadata(row):
    return (row.get("meta") or {}).get("noru", {})


def read(repo, candidate=False):
    from validate_manifest import load_yaml
    path = repo / MANIFEST
    document = load_yaml(path.read_text())[0] if path.is_file() else {}
    document = document or {}
    proposed = {}
    if candidate:
        cache = repo / ".noru/.cache/privacy-datamap.analysis.json"
        proposed = (analysis_storage.load(cache) or {}).get("structural_proposal", {})
    if not isinstance(document, dict) or not isinstance(proposed, dict) or set(proposed) - {"dataset", "system", "evidence_dependencies"}:
        raise ValueError("Structural proposal supports dataset, system and evidence_dependencies only")
    result = copy.deepcopy(document)
    for kind in ("dataset", "system"):
        existing = result.get(kind, [])
        rows = {row["fides_key"]: row for row in existing}
        if len(rows) != len(existing):
            raise ValueError(f"duplicate {kind} keys in manifest")
        additions = proposed.get(kind, [])
        if len({row["fides_key"] for row in additions}) != len(additions):
            raise ValueError(f"duplicate proposed {kind} keys")
        rows.update({row["fides_key"]: row for row in additions})
        result[kind] = list(rows.values())
    specs = {s["id"]: s for s in result.get("evidence_dependencies", [])}
    specs.update({s["id"]: s for s in proposed.get("evidence_dependencies", [])})
    result["evidence_dependencies"] = list(specs.values())
    return result


def dependency_gaps(doc):
    errors = []
    def require_dependencies(target, refs, alternate=None):
        for ref in refs or []:
            path = ref.rsplit(":", 1)[0]
            if not any(spec.get("path") == path and (target in spec.get("targets", []) or alternate in spec.get("targets", [])) for spec in doc.get("evidence_dependencies", [])):
                errors.append(f"{target}: register evidence_dependencies for every structural input citation before acceptance")
    for dataset in doc.get("dataset", []):
        if (dataset.get("meta") or {}).get("noru", {}).get("origin") == "supplemental":
            for collection in dataset.get("collections", []):
                base = dataset["fides_key"] + "/" + collection["name"]
                require_dependencies(base, dataset["meta"]["noru"].get("refs", []) + collection.get("refs", []))
                def check_inputs(fields, prefix=""):
                    for field in fields:
                        require_dependencies(base + "/" + prefix + field["name"], field.get("refs"), base)
                        check_inputs(field.get("fields", []), prefix + field["name"] + ".")
                check_inputs(collection.get("fields", []))
    for system in doc.get("system", []):
        meta = (system.get("meta") or {}).get("noru", {})
        if meta.get("origin") == "external" or system.get("ingress") or system.get("egress"):
            if not meta.get("refs"):
                errors.append(f"{system['fides_key']}: external systems and flows require meta.noru.refs")
            require_dependencies("system:" + system["fides_key"], meta.get("refs"))
    return errors


def extract(repo, candidate=False):
    document = read(repo, candidate)
    if candidate:
        for spec in document.get("evidence_dependencies", []):
            if dependencies.observe(repo, spec)["fingerprint"] != spec.get("fingerprint"):
                raise ValueError("Candidate structural evidence changed; investigate and refresh the proposal")
    known = set(dependencies.files(repo))

    def refs(values):
        if not isinstance(values, list) or not values:
            raise ValueError("Supplemental datasets and external systems require source citations")
        for value in values:
            match = re.fullmatch(r"(.+):(\d+)", value)
            if not match or match[1] not in known or match[1].startswith((".noru/", ".fides/")):
                raise ValueError("Input must cite scanned repository evidence, not the manifest or cache")
            path = (repo / match[1]).resolve()
            if not path.is_relative_to(repo.resolve()):
                raise ValueError("Input citation escapes repository")
            if not 0 < int(match[2]) <= len(path.read_text().splitlines()):
                raise ValueError(f"Input cites line {match[2]} outside {match[1]}")
        return values

    def fields(items, prefix=""):
        output = []
        for field in items:
            meta = metadata(field)
            if not meta.get("shape") or meta.get("evidence_kind") not in {"typed_contract", "serializer", "upload_payload", "download_result"}:
                raise ValueError("Supplemental fields require meta.noru.shape and evidence_kind")
            name = prefix + field["name"]
            output.append({"name": name, "shape": meta["shape"], "evidence_kind": meta["evidence_kind"], "refs": refs(field.get("refs"))})
            output.extend(fields(field.get("fields", []), name + "."))
        return output

    datasets = []
    for dataset in document.get("dataset", []):
        if metadata(dataset).get("origin") not in (None, "supplemental"):
            raise ValueError("Dataset meta.noru.origin must be supplemental")
        if metadata(dataset).get("origin") != "supplemental":
            continue
        collections = [{"name": c["name"], "refs": refs(c.get("refs")), "fields": fields(c.get("fields", []))} for c in dataset.get("collections", [])]
        if len({c["name"] for c in collections}) != len(collections) or any(len({f["name"] for f in c["fields"]}) != len(c["fields"]) for c in collections):
            raise ValueError("Duplicate supplemental collection or field identities")
        if not collections or any(not c["fields"] for c in collections):
            raise ValueError("Supplemental datasets require collections and evidenced fields")
        datasets.append({"fides_key": dataset["fides_key"], "name": dataset["name"], "refs": refs(metadata(dataset).get("refs")),
                         "collections": collections, "definition": dataset,
                         "system_references": [s["fides_key"] for s in document.get("system", []) if dataset["fides_key"] in s.get("dataset_references", [])]})
    external = []
    for system in document.get("system", []):
        if metadata(system).get("origin") == "external":
            external.append({**system, "refs": refs(metadata(system).get("refs"))})
    for system in document.get("system", []):
        if system.get("ingress") or system.get("egress"):
            refs(metadata(system).get("refs"))
    return {"datasets": datasets, "external_systems": external, "systems": document.get("system", []), "evidence_dependencies": document.get("evidence_dependencies", [])}



if __name__ == "__main__":
    try:
        root = pathlib.Path(next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--repo="))).resolve()
        print(json.dumps(extract(root, "--candidate" in sys.argv)))
    except (ValueError, OSError, KeyError, TypeError, StopIteration) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
