#!/usr/bin/env python3
"""Validate .noru/audit-pack.yml against the BUNDLED vocabulary.

Self-contained and atomic:
  * Python standard library only. No pip install, no venv, no network.
  * Conclusions, inspected kinds, sampling methods, dispositions, the minimum-sample bands, the
    per-conclusion assurance horizons and the queue tool names come from
    ../references/vocabulary.json. There is no framework content in it and there never will be —
    the testing procedure in particular stays behind the API, and this manifest records only
    whether one is available.
  * Parses YAML with PyYAML if it happens to be importable, otherwise a bundled fallback loader.

An audit pack is the one artifact in this toolkit that is mostly ASSEMBLED rather than discovered,
so the questions this validator asks are about whether the assembly holds together:

  * **Did the pack test what Noru put in scope?** Every control id and evidence item id has to be in
    the queue snapshot, and one workpaper covers one control. Two accounts of the same control is
    two conclusions somebody has to reconcile.
  * **Was what it says it inspected actually read?** An inspected artifact or manifest has to be one
    the scan digested, at that digest. What an auditor gets handed has to be the bytes that were
    tested, or the digests in the pack prove nothing.
  * **Can the sample be redrawn?** Method, seed and size are all recorded, the drawn list has to
    match the declared size, and the size has to meet the smallest defensible sample for a
    population that big.
  * **Is the conclusion bounded by the period it is about?** `expires_at` is REQUIRED and is
    measured from the END of the audit window rather than from the signature: a workpaper concludes
    about a period, and signing it late does not extend what it covers. A conclusion of `deficient`
    or `not_tested` gets a short horizon — a control you found broken is not something to sign off
    for a year.
  * **Did somebody other than the preparer look?** A pack reviewed by the person who prepared it is
    recorded as reviewed while nobody checked, which is worse than leaving it unreviewed.

`--as-of=YYYY-MM-DD` turns an already-expired conclusion into an error. Leave it off and the file is
judged on its own terms, which is what keeps this validator deterministic; pass it before an audit
and a pack nobody has renewed fails. Nothing here ever reads the clock by itself.

Usage:
    python3 validate_manifest.py <manifest.yml> [--as-of=YYYY-MM-DD] [--output=json] [--quiet]
                                 [--emit-parsed=<path>]
Exit codes: 0 = valid (warnings allowed), 1 = validation errors, 2 = usage / load error.
"""
import datetime
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

_BLOCK_SCALAR_RE = re.compile(r"^[>|](?:[+-]\d*|\d*[+-]?)$")  # >, |, >-, |+, >2, |2-, |-2, …


