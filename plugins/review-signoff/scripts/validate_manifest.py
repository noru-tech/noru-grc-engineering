#!/usr/bin/env python3
"""Validate .noru/review-signoff.yml against the BUNDLED vocabulary.

Self-contained and atomic:
  * Python standard library only. No pip install, no venv, no network.
  * Review kinds, cadences, cadence windows, dispositions and the queue tool names come from
    ../references/vocabulary.json. There is no framework content in it and there never will be.
  * Parses YAML with PyYAML if it happens to be importable, otherwise a bundled fallback loader.

In this piece the `interpretation` block from contract requirement 8 is not metadata about the
claim — it IS the claim. A review sign-off is exactly "a named person looked at this output on this
date and stands behind it until that one", so the block gets treated as the deliverable:

  * `expires_at` is REQUIRED, not a warning. An attestation with no end date is not a periodic
    review; it is a note.
  * `expires_at` must fall inside the window the declared `cadence` implies. A quarterly review that
    claims a two-year sign-off is not a quarterly review, and nothing else in the file will say so.
  * `decided_at` cannot precede `performed_on`. You cannot sign off a review before you did it.
  * `confirmed + exceptions` must reconcile with `records_reviewed`, and every exception needs a
    disposition and a named owner. A sign-off that does not account for what it covered is a
    signature on an unread page.

Requirement 9 is enforced the same way as in the other pieces: every control and evidence-item id
must appear in the `queue_snapshot` that came from Noru.

`--as-of=YYYY-MM-DD` turns an already-expired sign-off into an error. Leave it off and the file is
judged on its own terms, which is what keeps this validator deterministic; pass it in CI (or before
a release) and a stale attestation fails the build. Nothing here ever reads the clock by itself.

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
PIECE = "review-signoff"

REF_RE = re.compile(r"^[^:\s][^:]*:[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

TOP_LEVEL_KEYS = {"version", "piece", "source", "queue_snapshot", "reviews"}
SOURCE_KEYS = {"slug", "commit_sha", "branch", "generated_by", "derived_digest"}
INTERPRETATION_KEYS = {"owner", "decided_at", "expires_at", "rationale", "refs"}
QUEUE_KEYS = {"fetched_at", "via", "controls"}
QUEUE_CONTROL_KEYS = {
    "control_id", "display_id", "name", "status", "coverage", "unmet_evidence_items",
    "expiring_evidence", "testing_guidance_available",
}
QUEUE_ITEM_KEYS = {"id", "title", "type"}
QUEUE_EXPIRING_KEYS = {"evidence_id", "title", "status", "expires_at"}
REVIEW_KEYS = {
    "key", "kind", "title", "cadence", "performed_on", "supersedes", "input", "outcome",
    "exceptions", "refs", "control_mappings", "interpretation", "needs_review",
}
INPUT_KEYS = {"file", "sha256", "size_bytes", "records_reviewed", "produced_by"}
OUTCOME_KEYS = {"confirmed", "exceptions"}
EXCEPTION_KEYS = {"reference", "disposition", "owner", "note", "resolved_on"}
MAPPING_KEYS = {"control_id", "evidence_item_ids"}

# A TODO left by the collector is a decision nobody has made yet, so it is never publishable.
TODO_RE = re.compile(r"\bTODO\b")


def load_vocabulary():
    return json.loads((REFERENCES / "vocabulary.json").read_text(encoding="utf-8"))


def parse_date(value):
    """None when the value is not an ISO date. The caller decides whether that is an error."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


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


def check_refs(rep, path, obj):
    refs = obj.get("refs")
    if refs is None:
        rep.err(
            path,
            "missing required `refs` — cite the export you reviewed, as 'file:line'. The line is "
            "usually 1: it is the file being attested to, not a source line",
        )
        return
    if not isinstance(refs, list) or len(refs) == 0:
        rep.err(f"{path}.refs", "must be a non-empty list — an uncited attestation is an error")
        return
    for i, ref in enumerate(refs):
        if not isinstance(ref, str) or not REF_RE.match(ref):
            rep.err(f"{path}.refs[{i}]", f"'{ref}' is not a 'file:line' citation")


