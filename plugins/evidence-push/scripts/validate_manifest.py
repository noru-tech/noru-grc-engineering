#!/usr/bin/env python3
"""Validate .noru/evidence-push.yml against the BUNDLED vocabulary.

Self-contained and atomic:
  * Python standard library only. No pip install, no venv, no network.
  * Valid MIME types, the 50MB cap and the queue tool names come from ../references/vocabulary.json,
    which mirrors Noru's own upload contract.
  * Parses YAML with PyYAML if it happens to be importable, otherwise a bundled fallback loader.

Two rules do most of the work here:

  * Contract requirement 8 — every upload carries an `interpretation` block. The claim being
    attributed is "this artifact satisfies this catalogue expectation", which is precisely the
    judgement an auditor asks a named person to stand behind.
  * Contract requirement 9 — every control and evidence-item id an upload maps to must appear in
    the `queue_snapshot` that came from Noru. You cannot map to an expectation Noru did not say
    you had; that is how a plugin starts inventing a catalogue.

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
PIECE = "evidence-push"

REF_RE = re.compile(r"^[^:\s][^:]*:[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

TOP_LEVEL_KEYS = {"version", "piece", "source", "queue_snapshot", "uploads"}
SOURCE_KEYS = {"slug", "commit_sha", "branch", "generated_by", "derived_digest"}
INTERPRETATION_KEYS = {"owner", "decided_at", "expires_at", "rationale", "refs"}
QUEUE_KEYS = {"fetched_at", "via", "controls"}
QUEUE_CONTROL_KEYS = {
    "control_id", "display_id", "name", "status", "coverage",
    "unmet_evidence_items", "testing_guidance_available",
}
QUEUE_ITEM_KEYS = {"id", "title", "type"}
UPLOAD_KEYS = {
    "file", "sha256", "size_bytes", "mime_type", "title", "description", "tags",
    "expiry_date", "control_mappings", "interpretation", "needs_review",
}
MAPPING_KEYS = {"control_id", "evidence_item_ids"}


def load_vocabulary():
    return json.loads((REFERENCES / "vocabulary.json").read_text(encoding="utf-8"))


def check_unknown_keys(rep, path, obj, allowed):
    for key in obj:
        if key not in allowed:
            rep.err(f"{path}.{key}", f"unknown key '{key}'" + suggest(key, allowed))


def check_interpretation(rep, path, obj):
    block = obj.get("interpretation")
    if block is None:
        rep.err(
            path,
            "missing required `interpretation` — name the person who decided this artifact "
            "satisfies the expectation, when, until when, and why",
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

    if block.get("decided_at") is None:
        rep.err(f"{ipath}.decided_at", "missing required `decided_at` (YYYY-MM-DD)")
    for field in ("decided_at", "expires_at"):
        value = block.get(field)
        if value is not None and (not isinstance(value, str) or not DATE_RE.match(value)):
            rep.err(f"{ipath}.{field}", f"'{value}' is not an ISO date (YYYY-MM-DD)")

    rationale = block.get("rationale")
    if not rationale or not isinstance(rationale, str) or len(rationale.strip()) < 10:
        rep.err(
            f"{ipath}.rationale",
            "missing or too short — say why this artifact satisfies the expectation",
        )

    if block.get("expires_at") is None:
        rep.warn(
            f"{ipath}.expires_at",
            "no expiry set; most of this catalogue is periodic, so a review date is usually right",
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
            "missing required `queue_snapshot` — this piece works Noru's queue and must record "
            "the queue it worked (requirement 9)",
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
                f"'{control_id}' is the uppercase display id; store the canonical lowercase "
                "`id` returned by getOrganizationControls",
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


def check_upload(rep, path, upload, vocab, known_controls, seen_digests):
    if not isinstance(upload, dict):
        rep.err(path, "upload must be a mapping")
        return
    check_unknown_keys(rep, path, upload, UPLOAD_KEYS)

    file_path = upload.get("file")
    if not file_path or not isinstance(file_path, str):
        rep.err(f"{path}.file", "missing required `file`")
    elif file_path.startswith("/") or ".." in file_path.split("/"):
        rep.err(
            f"{path}.file",
            f"'{file_path}' must be a path inside the repository, not absolute and not traversing out of it",
        )

    sha = upload.get("sha256")
    if not isinstance(sha, str) or not SHA256_RE.match(sha):
        rep.err(f"{path}.sha256", "missing or malformed `sha256` (64 lowercase hex characters)")
    elif sha in seen_digests:
        rep.err(
            f"{path}.sha256",
            f"the same file content is already uploaded as {seen_digests[sha]} — "
            "uploading it twice creates two evidence records for one artifact",
        )
    else:
        seen_digests[sha] = file_path

    size = upload.get("size_bytes")
    max_bytes = vocab["max_file_bytes"]
    if not isinstance(size, int) or isinstance(size, bool):
        rep.err(f"{path}.size_bytes", "missing or non-integer `size_bytes`")
    elif size <= 0:
        rep.err(f"{path}.size_bytes", "must be greater than zero")
    elif size > max_bytes:
        rep.err(
            f"{path}.size_bytes",
            f"{size} bytes exceeds the {max_bytes // (1024 * 1024)}MB cap on "
            f"{vocab['upload_endpoint']}; Noru will reject it",
        )

    mime = upload.get("mime_type")
    if not mime:
        rep.err(f"{path}.mime_type", "missing required `mime_type`")
    elif mime not in vocab["mime_types"]:
        rep.err(
            f"{path}.mime_type",
            f"'{mime}' is not accepted by {vocab['upload_endpoint']}"
            + suggest(mime, vocab["mime_types"]),
        )

    if not upload.get("title"):
        rep.err(f"{path}.title", "missing required `title`")

    tags = upload.get("tags")
    if tags is not None and not isinstance(tags, list):
        rep.err(f"{path}.tags", "must be a list of strings")

    expiry = upload.get("expiry_date")
    if expiry is not None and (not isinstance(expiry, str) or len(expiry) < 10):
        rep.err(f"{path}.expiry_date", f"'{expiry}' is not an ISO 8601 date or datetime")

    mappings = upload.get("control_mappings")
    if not isinstance(mappings, list) or len(mappings) == 0:
        rep.err(
            f"{path}.control_mappings",
            "missing required `control_mappings` — an artifact that satisfies nothing does not "
            "belong in the queue",
        )
        mappings = []
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
                f"'{control_id}' is not in the queue snapshot — you can only satisfy an "
                "expectation Noru said you had"
                + suggest(normalized, sorted(known_controls)),
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
                    "queue snapshot"
                    + suggest(item_id, sorted(known_controls[normalized])),
                )

    check_interpretation(rep, path, upload)

    if upload.get("needs_review") is True:
        rep.err(
            f"{path}.needs_review",
            "still true — a human has not confirmed this artifact satisfies the expectation; "
            "resolve it and remove the flag before pushing",
        )


def validate(doc, vocab):
    rep = Report()
    counts = {"controls": 0, "unmet_items": 0, "uploads": 0}
    if not isinstance(doc, dict):
        rep.err(
            "<root>",
            "manifest must be a mapping with `version`, `piece`, `source`, `queue_snapshot`, `uploads`",
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

    uploads = doc.get("uploads")
    if uploads is None:
        rep.err("uploads", "missing required `uploads` (an empty list is a valid answer)")
        uploads = []
    elif not isinstance(uploads, list):
        rep.err("uploads", "must be a list")
        uploads = []

    seen_digests = {}
    for i, upload in enumerate(uploads):
        check_upload(rep, f"uploads[{i}]", upload, vocab, known_controls, seen_digests)
    counts["uploads"] = len(uploads)

    if counts["unmet_items"] > 0 and counts["uploads"] == 0:
        rep.warn(
            "uploads",
            f"{counts['unmet_items']} unmet expectation(s) in the queue and nothing staged to "
            "satisfy them",
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
            f"\nOK: {counts['uploads']} upload(s) against {counts['unmet_items']} unmet "
            f"expectation(s) across {counts['controls']} control(s) "
            f"({len(rep.warnings)} warning(s))."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