def load_yaml(text):
    """Return (document, loader_name).

    The two loaders must agree on types, or a manifest that validates on one machine fails on
    another. PyYAML resolves an unquoted `2026-08-01` to a datetime.date and an unquoted
    timestamp to a datetime.datetime; the fallback loader leaves both as strings. Every date in
    our manifests is an ISO string by contract, so we strip the timestamp resolver rather than
    converting after the fact -- that keeps the author's exact text, which the error messages
    quote back at them.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        return _fallback_load(text), "bundled fallback loader"

    return yaml.load(text, Loader=_string_dates_loader(yaml)), "PyYAML"


_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_loader_cache = {}


def _string_dates_loader(yaml):
    """A SafeLoader with the implicit timestamp resolver removed, so dates stay strings."""
    cached = _loader_cache.get("loader")
    if cached is not None:
        return cached

    class _StringDatesLoader(yaml.SafeLoader):
        pass

    # yaml_implicit_resolvers is a class attribute shared with the parent until reassigned, so
    # rebuild it here rather than mutating the lists in place and poisoning yaml.SafeLoader.
    _StringDatesLoader.yaml_implicit_resolvers = {
        ch: [(tag, regexp) for tag, regexp in resolvers if tag != _TIMESTAMP_TAG]
        for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    _loader_cache["loader"] = _StringDatesLoader
    return _StringDatesLoader


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


def _block_header(val):
    """Split a `>`/`|` header into (style, chomping, explicit indent), or None if it is not one."""
    if not _BLOCK_SCALAR_RE.match(val):
        return None
    style, chomp, digits = val[0], "", ""
    for ch in val[1:]:  # YAML allows the two indicators in either order: `|2-` and `|-2`
        if ch in "+-":
            chomp = ch
        else:
            digits += ch
    return style, chomp, int(digits) if digits else 0


def _read_block_scalar(raw, start, key_col, header, final_break):
    """Return (value, index after the block) for the `>`/`|` block whose header is above `start`.

    The lines are read from the raw document, never from the comment-stripped stream: inside a
    block scalar a `#` is prose, not a comment. Stripping it deletes the rest of the line, so a
    rationale citing `ticket #4412`, `C#` or a URL fragment would be stored short -- content loss
    the reader has no way to notice, on exactly the machines that have no PyYAML to fall back to.
    """
    style, chomp, increment = header
    # An explicit indicator counts from the key's column; otherwise the first non-blank line sets
    # the indentation, and anything less indented than it ends the block.
    indent = key_col + increment if increment else None
    body, i = [], start
    while i < len(raw):
        line = raw[i]
        if line.strip():
            line_indent = len(line) - len(line.lstrip(" "))
            if indent is None:
                if line_indent <= key_col:
                    break
                indent = line_indent
            elif line_indent < indent:
                break
        body.append(line)
        i += 1

    stripped = [line[indent:] if indent and len(line) > indent else "" for line in body]
    end = len(stripped)
    while end and stripped[end - 1] == "":
        end -= 1
    content, trailing = stripped[:end], len(stripped) - end

    # Breaks available to the chomping indicator: the one that ends the last content line, plus
    # one per blank line after it. A document that stops mid-line ends its last line with no
    # break at all, and PyYAML drops the newline accordingly.
    breaks = trailing + (1 if content else 0)
    if i >= len(raw) and not final_break:
        breaks -= 1
    breaks = max(breaks, 0)
    if not content:
        # An empty block keeps its blank lines only under `+`; clip and strip discard them.
        return ("\n" * breaks if chomp == "+" else ""), i

    text = "\n".join(content) if style == "|" else _fold(content)
    if chomp == "-":
        return text, i
    return text + "\n" * (breaks if chomp == "+" else min(breaks, 1)), i


def _fold(lines):
    """Fold a `>` block: line breaks become spaces, except around blank or more-indented lines."""
    out, prev = [], None
    for line in lines:
        if line == "":
            out.append("\n")  # a run of n blank lines folds to n newlines, not n + 1
        else:
            if out and prev != "":
                out.append("\n" if prev.startswith(" ") or line.startswith(" ") else " ")
            out.append(line)
        prev = line
    return "".join(out)


def _tokenize(text):
    """Flatten the document to (indent, content, block) triples; `block` is a resolved scalar.

    Block scalars are resolved here rather than in _parse_map because their extent is a property
    of the raw text -- their lines must escape comment stripping, blank-line dropping and the
    rstrip that the rest of the tokenizer applies.
    """
    out, raw, i = [], text.splitlines(), 0
    # Whether the document's last line is terminated: a block that runs to an unterminated final
    # line has one break fewer than its line count suggests.
    final_break = bool(raw) and text.splitlines(True)[-1] != raw[-1]
    while i < len(raw):
        stripped = _strip_comment(raw[i]).rstrip()
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            i += 1
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.lstrip(" ")
        key, val = _split_kv(content)
        header = _block_header(val) if val else None
        if key is None or header is None:
            out.append((indent, content, None))
            i += 1
            continue
        # A block under a sequence item indents from the key, not from the dash: in `- note: >`
        # the sibling `other:` sits deeper than the dash but shallower than the block's content.
        key_col = indent + (len(content) - len(content.lstrip("- ")))
        block, i = _read_block_scalar(raw, i + 1, key_col, header, final_break)
        out.append((indent, key + ":", block))
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
            group = [(child_indent, rest, lines[i][2])]
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
        if lines[i][2] is not None:
            d[key] = lines[i][2]
            i += 1
        elif val is None:
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
PIECE = "audit-pack"

REF_RE = re.compile(r"^[^:\s][^:]*:[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

TOP_LEVEL_KEYS = {"version", "piece", "source", "pack", "queue_snapshot", "inputs", "workpapers"}
SOURCE_KEYS = {"slug", "commit_sha", "branch", "generated_by", "derived_digest"}
INTERPRETATION_KEYS = {"owner", "decided_at", "expires_at", "rationale", "refs"}
PACK_KEYS = {"key", "title", "window", "prepared_by", "reviewed_by"}
WINDOW_KEYS = {"from", "to"}
QUEUE_KEYS = {"fetched_at", "via", "framework_id", "framework_name", "controls"}
QUEUE_CONTROL_KEYS = {
    "control_id", "display_id", "name", "status", "coverage", "testing_guidance_available",
    "expected_evidence_items", "linked_evidence",
}
QUEUE_ITEM_KEYS = {"id", "title", "type"}
QUEUE_LINKED_KEYS = {"evidence_id", "title", "status", "type", "expires_at", "evidence_item_id"}
INPUTS_KEYS = {"artifacts", "manifests"}
ARTIFACT_KEYS = {"file", "sha256", "size_bytes"}
MANIFEST_KEYS = {"piece", "file", "sha256"}
WORKPAPER_KEYS = {
    "key", "control_id", "evidence_item_ids", "scope", "inspected", "population", "sample",
    "exceptions", "conclusion", "refs", "interpretation", "needs_review",
}
INSPECTED_KEYS = {"kind", "reference", "sha256", "note"}
POPULATION_KEYS = {"file", "sha256", "size"}
SAMPLE_KEYS = {"method", "seed", "size", "drawn"}
EXCEPTION_KEYS = {"reference", "description", "disposition", "owner", "resolved_on"}

# A TODO left by the collector is a decision nobody has made yet, so it is never publishable.
TODO_RE = re.compile(r"\bTODO\b")


def load_vocabulary():
    vocab = json.loads((REFERENCES / "vocabulary.json").read_text(encoding="utf-8"))
    # A conclusion with no assurance horizon would skip the expiry check silently, which is the one
    # failure mode this validator cannot afford: the check would still report success.
    missing = sorted(set(vocab["conclusion"]) - set(vocab["assurance_days"]))
    if missing:
        raise ValueError(
            f"assurance_days has no entry for {missing} — every conclusion needs a horizon or the "
            "expiry check passes without checking anything"
        )
    return vocab


def parse_date(value):
    """None when the value is not an ISO date. The caller decides whether that is an error."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def minimum_sample_for(vocab, population_size):
    """The smallest defensible sample for a population this size, capped at testing everything."""
    for band in vocab["minimum_sample"]:
        if band["up_to"] is None or population_size <= band["up_to"]:
            return min(band["minimum"], population_size)
    return population_size


