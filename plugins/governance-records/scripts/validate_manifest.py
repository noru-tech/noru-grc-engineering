#!/usr/bin/env python3
"""Validate .noru/governance-records.yml against the BUNDLED vocabulary.

Self-contained and atomic:
  * Python standard library only. No pip install, no venv, no network.
  * Record kinds, the meeting-shaped subset and the queue tool names come from
    ../references/vocabulary.json. There is no framework content in it and there never will be.
  * Parses YAML with PyYAML if it happens to be importable, otherwise a bundled fallback loader.

Four rules do most of the work here, and each exists because the record is worthless without it:

  * Contract requirement 8 — every record carries an `interpretation` block. The claim being
    attributed is "this is a true account of what was decided, and it satisfies this expectation".
  * Contract requirement 9 — every control and evidence-item id a record maps to must appear in the
    `queue_snapshot` that came from Noru. You cannot file against an expectation Noru did not say
    you had.
  * **Minutes need people.** A meeting-shaped record with no participants asserts that a decision
    was taken by nobody. The bundled vocabulary says which kinds are meeting-shaped.
  * **A governance claim is never open-ended.** contract/README.md scopes `expires_at` to technical
    claims and lets procedural obligations run on a review cadence instead — so this validator
    accepts either `interpretation.expires_at` or `next_review_due`, and rejects neither present.

Usage:
    python3 validate_manifest.py <manifest.yml> [--output=json] [--quiet] [--emit-parsed=<path>]
Exit codes: 0 = valid (warnings allowed), 1 = validation errors, 2 = usage / load error.
"""
import json
import pathlib
import sys

# --- BEGIN VENDORED yaml_mini ---
# Canonical copy: contract/lib/yaml_mini.py. Every piece validator embeds this block verbatim so
# that an installed plugin is self-contained (no sibling imports, no package). scripts/check_vendored_lib.py
# fails CI if a copy drifts; scripts/scaffold-piece.mjs stamps it into new pieces.
#
# Loads the block-YAML subset our manifests use. Uses PyYAML when it happens to be importable and
# otherwise falls back to a built-in parser, so the validator runs anywhere python3 exists with no
# install step and no network.
import difflib
import re

_BLOCK_SCALAR_RE = re.compile(r"^[>|][+\-]?\d*$")  # >, |, >-, >+, |-, |+, >2, |2, …


def load_yaml(text):
    """Return (document, loader_name)."""
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text), "PyYAML"
    except ImportError:
        return _fallback_load(text), "bundled fallback loader"


def suggest(key, valid):
    """difflib 'did you mean ...?' hint, or '' when nothing is close."""
    hit = difflib.get_close_matches(str(key), list(valid), n=1, cutoff=0.6)
    return f" (did you mean '{hit[0]}'?)" if hit else ""


def _scalar(raw):
    s = raw.strip()
    if s == "" or s in ("null", "~", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_scalar(p) for p in _split_flow(inner)] if inner else []
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _split_flow(inner):
    parts, buf, depth, quote = [], "", 0, None
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf += ch
        elif ch in "[{":
            depth += 1
            buf += ch
        elif ch in "]}":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return parts


def _strip_comment(line):
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in (" ", "\t")):
            return line[:i]
    return line


def _tokenize(text):
    out = []
    for raw in text.splitlines():
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue
        if stripped.lstrip().startswith("#"):
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        out.append((indent, stripped.lstrip(" ")))
    return out


def _split_kv(content):
    if content.endswith(":"):
        return content[:-1].strip(), None
    m = re.match(r"^(.*?):\s+(.*)$", content)
    if m:
        return m.group(1).strip(), m.group(2)
    return None, None


def _fallback_load(text):
    lines = _tokenize(text)
    if not lines:
        return None
    value, _ = _parse_node(lines, 0)
    return value


