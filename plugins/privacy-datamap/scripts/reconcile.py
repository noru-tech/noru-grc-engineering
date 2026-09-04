#!/usr/bin/env python3
"""Reconcile current privacy observations with the last accepted local baseline.

This script is deterministic, offline and model-free. It decides which existing classifications
can be carried forward and writes the bounded queue an agent may analyse. The agent never decides
whether it should run.

Usage:
    python3 reconcile.py --repo=<path> [--seal] [--output=json] [--quiet]

Default mode writes only cache artifacts. --seal writes the committed lock, but only for a valid
manifest whose derived_digest matches the current scan.
"""

import hashlib
import importlib.util
import json
import pathlib
import re
import sys


PIECE = "privacy-datamap"
VERSION = "1.0.0"
HERE = pathlib.Path(__file__).resolve().parent
PLUGIN = HERE.parent
PLAIN_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 _./@-]*$")


def load_validator():
    path = HERE / "validate_manifest.py"
    spec = importlib.util.spec_from_file_location("privacy_datamap_validator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_validator()


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_json(value):
    return sha256_bytes(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )


def manifest_sha(path):
    return sha256_bytes(path.read_bytes())


def taxonomy_digest():
    paths = [
        PLUGIN / "references" / "classification.json",
        PLUGIN / "references" / "taxonomy" / "data_categories.json",
        PLUGIN / "references" / "taxonomy" / "data_uses.json",
        PLUGIN / "references" / "taxonomy" / "data_subjects.json",
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def yaml_scalar(value):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if (
        text
        and PLAIN_SAFE.fullmatch(text)
        and text.lower() not in {"true", "false", "null", "yes", "no", "on", "off"}
    ):
        return text
    return json.dumps(text, ensure_ascii=False)


def to_yaml(value, indent=0):
    pad = " " * indent
    if isinstance(value, list):
        if not value:
            return f"{pad}[]\n"
        rows = []
        for item in value:
            rendered = to_yaml(item, indent + 2)
            rows.append(f"{pad}- {rendered[indent + 2:]}")
        return "".join(rows)
    if isinstance(value, dict):
        if not value:
            return f"{pad}{{}}\n"
        rows = []
        for key, child in value.items():
            if isinstance(child, list):
                rows.append(
                    f"{pad}{key}: []\n" if not child else f"{pad}{key}:\n{to_yaml(child, indent + 2)}"
                )
            elif isinstance(child, dict):
                rows.append(
                    f"{pad}{key}: {{}}\n" if not child else f"{pad}{key}:\n{to_yaml(child, indent + 2)}"
                )
            else:
                rows.append(f"{pad}{key}: {yaml_scalar(child)}\n")
        return "".join(rows)
    return f"{pad}{yaml_scalar(value)}\n"


def load_json(path, required=False):
    if not path.is_file():
        if required:
            raise ValueError(f"missing {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc


def load_manifest(path):
    if not path.is_file():
        return None
    try:
        document, _loader = VALIDATOR.load_yaml(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not parse {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{path} is not a YAML mapping")
    return document


def field_rows(derived):
    rows = []
    for dataset in derived.get("datasets") or []:
        dataset_key = dataset.get("fides_key")
        for collection in dataset.get("collections") or []:
            collection_id = collection.get("entity_id") or f"{dataset_key}/{collection.get('name')}"
            for field in collection.get("fields") or []:
                entity_id = field.get("entity_id") or f"{collection_id}/{field.get('name')}"
                rows.append(
                    {
                        "entity_id": entity_id,
                        "collection_id": collection_id,
                        "dataset_key": dataset_key,
                        "dataset_name": dataset.get("name"),
                        "collection": collection.get("name"),
                        "field": field.get("name"),
                        "shape": field.get("shape", ""),
                        "semantic_digest": field.get("semantic_digest")
                        or sha256_json(
                            {
                                "dataset": dataset_key,
                                "collection": collection.get("name"),
                                "field": field.get("name"),
                                "shape": field.get("shape", ""),
                            }
                        ),
                        "refs": [field.get("ref")] if field.get("ref") else [],
                        "data_categories": list(field.get("data_categories") or []),
                        "needs_review": field.get("needs_review") is True,
                    }
                )
    return sorted(rows, key=lambda row: row["entity_id"])


def collection_rows(derived):
    rows = []
    for dataset in derived.get("datasets") or []:
        dataset_key = dataset.get("fides_key")
        for collection in dataset.get("collections") or []:
            identity = collection.get("entity_id") or f"{dataset_key}/{collection.get('name')}"
            rows.append(
                {
                    "entity_id": identity,
                    "dataset_key": dataset_key,
                    "collection": collection.get("name"),
                    "semantic_digest": collection.get("semantic_digest")
                    or sha256_json(
                        sorted(
                            [
                                {"name": field.get("name"), "shape": field.get("shape", "")}
                                for field in collection.get("fields") or []
                            ],
                            key=lambda row: row["name"],
                        )
                    ),
                    "refs": [collection.get("ref")] if collection.get("ref") else [],
                }
            )
    return sorted(rows, key=lambda row: row["entity_id"])


def manifest_indexes(manifest):
    datasets = {}
    collections = {}
    fields = {}
    systems = {}
    if not manifest:
        return datasets, collections, fields, systems
    for dataset in manifest.get("dataset") or []:
        key = dataset.get("fides_key")
        datasets[key] = dataset
        for collection in dataset.get("collections") or []:
            collection_id = f"{key}/{collection.get('name')}"
            collections[collection_id] = collection
            for field in collection.get("fields") or []:
                fields[f"{collection_id}/{field.get('name')}"] = field
    for system in manifest.get("system") or []:
        systems[system.get("fides_key")] = system
    return datasets, collections, fields, systems


def manifest_matches_observation(manifest, derived):
    """The source digest is a freshness hint, not permission to trust a hand-edited stamp."""
    _datasets, manifest_collections, manifest_fields, manifest_systems = manifest_indexes(manifest)
    observed_fields = {row["entity_id"] for row in field_rows(derived)}
    observed_collections = {row["entity_id"] for row in collection_rows(derived)}
    if set(manifest_fields) != observed_fields or set(manifest_collections) != observed_collections:
        return False
    observed_systems = {
        system.get("fides_key"): sorted(system.get("dataset_references") or [])
        for system in derived.get("systems") or []
    }
    accepted_systems = {
        key: sorted(system.get("dataset_references") or [])
        for key, system in manifest_systems.items()
    }
    return accepted_systems == observed_systems


def structure_digest(fields):
    return sha256_bytes("\n".join(sorted(field["name"] for field in fields)).encode("utf-8"))


def build_candidate(derived, scan, manifest, lock):
    manifest_datasets, manifest_collections, manifest_fields, manifest_systems = manifest_indexes(manifest)
    locked_fields = (lock or {}).get("entities") or {}
    locked_collections = (lock or {}).get("collections") or {}
    datasets = []

    for dataset in derived.get("datasets") or []:
        old_dataset = manifest_datasets.get(dataset.get("fides_key"), {})
        candidate_dataset = {
            "fides_key": dataset.get("fides_key"),
            "name": dataset.get("name"),
        }
        if old_dataset.get("description") is not None:
            candidate_dataset["description"] = old_dataset["description"]
        candidate_collections = []
        for collection in dataset.get("collections") or []:
            collection_id = collection.get("entity_id") or (
                f"{dataset.get('fides_key')}/{collection.get('name')}"
            )
            old_collection = manifest_collections.get(collection_id, {})
            locked_collection = locked_collections.get(collection_id)
            collection_unchanged = bool(
                locked_collection
                and locked_collection.get("semantic_digest") == collection.get("semantic_digest")
            )
            candidate_fields = []
            for field in collection.get("fields") or []:
                entity_id = field.get("entity_id") or f"{collection_id}/{field.get('name')}"
                old_field = manifest_fields.get(entity_id, {})
                locked_field = locked_fields.get(entity_id)
                field_unchanged = bool(
                    locked_field
                    and locked_field.get("semantic_digest") == field.get("semantic_digest")
                )
                candidate_field = {
                    "name": field.get("name"),
                    "data_categories": (
                        list(old_field.get("data_categories") or [])
                        if field_unchanged
                        else list(field.get("data_categories") or [])
                    ),
                    "refs": [field.get("ref")],
                }
                if field_unchanged and old_field.get("description") is not None:
                    candidate_field["description"] = old_field["description"]
                if not field_unchanged and field.get("needs_review") is True:
                    candidate_field["needs_review"] = True
                candidate_fields.append(candidate_field)

            candidate_collection = {
                "name": collection.get("name"),
                "refs": [collection.get("ref")],
                "structure_digest": structure_digest(candidate_fields),
            }
            if old_collection.get("description") is not None:
                candidate_collection["description"] = old_collection["description"]
            if collection_unchanged:
                if old_collection.get("interpretation") is not None:
                    interpretation = dict(old_collection["interpretation"])
                    if interpretation.get("refs") is not None:
                        interpretation["refs"] = [collection.get("ref")]
                    candidate_collection["interpretation"] = interpretation
                if old_collection.get("needs_review") is True:
                    candidate_collection["needs_review"] = True
            else:
                candidate_collection["needs_review"] = True
            candidate_collection["fields"] = candidate_fields
            candidate_collections.append(candidate_collection)
        candidate_dataset["collections"] = candidate_collections
        datasets.append(candidate_dataset)

    systems = []
    for system in derived.get("systems") or []:
        old = manifest_systems.get(system.get("fides_key"))
        if old:
            candidate = dict(old)
            previous_refs = list(old.get("dataset_references") or [])
            current_refs = list(system.get("dataset_references") or [])
            candidate["dataset_references"] = current_refs
            if previous_refs != current_refs:
                declarations = []
                for declaration in old.get("privacy_declarations") or []:
                    changed = dict(declaration)
                    changed.pop("interpretation", None)
                    changed["needs_review"] = True
                    declarations.append(changed)
                candidate["privacy_declarations"] = declarations
            systems.append(candidate)
        else:
            systems.append(
                {
                    "fides_key": system.get("fides_key"),
                    "name": system.get("name"),
                    "system_type": "Application",
                    "dataset_references": list(system.get("dataset_references") or []),
                    "privacy_declarations": [
                        {
                            "name": "",
                            "data_use": "",
                            "data_subjects": [],
                            "data_categories": [],
                            "refs": [system.get("ref")],
                            "needs_review": True,
                        }
                    ],
                }
            )

    provenance = scan.get("provenance") or {}
    return {
        "version": manifest.get("version", "0.7.0") if manifest else "0.7.0",
        "piece": PIECE,
        "source": {**provenance, "derived_digest": scan.get("derived_digest")},
        "dataset": datasets,
        "system": systems,
    }


def reconcile(derived, scan, manifest, lock, manifest_path):
    current_fields = {row["entity_id"]: row for row in field_rows(derived)}
    current_collections = {row["entity_id"]: row for row in collection_rows(derived)}
    locked_fields = (lock or {}).get("entities") or {}

    valid_manifest = False
    if manifest:
        report, _counts = VALIDATOR.validate(manifest, VALIDATOR.load_vocabulary())
        valid_manifest = not report.errors
    accepted_digest = (manifest or {}).get("source", {}).get("derived_digest")
    current_manifest = bool(
        manifest
        and accepted_digest in {scan.get("derived_digest"), scan.get("legacy_derived_digest")}
        and manifest_matches_observation(manifest, derived)
    )

    if lock:
        mode = "maintenance"
    elif valid_manifest and current_manifest:
        mode = "migration"
    else:
        mode = "bootstrap"

    # A valid manifest that describes this exact scan is already reviewed state. The first run of
    # the new workflow must seal it, not ask an agent to reinterpret every field just because the
    # new lock file did not exist in the previous release.
    if mode == "migration":
        locked_fields = {
            entity_id: {
                "semantic_digest": row["semantic_digest"],
                "refs": row["refs"],
            }
            for entity_id, row in current_fields.items()
        }

    actions = []
    proposal_required = []
    counts = {
        "unchanged": 0,
        "citation_only": 0,
        "added": 0,
        "removed": 0,
        "materially_changed": 0,
        "deterministically_classified": 0,
        "proposal_required": 0,
    }

    for entity_id, row in current_fields.items():
        locked = locked_fields.get(entity_id)
        if locked and locked.get("semantic_digest") == row["semantic_digest"]:
            citation_changed = locked.get("refs", []) != row["refs"]
            action = "refresh_evidence" if citation_changed else "carry_forward"
            counts["citation_only" if citation_changed else "unchanged"] += 1
        elif locked:
            action = "material_change"
            counts["materially_changed"] += 1
        else:
            action = "add"
            counts["added"] += 1

        needs_proposal = action in {"add", "material_change"} and row["needs_review"]
        if needs_proposal:
            counts["proposal_required"] += 1
            proposal_required.append(
                {
                    "entity_id": entity_id,
                    "dataset": row["dataset_name"],
                    "collection": row["collection"],
                    "field": row["field"],
                    "shape": row["shape"],
                    "refs": row["refs"],
                    "observation_digest": row["semantic_digest"],
                    "reason": "new field" if action == "add" else "materially changed field",
                }
            )
        elif action in {"add", "material_change"}:
            counts["deterministically_classified"] += 1

        actions.append(
            {
                "entity_id": entity_id,
                "action": action,
                "agent_required": needs_proposal,
                "collection_review_required": action in {"add", "material_change"},
            }
        )

    for entity_id, locked in locked_fields.items():
        if entity_id not in current_fields:
            counts["removed"] += 1
            actions.append(
                {
                    "entity_id": entity_id,
                    "action": "remove",
                    "agent_required": False,
                    "collection_review_required": True,
                    "previous_refs": locked.get("refs", []),
                }
            )

    actions.sort(key=lambda row: (row["entity_id"], row["action"]))
    proposal_required.sort(key=lambda row: row["entity_id"])
    baseline_mismatch = bool(
        lock
        and manifest_path.is_file()
        and lock.get("accepted_manifest_sha256") != manifest_sha(manifest_path)
    )
    collection_review_required = sorted(
        {
            action["entity_id"].rsplit("/", 1)[0]
            for action in actions
            if action["collection_review_required"]
        }
    )
    taxonomy_changed = bool(lock and lock.get("taxonomy_digest") != taxonomy_digest())
    reconciler_drift = bool(
        mode == "maintenance"
        and (
            baseline_mismatch
            or taxonomy_changed
            or any(action["action"] != "carry_forward" for action in actions)
        )
    )
    return {
        "version": VERSION,
        "piece": PIECE,
        "mode": mode,
        "drift": reconciler_drift,
        "agent_required": bool(proposal_required),
        "observation_digest": scan.get("derived_digest"),
        "baseline_manifest_matches_lock": not baseline_mismatch,
        "taxonomy_changed": taxonomy_changed,
        "current_manifest_valid": valid_manifest,
        "current_manifest_matches_observation": current_manifest,
        "counts": counts,
        "actions": actions,
        "proposal_required": proposal_required,
        "collection_review_required": collection_review_required,
        "special_category_refs": list(derived.get("special_category_refs") or []),
        "collections_observed": len(current_collections),
    }


def build_lock(derived, scan, manifest_path):
    return {
        "version": VERSION,
        "piece": PIECE,
        "source": {
            "slug": (scan.get("provenance") or {}).get("slug"),
            "derived_digest": scan.get("derived_digest"),
        },
        "taxonomy_digest": taxonomy_digest(),
        "accepted_manifest_sha256": manifest_sha(manifest_path),
        "collections": {
            row["entity_id"]: {
                "semantic_digest": row["semantic_digest"],
                "refs": row["refs"],
            }
            for row in collection_rows(derived)
        },
        "entities": {
            row["entity_id"]: {
                "kind": "field",
                "shape": row["shape"],
                "semantic_digest": row["semantic_digest"],
                "refs": row["refs"],
            }
            for row in field_rows(derived)
        },
    }


def parse_args(argv):
    opts = {"repo": pathlib.Path.cwd(), "seal": False, "json": False, "quiet": False}
    for arg in argv:
        if arg.startswith("--repo="):
            opts["repo"] = pathlib.Path(arg.split("=", 1)[1]).resolve()
        elif arg == "--seal":
            opts["seal"] = True
        elif arg == "--output=json":
            opts["json"] = True
        elif arg == "--output=text":
            opts["json"] = False
        elif arg == "--quiet":
            opts["quiet"] = True
        elif arg in {"-h", "--help"}:
            opts["help"] = True
        else:
            raise ValueError(f"unknown option '{arg}'")
    return opts


def main(argv):
    usage = "usage: reconcile.py --repo=<path> [--seal] [--output=json|text] [--quiet]\n"
    try:
        opts = parse_args(argv)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n{usage}")
        return 2
    if opts.get("help"):
        sys.stdout.write(usage)
        return 0

    repo = opts["repo"]
    cache = repo / ".noru" / ".cache"
    manifest_path = repo / ".noru" / "privacy-datamap.yml"
    lock_path = repo / ".noru" / "privacy-datamap.lock.json"
    try:
        derived = load_json(cache / "privacy-datamap.derived.json", required=True)
        scan = load_json(cache / "privacy-datamap.scan.json", required=True)
        manifest = load_manifest(manifest_path)
        lock = load_json(lock_path)
        if not isinstance(derived, dict) or not isinstance(scan, dict):
            raise ValueError("privacy-datamap scan artifacts must be JSON objects")
        if lock is not None and not isinstance(lock, dict):
            raise ValueError(".noru/privacy-datamap.lock.json must be a JSON object")
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    if opts["seal"]:
        if not manifest:
            sys.stderr.write("error: cannot seal without .noru/privacy-datamap.yml\n")
            return 1
        report, _counts = VALIDATOR.validate(manifest, VALIDATOR.load_vocabulary())
        if report.errors:
            sys.stderr.write(
                f"error: cannot seal an invalid manifest ({len(report.errors)} validation error(s))\n"
            )
            return 1
        if manifest.get("source", {}).get("derived_digest") != scan.get("derived_digest"):
            sys.stderr.write(
                "error: cannot seal a manifest that does not match the current repository scan\n"
            )
            return 1
        if not manifest_matches_observation(manifest, derived):
            sys.stderr.write(
                "error: cannot seal a manifest whose datasets, collections, fields or systems "
                "do not match the current observations\n"
            )
            return 1
        lock_document = build_lock(derived, scan, manifest_path)
        lock_path.write_text(
            json.dumps(lock_document, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = {
            "piece": PIECE,
            "ok": True,
            "mode": "sealed",
            "lock": str(lock_path.relative_to(repo)),
            "entities": len(lock_document["entities"]),
            "collections": len(lock_document["collections"]),
        }
    else:
        result = reconcile(derived, scan, manifest, lock, manifest_path)
        cache.mkdir(parents=True, exist_ok=True)
        reconciliation_path = cache / "privacy-datamap.reconciliation.json"
        proposals_path = cache / "privacy-datamap.proposals.json"
        candidate_path = cache / "privacy-datamap.candidate.yml"
        candidate_lock = (
            build_lock(derived, scan, manifest_path) if result["mode"] == "migration" else lock
        )
        candidate = build_candidate(derived, scan, manifest, candidate_lock)
        reconciliation_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        proposals_path.write_text(
            json.dumps(
                {
                    "version": VERSION,
                    "piece": PIECE,
                    "observation_digest": scan.get("derived_digest"),
                    "proposals": [
                        {
                            **item,
                            "proposed_categories": [],
                            "rationale": "",
                            "confidence": None,
                            "status": "requested",
                        }
                        for item in result["proposal_required"]
                    ],
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        candidate_path.write_text(
            "# Generated reconciliation candidate. Review this file; do not treat it as accepted.\n"
            "# Copy it to .noru/privacy-datamap.yml only after resolving every review flag.\n"
            + to_yaml(candidate),
            encoding="utf-8",
        )
        result.update(
            {
                "ok": True,
                "reconciliation": str(reconciliation_path.relative_to(repo)),
                "proposals": str(proposals_path.relative_to(repo)),
                "candidate": str(candidate_path.relative_to(repo)),
            }
        )

    if opts["json"]:
        sys.stdout.write(json.dumps(result, sort_keys=True if opts["quiet"] else False) + "\n")
    elif not opts["quiet"]:
        if result["mode"] == "sealed":
            print(f"sealed {result['entities']} field(s) in {result['lock']}")
        else:
            counts = result["counts"]
            print(
                f"{result['mode']}: {counts['unchanged']} unchanged, "
                f"{counts['citation_only']} citation-only, {counts['added']} added, "
                f"{counts['removed']} removed, {counts['materially_changed']} materially changed"
            )
            print(f"agent proposals required: {counts['proposal_required']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