def check_unknown_keys(rep, path, obj, allowed):
    for key in obj:
        if key not in allowed:
            rep.err(f"{path}.{key}", f"unknown key '{key}'" + suggest(key, allowed))


def check_date(rep, path, value, required, label):
    if value is None:
        if required:
            rep.err(path, f"missing required `{label}` (YYYY-MM-DD)")
        return None
    parsed = parse_date(value)
    if parsed is None:
        rep.err(path, f"'{value}' is not an ISO date (YYYY-MM-DD)")
    return parsed


def check_person(rep, path, value, what):
    if not value or not isinstance(value, str) or len(value) < 3:
        rep.err(path, f"missing or too short — {what}")
        return None
    if TODO_RE.search(value):
        rep.err(path, "still a TODO — a placeholder cannot stand behind anything")
        return None
    return value


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


def check_pack(rep, doc):
    """What this pack is. The window is the frame every conclusion in the file is measured against."""
    pack = doc.get("pack")
    if not isinstance(pack, dict):
        rep.err(
            "pack",
            "missing required `pack` block — a pack has to say which framework and which period it "
            "is about, and who assembled it",
        )
        return None
    check_unknown_keys(rep, "pack", pack, PACK_KEYS)

    key = pack.get("key")
    if not key or not isinstance(key, str) or not KEY_RE.match(key):
        rep.err("pack.key", f"'{key}' is not a stable lowercase key (letters, digits, '.', '_', '-')")

    title = pack.get("title")
    if isinstance(title, str) and TODO_RE.search(title):
        rep.err("pack.title", "still a TODO — name the pack before handing it to anyone")

    prepared_by = check_person(
        rep, "pack.prepared_by", pack.get("prepared_by"),
        "name the person who assembled this pack, not a team alias",
    )
    reviewed_by = pack.get("reviewed_by")
    if reviewed_by is None:
        rep.warn(
            "pack.reviewed_by",
            "nobody has reviewed this pack — one person's judgement, unchecked, is what a second "
            "pair of eyes exists to catch",
        )
    else:
        check_person(rep, "pack.reviewed_by", reviewed_by, "name the reviewer, not a team alias")
        if isinstance(prepared_by, str) and reviewed_by == prepared_by:
            rep.err(
                "pack.reviewed_by",
                f"'{reviewed_by}' also prepared this pack — somebody cannot review their own pack, "
                "and recording it as reviewed is worse than leaving it unreviewed",
            )

    window = pack.get("window")
    if not isinstance(window, dict):
        rep.err(
            "pack.window",
            "missing required `window` — every conclusion in this file is about a period, and the "
            "expiry of each one is measured from the day that period ended",
        )
        return None
    check_unknown_keys(rep, "pack.window", window, WINDOW_KEYS)
    start = check_date(rep, "pack.window.from", window.get("from"), True, "from")
    end = check_date(rep, "pack.window.to", window.get("to"), True, "to")
    if start is not None and end is not None and end <= start:
        rep.err(
            "pack.window.to",
            f"'{end.isoformat()}' is not after from '{start.isoformat()}' — an audit window runs "
            "forwards",
        )
    return end


