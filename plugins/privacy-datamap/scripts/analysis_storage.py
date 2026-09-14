"""Canonical investigation storage; derived views are rebuilt, never nested in the cache."""
import copy
import hashlib
import json
import os
import tempfile
from collections import Counter

DERIVED_SECTIONS = {"evidence", "candidate", "reconciliation"}


def investigation_rows(document):
    yield from document.get("proposals", [])
    yield from document.get("retired_analysis", {}).values()


def decode(document):
    document = copy.deepcopy(document)
    blocks = document.pop("shared_reasoning", {})
    for row in investigation_rows(document):
        for key in ("analysis", "rationale"):
            reference = row.pop(key + "_ref", None)
            if reference is None:
                continue
            if reference not in blocks:
                raise ValueError("Missing shared reasoning block; restore the analysis cache")
            if key in row:
                raise ValueError("Supply either inline reasoning or a reasoning reference, not both")
            row[key] = copy.deepcopy(blocks[reference])
    return document


def encode(document):
    document = decode(document)
    for key in DERIVED_SECTIONS:
        document.pop(key, None)
    values = Counter()
    for row in investigation_rows(document):
        for key in ("analysis", "rationale"):
            if row.get(key):
                value = json.dumps(row[key], sort_keys=True, separators=(",", ":"))
                if len(value) >= 512:
                    values[value] += 1
    blocks = {}
    for row in investigation_rows(document):
        for key in ("analysis", "rationale"):
            if not row.get(key):
                continue
            value = json.dumps(row[key], sort_keys=True, separators=(",", ":"))
            if values[value] < 2:
                continue
            identity = hashlib.sha256(value.encode()).hexdigest()
            blocks[identity] = row.pop(key)
            row[key + "_ref"] = identity
    if blocks:
        document["shared_reasoning"] = blocks
    return document


def load(path):
    return decode(json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else None


def save(path, document):
    content = json.dumps(encode(document), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