def _parse_node(lines, i):
    indent = lines[i][0]
    if lines[i][1] == "-" or lines[i][1].startswith("- "):
        return _parse_seq(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_seq(lines, i, indent):
    seq = []
    while i < len(lines) and lines[i][0] == indent and (
        lines[i][1] == "-" or lines[i][1].startswith("- ")
    ):
        rest = lines[i][1][1:].strip()
        if rest == "":
            i += 1
            if i < len(lines) and lines[i][0] > indent:
                val, i = _parse_node(lines, i)
            else:
                val = None
            seq.append(val)
        elif _split_kv(rest)[0] is not None:
            child_indent = indent + (len(lines[i][1]) - len(lines[i][1].lstrip("- ")))
            child_indent = indent + 2 if child_indent <= indent else child_indent
            group = [(child_indent, rest)]
            j = i + 1
            while j < len(lines) and lines[j][0] >= child_indent:
                group.append(lines[j])
                j += 1
            mapping, _ = _parse_map(group, 0, child_indent)
            seq.append(mapping)
            i = j
        else:
            seq.append(_scalar(rest))
            i += 1
    return seq, i


def _parse_map(lines, i, indent):
    d = {}
    while i < len(lines) and lines[i][0] == indent:
        key, val = _split_kv(lines[i][1])
        if key is None:
            break
        if val is None:
            i += 1
            if i < len(lines) and lines[i][0] > indent:
                child, i = _parse_node(lines, i)
                d[key] = child
            elif i < len(lines) and lines[i][0] == indent and (
                lines[i][1] == "-" or lines[i][1].startswith("- ")
            ):
                child, i = _parse_seq(lines, i, indent)
                d[key] = child
            else:
                d[key] = None
        elif _BLOCK_SCALAR_RE.match(val):
            # Block scalar (> folded, | literal): consume the indented continuation lines as the
            # value. Without this the continuation sits deeper than the current map context and
            # _parse_map breaks early, silently dropping every key after it.
            i += 1
            block_lines = []
            while i < len(lines) and lines[i][0] > indent:
                block_lines.append(lines[i][1])
                i += 1
            sep = " " if val[0] == ">" else "\n"
            d[key] = sep.join(block_lines)
        else:
            d[key] = _scalar(val)
            i += 1
    return d, i


class Report:
    """Collects errors and warnings with a dotted path, the way validate_manifest.py reports them."""

    def __init__(self):
        self.errors = []
        self.warnings = []

    def err(self, path, msg):
        self.errors.append((path, msg))

    def warn(self, path, msg):
        self.warnings.append((path, msg))

    def aslist(self, node):
        return node if isinstance(node, list) else []


# --- END VENDORED yaml_mini ---

REFERENCES = pathlib.Path(__file__).resolve().parent.parent / "references"
PIECE = "governance-records"

REF_RE = re.compile(r"^[^:\s][^:]*:[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

TOP_LEVEL_KEYS = {"version", "piece", "source", "queue_snapshot", "records"}
SOURCE_KEYS = {"slug", "commit_sha", "branch", "generated_by", "derived_digest"}
INTERPRETATION_KEYS = {"owner", "decided_at", "expires_at", "rationale", "refs"}
QUEUE_KEYS = {"fetched_at", "via", "controls"}
QUEUE_CONTROL_KEYS = {
    "control_id", "display_id", "name", "status", "coverage",
    "unmet_evidence_items", "testing_guidance_available",
}
QUEUE_ITEM_KEYS = {"id", "title", "type"}
RECORD_KEYS = {
    "key", "kind", "title", "occurred_on", "approved_on", "approved_by", "next_review_due",
    "document", "participants", "decisions", "actions", "refs", "control_mappings",
    "interpretation", "needs_review",
}
DOCUMENT_KEYS = {"file", "sha256", "size_bytes"}
PARTICIPANT_KEYS = {"name", "role", "attendance"}
ACTION_KEYS = {"description", "owner", "due_on", "status"}
MAPPING_KEYS = {"control_id", "evidence_item_ids"}

# A TODO left by the collector is a decision nobody has made yet, so it is never publishable.
TODO_RE = re.compile(r"\bTODO\b")


def load_vocabulary():
    return json.loads((REFERENCES / "vocabulary.json").read_text(encoding="utf-8"))


def check_unknown_keys(rep, path, obj, allowed):
    for key in obj:
        if key not in allowed:
            rep.err(f"{path}.{key}", f"unknown key '{key}'" + suggest(key, allowed))


def check_date(rep, path, value, required, label):
    if value is None:
        if required:
            rep.err(path, f"missing required `{label}` (YYYY-MM-DD)")
        return None
    if not isinstance(value, str) or not DATE_RE.match(value):
        rep.err(path, f"'{value}' is not an ISO date (YYYY-MM-DD)")
        return None
    return value


def check_refs(rep, path, obj):
    refs = obj.get("refs")
    if refs is None:
        rep.err(
            path,
            "missing required `refs` — cite the lines of the source document that produced this "
            "record",
        )
        return
    if not isinstance(refs, list) or len(refs) == 0:
        rep.err(f"{path}.refs", "must be a non-empty list — an uncited claim is an error")
        return
    for i, ref in enumerate(refs):
        if not isinstance(ref, str) or not REF_RE.match(ref):
            rep.err(f"{path}.refs[{i}]", f"'{ref}' is not a 'file:line' citation")


def check_interpretation(rep, path, record):
    """Requirement 8, plus the cadence carve-out contract/README.md grants procedural obligations."""
    block = record.get("interpretation")
    if block is None:
        rep.err(
            path,
            "missing required `interpretation` — name the person who stands behind this record, "
            "when they decided it, and why it satisfies the expectation",
        )
        return
    ipath = f"{path}.interpretation"
    if not isinstance(block, dict):
        rep.err(ipath, "must be a mapping")
        return
    check_unknown_keys(rep, ipath, block, INTERPRETATION_KEYS)

    owner = block.get("owner")
    if not owner or not isinstance(owner, str) or len(owner) < 3:
        rep.err(f"{ipath}.owner", "missing or too short — must name a person, not a team alias")
    elif TODO_RE.search(owner):
        rep.err(f"{ipath}.owner", "still a TODO — a placeholder cannot stand behind a record")

    decided_at = check_date(rep, f"{ipath}.decided_at", block.get("decided_at"), True, "decided_at")
    expires_at = check_date(rep, f"{ipath}.expires_at", block.get("expires_at"), False, "expires_at")

    rationale = block.get("rationale")
    if not rationale or not isinstance(rationale, str) or len(rationale.strip()) < 10:
        rep.err(
            f"{ipath}.rationale",
            "missing or too short — say why this record satisfies the expectation",
        )
    elif TODO_RE.search(rationale):
        rep.err(f"{ipath}.rationale", "still a TODO — write the reasoning before pushing it")

    next_review = record.get("next_review_due")
    if expires_at is None and next_review is None:
        rep.err(
            ipath,
            "no `expires_at` and no `next_review_due` — a governance obligation runs on a review "
            "cadence, so say when this must be produced again. An open-ended record is one nobody "
            "will ever revisit",
        )

    if expires_at and decided_at and expires_at <= decided_at:
        rep.err(
            f"{ipath}.expires_at",
            f"'{expires_at}' is not after decided_at '{decided_at}' — a claim cannot expire before "
            "it was made",
        )

    refs = block.get("refs")
    if refs is not None:
        if not isinstance(refs, list):
            rep.err(f"{ipath}.refs", "must be a list of 'file:line' strings")
        else:
            for i, ref in enumerate(refs):
                if not isinstance(ref, str) or not REF_RE.match(ref):
                    rep.err(f"{ipath}.refs[{i}]", f"'{ref}' is not a 'file:line' citation")


def check_source(rep, doc):
    src = doc.get("source")
    if not isinstance(src, dict):
        rep.err("source", "missing required `source` block (slug, commit_sha, branch, generated_by)")
        return
    check_unknown_keys(rep, "source", src, SOURCE_KEYS)
    for field in ("slug", "commit_sha", "branch", "generated_by"):
        if not src.get(field) or not isinstance(src.get(field), str):
            rep.err(
                f"source.{field}",
                f"missing required `{field}` — push provenance is not optional (requirement 4)",
            )
    sha = src.get("commit_sha")
    if isinstance(sha, str) and 0 < len(sha) < 7:
        rep.err("source.commit_sha", f"'{sha}' is too short to identify a commit")


def check_queue(rep, doc, vocab):
    queue = doc.get("queue_snapshot")
    known_controls = {}
    if not isinstance(queue, dict):
        rep.err(
            "queue_snapshot",
            "missing required `queue_snapshot` — this piece works Noru's queue and must record the "
            "queue it worked (requirement 9)",
        )
        return known_controls
    check_unknown_keys(rep, "queue_snapshot", queue, QUEUE_KEYS)

    if not queue.get("fetched_at"):
        rep.err("queue_snapshot.fetched_at", "missing required `fetched_at`")

    via = queue.get("via")
    if not isinstance(via, list) or len(via) == 0:
        rep.err(
            "queue_snapshot.via",
            "missing required `via` — name the Noru tools the snapshot came from",
        )
    else:
        for tool in via:
            if tool not in vocab["queue_tools"]:
                rep.err(
                    "queue_snapshot.via",
                    f"'{tool}' is not a Noru queue tool" + suggest(tool, vocab["queue_tools"]),
                )

    controls = queue.get("controls")
    if controls is None:
        rep.err("queue_snapshot.controls", "missing required `controls` (an empty list is valid)")
        controls = []
    elif not isinstance(controls, list):
        rep.err("queue_snapshot.controls", "must be a list")
        controls = []

    for i, control in enumerate(controls):
        cpath = f"queue_snapshot.controls[{i}]"
        if not isinstance(control, dict):
            rep.err(cpath, "control must be a mapping")
            continue
        check_unknown_keys(rep, cpath, control, QUEUE_CONTROL_KEYS)
        control_id = control.get("control_id")
        if not control_id or not isinstance(control_id, str):
            rep.err(f"{cpath}.control_id", "missing required `control_id`")
            continue
        if control_id != control_id.lower():
            rep.err(
                f"{cpath}.control_id",
                f"'{control_id}' is the uppercase display id; store the canonical lowercase `id` "
                "returned by getOrganizationControls",
            )
        items = control.get("unmet_evidence_items")
        if items is None:
            rep.err(f"{cpath}.unmet_evidence_items", "missing required `unmet_evidence_items`")
            items = []
        elif not isinstance(items, list):
            rep.err(f"{cpath}.unmet_evidence_items", "must be a list")
            items = []
        ids = set()
        for j, item in enumerate(items):
            ipath = f"{cpath}.unmet_evidence_items[{j}]"
            if not isinstance(item, dict):
                rep.err(ipath, "evidence item must be a mapping")
                continue
            check_unknown_keys(rep, ipath, item, QUEUE_ITEM_KEYS)
            if not item.get("id"):
                rep.err(f"{ipath}.id", "missing required `id`")
            elif not item.get("title"):
                rep.err(f"{ipath}.title", "missing required `title`")
            else:
                ids.add(item["id"])
        known_controls[control_id.lower()] = ids

    return known_controls


def check_document(rep, path, record, seen_digests):
    document = record.get("document")
    if not isinstance(document, dict):
        rep.err(
            f"{path}.document",
            "missing required `document` — a record has to say which file it was read from and what "
            "that file's bytes were",
        )
        return
    dpath = f"{path}.document"
    check_unknown_keys(rep, dpath, document, DOCUMENT_KEYS)

    file_path = document.get("file")
    if not file_path or not isinstance(file_path, str):
        rep.err(f"{dpath}.file", "missing required `file`")
    elif file_path.startswith("/") or ".." in file_path.split("/"):
        rep.err(
            f"{dpath}.file",
            f"'{file_path}' must be a path inside the repository, not absolute and not traversing "
            "out of it",
        )

    sha = document.get("sha256")
    if not isinstance(sha, str) or not SHA256_RE.match(sha):
        rep.err(f"{dpath}.sha256", "missing or malformed `sha256` (64 lowercase hex characters)")
    elif sha in seen_digests:
        rep.err(
            f"{dpath}.sha256",
            f"the same document is already filed as record '{seen_digests[sha]}' — filing one "
            "document twice puts two accounts of one meeting in front of an auditor",
        )
    else:
        seen_digests[sha] = record.get("key")

    size = document.get("size_bytes")
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
        rep.err(f"{dpath}.size_bytes", "must be a positive integer when present")


def check_participants(rep, path, record, vocab):
    """Minutes with nobody in them assert that a decision was taken by no one."""
    kind = record.get("kind")
    participants = record.get("participants")
    if participants is None:
        participants = []
    elif not isinstance(participants, list):
        rep.err(f"{path}.participants", "must be a list")
        return

    present = 0
    for i, participant in enumerate(participants):
        ppath = f"{path}.participants[{i}]"
        if not isinstance(participant, dict):
            rep.err(ppath, "participant must be a mapping")
            continue
        check_unknown_keys(rep, ppath, participant, PARTICIPANT_KEYS)
        name = participant.get("name")
        if not name or not isinstance(name, str) or len(name.strip()) < 2:
            rep.err(f"{ppath}.name", "missing or too short — name the person who was there")
        attendance = participant.get("attendance")
        if attendance is not None and attendance not in vocab["attendance"]:
            rep.err(
                f"{ppath}.attendance",
                f"'{attendance}' is not a known attendance value"
                + suggest(attendance, vocab["attendance"]),
            )
        if attendance in (None, "present", "delegate"):
            present += 1

    if kind in vocab["meeting_kinds"] and present == 0:
        rep.err(
            f"{path}.participants",
            f"a '{kind}' record with nobody present asserts that a decision was taken by no one; "
            "list who was in the room",
        )


def check_actions(rep, path, record, vocab):
    actions = record.get("actions")
    if actions is None:
        return
    if not isinstance(actions, list):
        rep.err(f"{path}.actions", "must be a list")
        return
    for i, action in enumerate(actions):
        apath = f"{path}.actions[{i}]"
        if not isinstance(action, dict):
            rep.err(apath, "action must be a mapping")
            continue
        check_unknown_keys(rep, apath, action, ACTION_KEYS)
        description = action.get("description")
        if not description or not isinstance(description, str) or len(description.strip()) < 5:
            rep.err(f"{apath}.description", "missing or too short — say what has to be done")
        owner = action.get("owner")
        if not owner or not isinstance(owner, str) or len(owner) < 3:
            rep.err(
                f"{apath}.owner",
                "missing required `owner` — an action nobody owns is not an action, it is a wish",
            )
        elif TODO_RE.search(owner):
            rep.err(f"{apath}.owner", "still a TODO — name the person who owns this action")
        check_date(rep, f"{apath}.due_on", action.get("due_on"), False, "due_on")
        status = action.get("status")
        if status is not None and status not in vocab["action_status"]:
            rep.err(
                f"{apath}.status",
                f"'{status}' is not a known action status" + suggest(status, vocab["action_status"]),
            )
        if action.get("due_on") is None and status in (None, "open", "in_progress"):
            rep.warn(
                f"{apath}.due_on",
                "an open action with no due date will not be chased by anyone",
            )


def check_mappings(rep, path, record, known_controls):
    mappings = record.get("control_mappings")
    if not isinstance(mappings, list) or len(mappings) == 0:
        rep.err(
            f"{path}.control_mappings",
            "missing required `control_mappings` — a record that satisfies nothing does not belong "
            "in the queue",
        )
        return
    for i, mapping in enumerate(mappings):
        mpath = f"{path}.control_mappings[{i}]"
        if not isinstance(mapping, dict):
            rep.err(mpath, "control mapping must be a mapping")
            continue
        check_unknown_keys(rep, mpath, mapping, MAPPING_KEYS)
        control_id = mapping.get("control_id")
        if not control_id or not isinstance(control_id, str):
            rep.err(f"{mpath}.control_id", "missing required `control_id`")
            continue
        normalized = control_id.lower()
        if normalized not in known_controls:
            rep.err(
                f"{mpath}.control_id",
                f"'{control_id}' is not in the queue snapshot — you can only file against an "
                "expectation Noru said you had" + suggest(normalized, sorted(known_controls)),
            )
            continue
        item_ids = mapping.get("evidence_item_ids")
        if item_ids is None:
            continue
        if not isinstance(item_ids, list):
            rep.err(f"{mpath}.evidence_item_ids", "must be a list")
            continue
        for item_id in item_ids:
            if item_id not in known_controls[normalized]:
                rep.err(
                    f"{mpath}.evidence_item_ids",
                    f"'{item_id}' is not an unmet evidence item of control '{normalized}' in the "
                    "queue snapshot" + suggest(item_id, sorted(known_controls[normalized])),
                )


def check_record(rep, path, record, vocab, known_controls, seen_keys, seen_digests):
    if not isinstance(record, dict):
        rep.err(path, "record must be a mapping")
        return
    check_unknown_keys(rep, path, record, RECORD_KEYS)

    key = record.get("key")
    if not key or not isinstance(key, str) or not KEY_RE.match(key):
        rep.err(
            f"{path}.key",
            f"'{key}' is not a stable lowercase key (letters, digits, '.', '_', '-')",
        )
    elif key in seen_keys:
        rep.err(
            f"{path}.key",
            f"duplicate key '{key}' — keys are the identity a re-push recognises, so they must be "
            "unique and stable",
        )
    else:
        seen_keys.add(key)

    kind = record.get("kind")
    if kind is None:
        rep.err(f"{path}.kind", "missing required `kind`")
    elif kind not in vocab["record_kind"]:
        rep.err(
            f"{path}.kind",
            f"unknown record kind '{kind}'" + suggest(kind, vocab["record_kind"]),
        )

    title = record.get("title")
    if not title or not isinstance(title, str):
        rep.err(f"{path}.title", "missing required `title`")

    occurred_on = check_date(
        rep, f"{path}.occurred_on", record.get("occurred_on"), True, "occurred_on"
    )
    approved_on = check_date(
        rep, f"{path}.approved_on", record.get("approved_on"), False, "approved_on"
    )
    next_review = check_date(
        rep, f"{path}.next_review_due", record.get("next_review_due"), False, "next_review_due"
    )

    if occurred_on and approved_on and approved_on < occurred_on:
        rep.err(
            f"{path}.approved_on",
            f"'{approved_on}' is before occurred_on '{occurred_on}' — a record cannot be approved "
            "before the thing it records happened",
        )
    if occurred_on and next_review and next_review <= occurred_on:
        rep.err(
            f"{path}.next_review_due",
            f"'{next_review}' is not after occurred_on '{occurred_on}'",
        )

    approved_by = record.get("approved_by")
    if approved_by is not None and (not isinstance(approved_by, str) or len(approved_by) < 3):
        rep.err(f"{path}.approved_by", "must name a person when present")

    decisions = record.get("decisions")
    if decisions is not None:
        if not isinstance(decisions, list):
            rep.err(f"{path}.decisions", "must be a list of strings")
        else:
            for i, decision in enumerate(decisions):
                if not isinstance(decision, str) or len(decision.strip()) < 5:
                    rep.err(f"{path}.decisions[{i}]", "each decision must be a sentence, not a stub")
    if not decisions and kind in vocab["meeting_kinds"]:
        rep.warn(
            f"{path}.decisions",
            "no decisions recorded for a meeting — if nothing was decided, say so in the rationale",
        )

    check_document(rep, path, record, seen_digests)
    check_participants(rep, path, record, vocab)
    check_actions(rep, path, record, vocab)
    check_refs(rep, path, record)
    check_mappings(rep, path, record, known_controls)
    check_interpretation(rep, path, record)

    if record.get("needs_review") is True:
        rep.err(
            f"{path}.needs_review",
            "still true — nobody has confirmed this record is a true account; resolve it and remove "
            "the flag before pushing",
        )


def validate(doc, vocab):
    rep = Report()
    counts = {"controls": 0, "unmet_items": 0, "records": 0}
    if not isinstance(doc, dict):
        rep.err(
            "<root>",
            "manifest must be a mapping with `version`, `piece`, `source`, `queue_snapshot`, "
            "`records`",
        )
        return rep, counts

    check_unknown_keys(rep, "<root>", doc, TOP_LEVEL_KEYS)

    version = doc.get("version")
    if not version or not isinstance(version, str) or not SEMVER_RE.match(version):
        rep.err("version", f"'{version}' is not a semantic version (for example 0.1.0)")
    if doc.get("piece") != PIECE:
        rep.err("piece", f"expected '{PIECE}', found '{doc.get('piece')}'")

    check_source(rep, doc)
    known_controls = check_queue(rep, doc, vocab)
    counts["controls"] = len(known_controls)
    counts["unmet_items"] = sum(len(v) for v in known_controls.values())

    records = doc.get("records")
    if records is None:
        rep.err("records", "missing required `records` (an empty list is a valid answer)")
        records = []
    elif not isinstance(records, list):
        rep.err("records", "must be a list")
        records = []

    seen_keys = set()
    seen_digests = {}
    for i, record in enumerate(records):
        check_record(rep, f"records[{i}]", record, vocab, known_controls, seen_keys, seen_digests)
    counts["records"] = len(records)

    if counts["unmet_items"] > 0 and counts["records"] == 0:
        rep.warn(
            "records",
            f"{counts['unmet_items']} unmet expectation(s) in the queue and no record filed against "
            "them",
        )

    return rep, counts


USAGE = (
    "usage: validate_manifest.py <manifest.yml> [--output=json] [--quiet] "
    "[--emit-parsed=<path.json>]\n"
)


def main(argv):
    output_json = False
    quiet = False
    emit_parsed = None
    positional = []
    for arg in argv:
        if arg == "--output=json":
            output_json = True
        elif arg == "--output=text":
            output_json = False
        elif arg == "--quiet":
            quiet = True
        elif arg.startswith("--emit-parsed="):
            emit_parsed = arg.split("=", 1)[1]
        elif arg in ("-h", "--help"):
            sys.stdout.write(USAGE)
            return 0
        elif arg.startswith("-"):
            sys.stderr.write(f"error: unknown option '{arg}'\n" + USAGE)
            return 2
        else:
            positional.append(arg)

    if len(positional) != 1:
        sys.stderr.write(USAGE)
        return 2

    path = pathlib.Path(positional[0])
    if not path.is_file():
        sys.stderr.write(f"error: no such file: {path}\n")
        return 2
    try:
        doc, loader = load_yaml(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"error: could not parse YAML ({exc})\n")
        return 2

    try:
        vocab = load_vocabulary()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"error: could not load bundled vocabulary ({exc})\n")
        return 2

    rep, counts = validate(doc, vocab)
    ok = not rep.errors

    if ok and emit_parsed:
        try:
            parsed_path = pathlib.Path(emit_parsed)
            parsed_path.parent.mkdir(parents=True, exist_ok=True)
            parsed_path.write_text(
                json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"error: could not write {emit_parsed} ({exc})\n")
            return 2

    if output_json:
        sys.stdout.write(
            json.dumps(
                {
                    "piece": PIECE,
                    "manifest": str(path),
                    "ok": ok,
                    "loader": loader,
                    "counts": counts,
                    "errors": [{"path": p, "message": m} for p, m in rep.errors],
                    "warnings": [{"path": p, "message": m} for p, m in rep.warnings],
                },
                indent=None if quiet else 2,
                sort_keys=True,
            )
            + "\n"
        )
        return 0 if ok else 1

    if not quiet:
        print(f"(parsed with {loader})")
        for p, m in rep.warnings:
            print(f"  WARN  {p}: {m}")
    for p, m in rep.errors:
        print(f"  ERROR {p}: {m}")

    if not ok:
        print(f"\nFAILED: {len(rep.errors)} error(s), {len(rep.warnings)} warning(s).")
        return 1
    if not quiet:
        print(
            f"\nOK: {counts['records']} record(s) against {counts['unmet_items']} unmet "
            f"expectation(s) across {counts['controls']} control(s) "
            f"({len(rep.warnings)} warning(s))."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