def check_queue(rep, doc, vocab):
    """Requirement 9. The scope came from Noru; nothing here may name anything it did not return."""
    queue = doc.get("queue_snapshot")
    empty = {"controls": {}, "evidence": set()}
    if not isinstance(queue, dict):
        rep.err(
            "queue_snapshot",
            "missing required `queue_snapshot` — this piece assembles what Noru already holds and "
            "must record the scope it assembled (requirement 9)",
        )
        return empty
    check_unknown_keys(rep, "queue_snapshot", queue, QUEUE_KEYS)

    if not queue.get("fetched_at"):
        rep.err("queue_snapshot.fetched_at", "missing required `fetched_at`")
    if not queue.get("framework_id"):
        rep.err(
            "queue_snapshot.framework_id",
            "missing required `framework_id` — a pack is about one framework's expectations, and "
            "the id comes from getOrganizationFrameworks",
        )

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
                    f"'{tool}' is not a Noru queue tool this piece reads"
                    + suggest(tool, vocab["queue_tools"]),
                )

    controls = queue.get("controls")
    if controls is None:
        rep.err("queue_snapshot.controls", "missing required `controls` (an empty list is valid)")
        controls = []
    elif not isinstance(controls, list):
        rep.err("queue_snapshot.controls", "must be a list")
        controls = []

    known = {}
    evidence_ids = set()
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

        expected = control.get("expected_evidence_items")
        if expected is None:
            rep.err(f"{cpath}.expected_evidence_items", "missing required `expected_evidence_items`")
            expected = []
        elif not isinstance(expected, list):
            rep.err(f"{cpath}.expected_evidence_items", "must be a list")
            expected = []
        item_ids = set()
        for j, item in enumerate(expected):
            ipath = f"{cpath}.expected_evidence_items[{j}]"
            if not isinstance(item, dict):
                rep.err(ipath, "evidence item must be a mapping")
                continue
            check_unknown_keys(rep, ipath, item, QUEUE_ITEM_KEYS)
            if not item.get("id"):
                rep.err(f"{ipath}.id", "missing required `id`")
            elif not item.get("title"):
                rep.err(f"{ipath}.title", "missing required `title`")
            else:
                item_ids.add(item["id"])

        linked = control.get("linked_evidence")
        if linked is None:
            rep.err(f"{cpath}.linked_evidence", "missing required `linked_evidence`")
            linked = []
        elif not isinstance(linked, list):
            rep.err(f"{cpath}.linked_evidence", "must be a list")
            linked = []
        expired = []
        for j, row in enumerate(linked):
            lpath = f"{cpath}.linked_evidence[{j}]"
            if not isinstance(row, dict):
                rep.err(lpath, "linked evidence must be a mapping")
                continue
            check_unknown_keys(rep, lpath, row, QUEUE_LINKED_KEYS)
            if not row.get("evidence_id"):
                rep.err(f"{lpath}.evidence_id", "missing required `evidence_id`")
                continue
            evidence_ids.add(row["evidence_id"])
            if str(row.get("status") or "") == "expired":
                expired.append(row["evidence_id"])

        known[control_id.lower()] = {"items": item_ids, "expired": expired}

    return {"controls": known, "evidence": evidence_ids}


def check_inputs(rep, doc):
    """The local half, digested by the scan. Nothing may be inspected that was not digested."""
    inputs = doc.get("inputs")
    digests = {}
    if not isinstance(inputs, dict):
        rep.err(
            "inputs",
            "missing required `inputs` block — a pack has to say which local files it was built "
            "from and what their bytes were, or what an auditor is handed cannot be checked",
        )
        return digests
    check_unknown_keys(rep, "inputs", inputs, INPUTS_KEYS)

    for field, allowed, required in (
        ("artifacts", ARTIFACT_KEYS, ("file", "sha256")),
        ("manifests", MANIFEST_KEYS, ("piece", "file", "sha256")),
    ):
        rows = inputs.get(field)
        if rows is None:
            rep.err(f"inputs.{field}", f"missing required `{field}` (an empty list is valid)")
            continue
        if not isinstance(rows, list):
            rep.err(f"inputs.{field}", "must be a list")
            continue
        for i, row in enumerate(rows):
            rpath = f"inputs.{field}[{i}]"
            if not isinstance(row, dict):
                rep.err(rpath, "must be a mapping")
                continue
            check_unknown_keys(rep, rpath, row, allowed)
            for name in required:
                if not row.get(name) or not isinstance(row.get(name), str):
                    rep.err(f"{rpath}.{name}", f"missing required `{name}`")
            path = row.get("file")
            sha = row.get("sha256")
            if isinstance(path, str) and (path.startswith("/") or ".." in path.split("/")):
                rep.err(
                    f"{rpath}.file",
                    f"'{path}' must be a path inside the repository, not absolute and not "
                    "traversing out of it",
                )
            if isinstance(sha, str) and not SHA256_RE.match(sha):
                rep.err(f"{rpath}.sha256", "malformed `sha256` (64 lowercase hex characters)")
            elif isinstance(path, str) and isinstance(sha, str):
                if path in digests and digests[path] != sha:
                    rep.err(
                        f"{rpath}.sha256",
                        f"'{path}' is listed twice with different digests — one of them is not the "
                        "file that was tested",
                    )
                digests[path] = sha

    size = inputs.get("artifacts")
    if isinstance(size, list):
        for i, row in enumerate(size):
            if not isinstance(row, dict):
                continue
            value = row.get("size_bytes")
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                rep.err(f"inputs.artifacts[{i}].size_bytes", "must be a non-negative integer")

    return digests