def check_signoff(rep, path, review, vocab, performed_on, as_of):
    """Requirement 8. Here the interpretation block is the sign-off, so it carries the real rules."""
    block = review.get("interpretation")
    if block is None:
        rep.err(
            path,
            "missing required `interpretation` — this block IS the sign-off: name the person who "
            "reviewed the output, the day they signed, the day the sign-off stops meaning anything, "
            "and what they are standing behind",
        )
        return
    ipath = f"{path}.interpretation"
    if not isinstance(block, dict):
        rep.err(ipath, "must be a mapping")
        return
    check_unknown_keys(rep, ipath, block, INTERPRETATION_KEYS)

    owner = block.get("owner")
    if not owner or not isinstance(owner, str) or len(owner) < 3:
        rep.err(
            f"{ipath}.owner",
            "missing or too short — a sign-off must name the person who did the review, not a team "
            "alias. A team cannot be asked what it was looking at",
        )
    elif TODO_RE.search(owner):
        rep.err(f"{ipath}.owner", "still a TODO — a placeholder cannot attest to anything")

    decided_at = check_date(rep, f"{ipath}.decided_at", block.get("decided_at"), True, "decided_at")

    expires_at = None
    if block.get("expires_at") is None:
        rep.err(
            f"{ipath}.expires_at",
            "missing required `expires_at` — a sign-off with no end date is not a periodic review, "
            "it is a note. Set the day this attestation stops meaning anything",
        )
    else:
        expires_at = check_date(
            rep, f"{ipath}.expires_at", block.get("expires_at"), True, "expires_at"
        )

    rationale = block.get("rationale")
    if not rationale or not isinstance(rationale, str) or len(rationale.strip()) < 10:
        rep.err(
            f"{ipath}.rationale",
            "missing or too short — say what you actually checked and why the result stands",
        )
    elif TODO_RE.search(rationale):
        rep.err(f"{ipath}.rationale", "still a TODO — write what you checked before signing it")

    if decided_at and performed_on and decided_at < performed_on:
        rep.err(
            f"{ipath}.decided_at",
            f"'{decided_at.isoformat()}' is before performed_on '{performed_on.isoformat()}' — a "
            "review cannot be signed off before it was carried out",
        )

    cadence = review.get("cadence")
    if decided_at and expires_at:
        if expires_at <= decided_at:
            rep.err(
                f"{ipath}.expires_at",
                f"'{expires_at.isoformat()}' is not after decided_at '{decided_at.isoformat()}' — "
                "a sign-off cannot expire before it was made",
            )
        elif cadence in vocab["cadence_days"]:
            low, high = vocab["cadence_days"][cadence]
            days = (expires_at - decided_at).days
            if days < low or days > high:
                rep.err(
                    f"{ipath}.expires_at",
                    f"a '{cadence}' review signed on {decided_at.isoformat()} should expire between "
                    f"{low} and {high} days later, but this expires after {days}. Either the cadence "
                    "is wrong or the sign-off is claiming a period nobody agreed to",
                )

    if as_of is not None and expires_at is not None and expires_at < as_of:
        rep.err(
            f"{ipath}.expires_at",
            f"this sign-off expired on {expires_at.isoformat()}, before the --as-of date "
            f"{as_of.isoformat()} — what is due is another review, not another push of this one",
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

        expiring = control.get("expiring_evidence")
        if expiring is not None:
            if not isinstance(expiring, list):
                rep.err(f"{cpath}.expiring_evidence", "must be a list")
            else:
                for j, record in enumerate(expiring):
                    epath = f"{cpath}.expiring_evidence[{j}]"
                    if not isinstance(record, dict):
                        rep.err(epath, "expiring evidence must be a mapping")
                        continue
                    check_unknown_keys(rep, epath, record, QUEUE_EXPIRING_KEYS)
                    if not record.get("evidence_id"):
                        rep.err(f"{epath}.evidence_id", "missing required `evidence_id`")

        known_controls[control_id.lower()] = ids

    return known_controls


def check_input(rep, path, review, seen_digests):
    payload = review.get("input")
    if not isinstance(payload, dict):
        rep.err(
            f"{path}.input",
            "missing required `input` — a sign-off has to say which output it covers and what that "
            "output's bytes were",
        )
        return None
    ipath = f"{path}.input"
    check_unknown_keys(rep, ipath, payload, INPUT_KEYS)

    file_path = payload.get("file")
    if not file_path or not isinstance(file_path, str):
        rep.err(f"{ipath}.file", "missing required `file`")
    elif file_path.startswith("/") or ".." in file_path.split("/"):
        rep.err(
            f"{ipath}.file",
            f"'{file_path}' must be a path inside the repository, not absolute and not traversing "
            "out of it",
        )

    sha = payload.get("sha256")
    if not isinstance(sha, str) or not SHA256_RE.match(sha):
        rep.err(f"{ipath}.sha256", "missing or malformed `sha256` (64 lowercase hex characters)")
    elif sha in seen_digests:
        rep.err(
            f"{ipath}.sha256",
            f"the same export is already signed off as '{seen_digests[sha]}' — one export, one "
            "sign-off; a second one asserts a review that did not happen",
        )
    else:
        seen_digests[sha] = review.get("key")

    size = payload.get("size_bytes")
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
        rep.err(f"{ipath}.size_bytes", "must be a positive integer when present")

    reviewed = payload.get("records_reviewed")
    if not isinstance(reviewed, int) or isinstance(reviewed, bool) or reviewed < 0:
        rep.err(
            f"{ipath}.records_reviewed",
            "missing or negative — say how many records the reviewer actually looked at",
        )
        return None

    produced_by = payload.get("produced_by")
    if isinstance(produced_by, str) and TODO_RE.search(produced_by):
        rep.err(f"{ipath}.produced_by", "still a TODO — say where the export came from")

    return reviewed


def check_outcome(rep, path, review, vocab, reviewed):
    """The arithmetic is the point: a sign-off that does not account for what it covered is a
    signature on an unread page."""
    outcome = review.get("outcome")
    if not isinstance(outcome, dict):
        rep.err(
            f"{path}.outcome",
            "missing required `outcome` — how many records were confirmed, and how many were not",
        )
        return
    opath = f"{path}.outcome"
    check_unknown_keys(rep, opath, outcome, OUTCOME_KEYS)

    counts = {}
    for field in ("confirmed", "exceptions"):
        value = outcome.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            rep.err(f"{opath}.{field}", f"missing or negative `{field}`")
        else:
            counts[field] = value

    exceptions = review.get("exceptions")
    if exceptions is None:
        exceptions = []
    elif not isinstance(exceptions, list):
        rep.err(f"{path}.exceptions", "must be a list")
        exceptions = []

    if len(counts) == 2 and reviewed is not None:
        total = counts["confirmed"] + counts["exceptions"]
        if total != reviewed:
            rep.err(
                opath,
                f"confirmed ({counts['confirmed']}) + exceptions ({counts['exceptions']}) = {total}, "
                f"but records_reviewed is {reviewed}. The sign-off does not account for every record "
                "it claims to cover",
            )

    if "exceptions" in counts and counts["exceptions"] != len(exceptions):
        rep.err(
            f"{path}.exceptions",
            f"outcome.exceptions says {counts['exceptions']} but {len(exceptions)} are listed — an "
            "exception nobody wrote down is one nobody will act on",
        )

    for i, exception in enumerate(exceptions):
        epath = f"{path}.exceptions[{i}]"
        if not isinstance(exception, dict):
            rep.err(epath, "exception must be a mapping")
            continue
        check_unknown_keys(rep, epath, exception, EXCEPTION_KEYS)
        reference = exception.get("reference")
        if not reference or not isinstance(reference, str):
            rep.err(
                f"{epath}.reference",
                "missing required `reference` — say how to find this row again in the export",
            )
        disposition = exception.get("disposition")
        if disposition is None:
            rep.err(
                f"{epath}.disposition",
                "missing required `disposition` — say what was done about it",
            )
        elif disposition not in vocab["disposition"]:
            rep.err(
                f"{epath}.disposition",
                f"unknown disposition '{disposition}'" + suggest(disposition, vocab["disposition"]),
            )
        owner = exception.get("owner")
        if not owner or not isinstance(owner, str) or len(owner) < 3:
            rep.err(
                f"{epath}.owner",
                "missing required `owner` — an exception nobody owns will still be there next "
                "quarter",
            )
        elif TODO_RE.search(owner):
            rep.err(f"{epath}.owner", "still a TODO — name the person who owns this exception")
        check_date(rep, f"{epath}.resolved_on", exception.get("resolved_on"), False, "resolved_on")
        if disposition in ("deferred", "accepted_risk") and not exception.get("note"):
            rep.warn(
                f"{epath}.note",
                f"'{disposition}' with no note — an auditor will ask why, and so will you in three "
                "months",
            )


def check_mappings(rep, path, review, known_controls):
    mappings = review.get("control_mappings")
    if not isinstance(mappings, list) or len(mappings) == 0:
        rep.err(
            f"{path}.control_mappings",
            "missing required `control_mappings` — a sign-off that satisfies nothing does not "
            "belong in the queue",
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
                f"'{control_id}' is not in the queue snapshot — you can only sign off against an "
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


def check_review(rep, path, review, vocab, known_controls, seen_keys, seen_digests, as_of):
    if not isinstance(review, dict):
        rep.err(path, "review must be a mapping")
        return
    check_unknown_keys(rep, path, review, REVIEW_KEYS)

    key = review.get("key")
    if not key or not isinstance(key, str) or not KEY_RE.match(key):
        rep.err(
            f"{path}.key",
            f"'{key}' is not a stable lowercase key (letters, digits, '.', '_', '-')",
        )
    elif key in seen_keys:
        rep.err(
            f"{path}.key",
            f"duplicate key '{key}' — each period is its own review, so keys must be unique",
        )
    else:
        seen_keys.add(key)

    kind = review.get("kind")
    if kind is None:
        rep.err(f"{path}.kind", "missing required `kind`")
    elif kind not in vocab["review_kind"]:
        rep.err(
            f"{path}.kind",
            f"unknown review kind '{kind}'" + suggest(kind, vocab["review_kind"]),
        )

    cadence = review.get("cadence")
    if cadence is None:
        rep.err(
            f"{path}.cadence",
            "missing required `cadence` — without it there is nothing for the expiry to be "
            "consistent with",
        )
    elif cadence not in vocab["cadence"]:
        rep.err(
            f"{path}.cadence",
            f"unknown cadence '{cadence}'" + suggest(cadence, vocab["cadence"]),
        )

    title = review.get("title")
    if not title or not isinstance(title, str):
        rep.err(f"{path}.title", "missing required `title`")

    performed_on = check_date(
        rep, f"{path}.performed_on", review.get("performed_on"), True, "performed_on"
    )

    reviewed = check_input(rep, path, review, seen_digests)
    check_outcome(rep, path, review, vocab, reviewed)
    check_refs(rep, path, review)
    check_mappings(rep, path, review, known_controls)
    check_signoff(rep, path, review, vocab, performed_on, as_of)

    if review.get("needs_review") is True:
        rep.err(
            f"{path}.needs_review",
            "still true — nobody has confirmed this review; resolve it and remove the flag before "
            "pushing",
        )


def validate(doc, vocab, as_of=None):
    rep = Report()
    counts = {"controls": 0, "unmet_items": 0, "reviews": 0, "exceptions": 0}
    if not isinstance(doc, dict):
        rep.err(
            "<root>",
            "manifest must be a mapping with `version`, `piece`, `source`, `queue_snapshot`, "
            "`reviews`",
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

    reviews = doc.get("reviews")
    if reviews is None:
        rep.err("reviews", "missing required `reviews` (an empty list is a valid answer)")
        reviews = []
    elif not isinstance(reviews, list):
        rep.err("reviews", "must be a list")
        reviews = []

    seen_keys = set()
    seen_digests = {}
    for i, review in enumerate(reviews):
        check_review(
            rep, f"reviews[{i}]", review, vocab, known_controls, seen_keys, seen_digests, as_of
        )
        if isinstance(review, dict) and isinstance(review.get("exceptions"), list):
            counts["exceptions"] += len(review["exceptions"])
    counts["reviews"] = len(reviews)

    if counts["unmet_items"] > 0 and counts["reviews"] == 0:
        rep.warn(
            "reviews",
            f"{counts['unmet_items']} unmet expectation(s) in the queue and no review signed off "
            "against them",
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
        sys.stderr.write(f"error: could not load bundled vocabulary ({exc})\n")
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
            f"\nOK: {counts['reviews']} sign-off(s) with {counts['exceptions']} exception(s) "
            f"against {counts['unmet_items']} unmet expectation(s) across {counts['controls']} "
            f"control(s) ({len(rep.warnings)} warning(s))."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