def check_inspected(rep, path, workpaper, digests, queue):
    rows = workpaper.get("inspected")
    if rows is None:
        rep.err(
            path,
            "missing required `inspected` — a workpaper says what somebody actually opened, not "
            "what happens to exist",
        )
        return
    if not isinstance(rows, list) or len(rows) == 0:
        rep.err(f"{path}.inspected", "must be a non-empty list")
        return

    vocab_kinds = ("artifact", "evidence", "manifest")
    for i, row in enumerate(rows):
        ipath = f"{path}.inspected[{i}]"
        if not isinstance(row, dict):
            rep.err(ipath, "must be a mapping")
            continue
        check_unknown_keys(rep, ipath, row, INSPECTED_KEYS)
        kind = row.get("kind")
        if kind not in vocab_kinds:
            rep.err(f"{ipath}.kind", f"unknown inspected kind '{kind}'" + suggest(kind, vocab_kinds))
            continue
        reference = row.get("reference")
        if not reference or not isinstance(reference, str):
            rep.err(f"{ipath}.reference", "missing required `reference`")
            continue
        if TODO_RE.search(reference):
            rep.err(f"{ipath}.reference", "still a TODO — say what was actually inspected")
            continue

        if kind == "evidence":
            if reference not in queue["evidence"]:
                rep.err(
                    f"{ipath}.reference",
                    f"'{reference}' is not an evidence record the queue snapshot returned — a pack "
                    "cites what Noru says is linked, never an id from memory"
                    + suggest(reference, sorted(queue["evidence"])),
                )
            if row.get("sha256") is not None:
                rep.err(
                    f"{ipath}.sha256",
                    "an evidence record is identified by its id, not by a local digest — the bytes "
                    "are Noru's",
                )
            continue

        if reference not in digests:
            rep.err(
                f"{ipath}.reference",
                f"'{reference}' was not digested by this scan — a pack may only be built from files "
                "the scan actually read, or the digest an auditor is handed proves nothing"
                + suggest(reference, sorted(digests)),
            )
            continue
        sha = row.get("sha256")
        if sha is None:
            rep.err(
                f"{ipath}.sha256",
                "missing required `sha256` for an inspected file — what an auditor gets handed has "
                "to be the bytes that were tested",
            )
        elif sha != digests[reference]:
            rep.err(
                f"{ipath}.sha256",
                f"records a digest that does not match the one the scan took for '{reference}' — "
                "the file changed after it was tested, or the digest was copied from elsewhere",
            )


def check_sampling(rep, path, workpaper, vocab):
    population = workpaper.get("population")
    sample = workpaper.get("sample")
    if population is None and sample is None:
        return
    if sample is not None and population is None:
        rep.err(
            f"{path}.population",
            "a sample with no population is a list of items nobody can put in proportion — say what "
            "it was drawn from, how big that was, and what its digest is",
        )
        return

    ppath = f"{path}.population"
    if not isinstance(population, dict):
        rep.err(ppath, "must be a mapping")
        return
    check_unknown_keys(rep, ppath, population, POPULATION_KEYS)
    if not population.get("file") or not isinstance(population.get("file"), str):
        rep.err(f"{ppath}.file", "missing required `file`")
    sha = population.get("sha256")
    if not isinstance(sha, str) or not SHA256_RE.match(sha):
        rep.err(
            f"{ppath}.sha256",
            "missing or malformed `sha256` (64 lowercase hex characters) — the digest is what makes "
            "the sample checkable: change the export and the conclusion no longer covers it",
        )
    size = population.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        rep.err(f"{ppath}.size", "missing or not a positive integer")
        return

    if sample is None:
        rep.warn(
            ppath,
            "a population with no sample — either say what was drawn from it, or drop it",
        )
        return

    spath = f"{path}.sample"
    if not isinstance(sample, dict):
        rep.err(spath, "must be a mapping")
        return
    check_unknown_keys(rep, spath, sample, SAMPLE_KEYS)

    method = sample.get("method")
    if method not in vocab["sampling_method"]:
        rep.err(
            f"{spath}.method",
            f"unknown sampling method '{method}'" + suggest(method, vocab["sampling_method"]),
        )
        method = None

    seed = sample.get("seed")
    if method == "deterministic_hash":
        if not seed or not isinstance(seed, str) or len(seed) < 8:
            rep.err(
                f"{spath}.seed",
                "missing required `seed` — without it nobody can redraw this sample, and a sample "
                "nobody can redraw is a list somebody typed",
            )
    elif method == "full_population" and seed is not None:
        rep.err(
            f"{spath}.seed",
            "a full-population test draws nothing, so there is no seed to record",
        )

    drawn = sample.get("drawn")
    if not isinstance(drawn, list) or len(drawn) == 0:
        rep.err(f"{spath}.drawn", "must be a non-empty list of references into the population")
        return
    if len(set(drawn)) != len(drawn):
        rep.err(
            f"{spath}.drawn",
            "contains the same item twice — a sample of ten with a duplicate is a sample of nine",
        )

    declared = sample.get("size")
    if not isinstance(declared, int) or isinstance(declared, bool) or declared < 1:
        rep.err(f"{spath}.size", "missing or not a positive integer")
        return
    if declared != len(drawn):
        rep.err(
            f"{spath}.size",
            f"says {declared} but {len(drawn)} item(s) are listed — the number that was tested and "
            "the number that was recorded are not the same claim",
        )
    if declared > size:
        rep.err(
            f"{spath}.size",
            f"is larger than the population ({size}) — a sample cannot exceed what it was drawn from",
        )
        return

    if method == "full_population" and declared != size:
        rep.err(
            f"{spath}.size",
            f"claims the whole population but covers {declared} of {size} — that is a sample, not a "
            "full-population test",
        )
        return

    minimum = minimum_sample_for(vocab, size)
    if declared < minimum:
        rep.err(
            f"{spath}.size",
            f"{declared} out of a population of {size} is below {minimum}, the smallest defensible "
            "sample for a population that size. Either test more of it, or record what you did as "
            "a full-population test over the subset you actually scoped and say in the rationale "
            "how that subset was chosen",
        )


def check_exceptions(rep, path, workpaper, vocab, conclusion):
    exceptions = workpaper.get("exceptions")
    if exceptions is None:
        return
    if not isinstance(exceptions, list):
        rep.err(f"{path}.exceptions", "must be a list")
        return
    for i, exception in enumerate(exceptions):
        epath = f"{path}.exceptions[{i}]"
        if not isinstance(exception, dict):
            rep.err(epath, "exception must be a mapping")
            continue
        check_unknown_keys(rep, epath, exception, EXCEPTION_KEYS)
        if not exception.get("reference") or not isinstance(exception.get("reference"), str):
            rep.err(
                f"{epath}.reference",
                "missing required `reference` — say how to find this item again in the population",
            )
        description = exception.get("description")
        if not description or not isinstance(description, str) or len(description.strip()) < 10:
            rep.err(f"{epath}.description", "missing or too short — say what was wrong with it")
        disposition = exception.get("disposition")
        if disposition is None:
            rep.err(f"{epath}.disposition", "missing required `disposition`")
        elif disposition not in vocab["disposition"]:
            rep.err(
                f"{epath}.disposition",
                f"unknown disposition '{disposition}'" + suggest(disposition, vocab["disposition"]),
            )
        check_person(
            rep, f"{epath}.owner", exception.get("owner"),
            "an exception nobody owns will still be there at the next audit",
        )
        check_date(rep, f"{epath}.resolved_on", exception.get("resolved_on"), False, "resolved_on")

        if conclusion == "effective" and disposition == "deferred":
            rep.err(
                f"{epath}.disposition",
                "this workpaper concludes the control was effective while deferring an exception "
                "against it. A deferred exception is one nobody has dealt with, so either it was "
                "not effective or this is not really deferred",
            )
        elif conclusion == "effective" and disposition == "accepted_risk":
            rep.warn(
                f"{epath}.disposition",
                "an accepted risk under an effective conclusion — defensible, but the rationale "
                "has to say why the acceptance does not undermine the conclusion",
            )


def check_interpretation(rep, path, workpaper, vocab, window_end, as_of):
    """Requirement 8, anchored on the end of the window the conclusion is about."""
    block = workpaper.get("interpretation")
    if block is None:
        rep.err(
            path,
            "missing required `interpretation` — a conclusion nobody signed is a note. Name the "
            "person who concluded, the day they did, the day the conclusion stops being current, "
            "and what it rests on",
        )
        return
    ipath = f"{path}.interpretation"
    if not isinstance(block, dict):
        rep.err(ipath, "must be a mapping")
        return
    check_unknown_keys(rep, ipath, block, INTERPRETATION_KEYS)

    check_person(
        rep, f"{ipath}.owner", block.get("owner"),
        "name the person who drew this conclusion, not a team alias. A team cannot be asked what "
        "it looked at",
    )

    decided_at = check_date(rep, f"{ipath}.decided_at", block.get("decided_at"), True, "decided_at")

    expires_at = None
    if block.get("expires_at") is None:
        rep.err(
            f"{ipath}.expires_at",
            "missing required `expires_at` — a pack is assurance about a period, and assurance that "
            "never lapses is assurance nobody will renew",
        )
    else:
        expires_at = check_date(
            rep, f"{ipath}.expires_at", block.get("expires_at"), True, "expires_at"
        )

    rationale = block.get("rationale")
    if not rationale or not isinstance(rationale, str) or len(rationale.strip()) < 10:
        rep.err(
            f"{ipath}.rationale",
            "missing or too short — say what you concluded and what it rests on",
        )
    elif TODO_RE.search(rationale):
        rep.err(f"{ipath}.rationale", "still a TODO — write the conclusion before signing it")

    if decided_at is not None and window_end is not None and decided_at < window_end:
        rep.err(
            f"{ipath}.decided_at",
            f"'{decided_at.isoformat()}' is before the window it covers had ended "
            f"({window_end.isoformat()}) — a conclusion about a period cannot be drawn while the "
            "period is still running",
        )

    conclusion = workpaper.get("conclusion")
    horizon = vocab["assurance_days"].get(conclusion)
    if expires_at is not None and window_end is not None:
        if expires_at <= window_end:
            rep.err(
                f"{ipath}.expires_at",
                f"'{expires_at.isoformat()}' is not after the window it covers "
                f"({window_end.isoformat()}) — a conclusion that expires inside its own period never "
                "asserted anything",
            )
        elif horizon is not None:
            days = (expires_at - window_end).days
            if days > horizon:
                rep.err(
                    f"{ipath}.expires_at",
                    f"stands for {days} day(s) after the window closed on {window_end.isoformat()}, "
                    f"and a conclusion of '{conclusion}' may stand for at most {horizon}. The anchor "
                    "is the end of the window and not the signature: signing late does not extend "
                    "what the pack covers",
                )

    if as_of is not None and expires_at is not None and expires_at < as_of:
        rep.err(
            f"{ipath}.expires_at",
            f"this conclusion expired on {expires_at.isoformat()}, before the --as-of date "
            f"{as_of.isoformat()} — what is due is another pack, not another push of this one",
        )

    refs = block.get("refs")
    if refs is not None:
        if not isinstance(refs, list):
            rep.err(f"{ipath}.refs", "must be a list of 'file:line' strings")
        else:
            for i, ref in enumerate(refs):
                if not isinstance(ref, str) or not REF_RE.match(ref):
                    rep.err(f"{ipath}.refs[{i}]", f"'{ref}' is not a 'file:line' citation")


def check_refs(rep, path, workpaper):
    refs = workpaper.get("refs")
    if refs is None:
        rep.err(
            path,
            "missing required `refs` — cite what you read, as 'file:line'. For a whole file the "
            "line is usually 1",
        )
        return
    if not isinstance(refs, list) or len(refs) == 0:
        rep.err(f"{path}.refs", "must be a non-empty list — an uncited conclusion is an error")
        return
    for i, ref in enumerate(refs):
        if not isinstance(ref, str) or not REF_RE.match(ref):
            rep.err(f"{path}.refs[{i}]", f"'{ref}' is not a 'file:line' citation")


def check_workpaper(rep, path, workpaper, vocab, queue, digests, seen_keys, seen_controls,
                    window_end, as_of):
    if not isinstance(workpaper, dict):
        rep.err(path, "workpaper must be a mapping")
        return
    check_unknown_keys(rep, path, workpaper, WORKPAPER_KEYS)

    key = workpaper.get("key")
    if not key or not isinstance(key, str) or not KEY_RE.match(key):
        rep.err(
            f"{path}.key",
            f"'{key}' is not a stable lowercase key (letters, digits, '.', '_', '-')",
        )
    elif key in seen_keys:
        rep.err(f"{path}.key", f"duplicate key '{key}' — every workpaper in a pack needs its own")
    else:
        seen_keys.add(key)

    control_id = workpaper.get("control_id")
    control = None
    if not control_id or not isinstance(control_id, str):
        rep.err(f"{path}.control_id", "missing required `control_id`")
    else:
        normalized = control_id.lower()
        control = queue["controls"].get(normalized)
        if control is None:
            rep.err(
                f"{path}.control_id",
                f"'{control_id}' is not in the queue snapshot — a pack covers the controls Noru put "
                "in scope, and a workpaper about anything else is testing something nobody asked "
                "for" + suggest(normalized, sorted(queue["controls"])),
            )
        elif normalized in seen_controls:
            rep.err(
                f"{path}.control_id",
                f"'{control_id}' already has a workpaper ('{seen_controls[normalized]}') — one "
                "workpaper, one control. Two accounts of the same control is two conclusions an "
                "auditor has to reconcile",
            )
        else:
            seen_controls[normalized] = key

    item_ids = workpaper.get("evidence_item_ids")
    if item_ids is not None:
        if not isinstance(item_ids, list):
            rep.err(f"{path}.evidence_item_ids", "must be a list")
        elif control is not None:
            for item_id in item_ids:
                if item_id not in control["items"]:
                    rep.err(
                        f"{path}.evidence_item_ids",
                        f"'{item_id}' is not an expectation of control '{control_id}' in the queue "
                        "snapshot" + suggest(item_id, sorted(control["items"])),
                    )

    scope = workpaper.get("scope")
    if not scope or not isinstance(scope, str) or len(scope.strip()) < 20:
        rep.err(
            f"{path}.scope",
            "missing or too short — say what was tested and how, in your own words. Do not paste "
            "the framework's procedure here: it is Noru's to serve, and a copy goes stale",
        )
    elif TODO_RE.search(scope):
        rep.err(f"{path}.scope", "still a TODO — say what was tested before concluding anything")

    conclusion = workpaper.get("conclusion")
    if conclusion is None:
        rep.err(f"{path}.conclusion", "missing required `conclusion`")
    elif conclusion not in vocab["conclusion"]:
        rep.err(
            f"{path}.conclusion",
            f"unknown conclusion '{conclusion}'" + suggest(conclusion, vocab["conclusion"]),
        )

    check_inspected(rep, path, workpaper, digests, queue)
    check_sampling(rep, path, workpaper, vocab)
    check_exceptions(rep, path, workpaper, vocab, conclusion)
    check_refs(rep, path, workpaper)
    check_interpretation(rep, path, workpaper, vocab, window_end, as_of)

    if conclusion == "effective" and control is not None and control["expired"]:
        rep.warn(
            f"{path}.conclusion",
            f"the queue snapshot says {len(control['expired'])} record(s) linked to this control "
            "have expired, and this workpaper concludes it was effective. That may be right — the "
            "test is what matters, not the register — but say so in the rationale",
        )

    if workpaper.get("needs_review") is True:
        rep.err(
            f"{path}.needs_review",
            "still true — nobody has tested this control or drawn this conclusion; resolve it and "
            "remove the flag before pushing",
        )


def validate(doc, vocab, as_of=None):
    rep = Report()
    counts = {"controls": 0, "workpapers": 0, "artifacts": 0, "sampled": 0, "exceptions": 0}
    if not isinstance(doc, dict):
        rep.err(
            "<root>",
            "manifest must be a mapping with `version`, `piece`, `source`, `pack`, "
            "`queue_snapshot`, `inputs`, `workpapers`",
        )
        return rep, counts

    check_unknown_keys(rep, "<root>", doc, TOP_LEVEL_KEYS)

    version = doc.get("version")
    if not version or not isinstance(version, str) or not SEMVER_RE.match(version):
        rep.err("version", f"'{version}' is not a semantic version (for example 0.1.0)")
    if doc.get("piece") != PIECE:
        rep.err("piece", f"expected '{PIECE}', found '{doc.get('piece')}'")

    check_source(rep, doc)
    window_end = check_pack(rep, doc)
    queue = check_queue(rep, doc, vocab)
    digests = check_inputs(rep, doc)
    counts["controls"] = len(queue["controls"])
    counts["artifacts"] = len(digests)

    workpapers = doc.get("workpapers")
    if workpapers is None:
        rep.err("workpapers", "missing required `workpapers` (an empty list is a valid answer)")
        workpapers = []
    elif not isinstance(workpapers, list):
        rep.err("workpapers", "must be a list")
        workpapers = []

    seen_keys = set()
    seen_controls = {}
    for i, workpaper in enumerate(workpapers):
        check_workpaper(
            rep, f"workpapers[{i}]", workpaper, vocab, queue, digests, seen_keys, seen_controls,
            window_end, as_of,
        )
        if isinstance(workpaper, dict):
            if isinstance(workpaper.get("exceptions"), list):
                counts["exceptions"] += len(workpaper["exceptions"])
            if workpaper.get("sample") is not None:
                counts["sampled"] += 1
    counts["workpapers"] = len(workpapers)

    # A control in scope with no workpaper is a gap in the pack, not an error: scoping some controls
    # out is a legitimate decision. It has to be a visible one.
    uncovered = sorted(set(queue["controls"]) - set(seen_controls))
    for control_id in uncovered:
        rep.warn(
            "workpapers",
            f"control '{control_id}' is in the pack's scope and has no workpaper — if it was "
            "deliberately left out, say so; if not, this is what an auditor will notice first",
        )

    return rep, counts


USAGE = (
    "usage: validate_manifest.py <manifest.yml> [--as-of=YYYY-MM-DD] [--output=json] [--quiet] "
    "[--emit-parsed=<path.json>]\n"
)


def main(argv):
    output_json = False
    quiet = False
    emit_parsed = None
    as_of = None
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
        elif arg.startswith("--as-of="):
            raw = arg.split("=", 1)[1]
            as_of = parse_date(raw)
            if as_of is None:
                sys.stderr.write(f"error: --as-of='{raw}' is not an ISO date (YYYY-MM-DD)\n")
                return 2
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
        sys.stderr.write(f"error: could not load the bundled vocabulary ({exc})\n")
        return 2

    rep, counts = validate(doc, vocab, as_of)
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
                    "as_of": as_of.isoformat() if as_of else None,
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
            f"\nOK: {counts['workpapers']} workpaper(s) across {counts['controls']} control(s) in "
            f"scope, {counts['sampled']} sampled, {counts['exceptions']} exception(s), built from "
            f"{counts['artifacts']} local input(s) ({len(rep.warnings)} warning(s))."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
