#!/usr/bin/env python3
"""Validate .noru/iac-scan.yml against the BUNDLED vocabulary and rule set.

Self-contained and atomic:
  * Python standard library only. No pip install, no venv, no network.
  * Technologies, severities, statuses, categories, the per-status expiry horizons and the queue
    tool names come from ../references/vocabulary.json; the rules come from ../references/checks.json.
    Neither file contains framework content and neither ever will.
  * Parses YAML with PyYAML if it happens to be importable, otherwise a bundled fallback loader.

What this validator is really for: a scanner produces a list, and a list is not a finding. Somebody
has to say whether each rule that fired means anything in this environment, how bad it is here, and
until when that judgement stands. Those three answers are what this file insists on.

The rules that are specific to this piece:

  * `interpretation.expires_at` is REQUIRED, and is measured from `observed_on` rather than from
    `decided_at`. A finding observed in March and signed in August is a claim about March's
    configuration however recent the signature is, and anchoring on the signature would let a stale
    observation be renewed forever without anybody looking at the configuration again.
  * How far it may stand depends on the status. An `open` finding is a live observation the next
    scan refreshes. `accepted` and `false_positive` are decisions to leave the configuration alone;
    they get a longer horizon and a hard requirement that the reasoning is actually written down,
    because an acceptance nobody revisits is how a known misconfiguration becomes permanent.
  * `decided_at` cannot precede `observed_on`. You cannot judge a configuration before you saw it.
  * `asset_external_id` and `risk_id` may only name things the queue snapshot says the organization
    already has. This piece never creates an asset and never opens a risk.
  * A citation has to point at the file the finding says it is about. The citation is the whole
    evidence: no matched line text is ever recorded, because one of the rules fires on a line that
    holds a credential.

Requirement 9 is enforced the way the other pieces enforce it: nothing may reference something that
is not in the `queue_snapshot` that came from Noru.

`--as-of=YYYY-MM-DD` turns an already-expired judgement into an error. Leave it off and the file is
judged on its own terms, which is what keeps this validator deterministic; pass it in CI and a
decision nobody has revisited fails the build. Nothing here ever reads the clock by itself.

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
PIECE = "iac-scan"

# The `source` every finding this piece lands carries in Noru. Findings are keyed on
# (source, externalId), so this string is half of every identity the piece owns. The queue snapshot
# has to have been fetched with the same filter, or the "no longer reproducing" set is somebody
# else's findings and closing against it would close records this piece never wrote.
FINDING_SOURCE = "iac-scan"

REF_RE = re.compile(r"^[^:\s][^:]*:[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

TOP_LEVEL_KEYS = {"version", "piece", "source", "queue_snapshot", "findings"}
SOURCE_KEYS = {"slug", "commit_sha", "branch", "generated_by", "derived_digest"}
INTERPRETATION_KEYS = {"owner", "decided_at", "expires_at", "rationale", "refs"}
QUEUE_KEYS = {"fetched_at", "via", "source", "open_findings", "assets", "risks"}
QUEUE_OPEN_KEYS = {"external_id", "check_name", "title", "severity", "status", "category"}
QUEUE_ASSET_KEYS = {"id", "external_id", "name"}
QUEUE_RISK_KEYS = {"id", "title"}
FINDING_KEYS = {
    "key", "check", "technology", "severity", "category", "status", "title", "file", "resource",
    "observed_on", "refs", "asset_external_id", "risk_id", "owner_email", "interpretation",
    "needs_review",
}

# Deciding to leave a misconfiguration in place is the judgement that most needs its reasoning
# written down, and the one most likely to be a shrug. A sentence is not much to ask.
DECIDED_TO_LEAVE = ("accepted", "false_positive")
REASONING_MIN_CHARS = 60

# A TODO left by the collector is a decision nobody has made yet, so it is never publishable.
TODO_RE = re.compile(r"\bTODO\b")


def load_vocabulary():
    vocab = json.loads((REFERENCES / "vocabulary.json").read_text(encoding="utf-8"))
    # A status with no horizon would skip the expiry check silently, which is the one failure mode
    # this validator cannot afford: the check would still report success. A broken bundle is a
    # setup error, not a finding about the manifest.
    missing = sorted(set(vocab["finding_status"]) - set(vocab["status_horizon_days"]))
    if missing:
        raise ValueError(
            f"status_horizon_days has no entry for {missing} — every status needs an expiry horizon "
            "or the horizon check passes without checking anything"
        )
    return vocab


def load_checks():
    """The bundled rules, by id. Rules describe configuration; there is no framework content here."""
    data = json.loads((REFERENCES / "checks.json").read_text(encoding="utf-8"))
    return {check["id"]: check for check in data["checks"]}


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


def check_source(rep, doc):
    src = doc.get("source")
    if not isinstance(src, dict):
        rep.err("source", "missing required `source` block (slug, commit_sha, branch, generated_by)")
        return None
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
    slug = src.get("slug")
    return slug if isinstance(slug, str) else None


def check_queue(rep, doc, vocab):
    """Requirement 9. What Noru already held, recorded so the plan can be argued with later."""
    queue = doc.get("queue_snapshot")
    empty = {"open": {}, "assets": set(), "risks": set()}
    if not isinstance(queue, dict):
        rep.err(
            "queue_snapshot",
            "missing required `queue_snapshot` — this piece works Noru's queue and must record the "
            "queue it worked (requirement 9). Without the open set a re-scan cannot tell which "
            "findings should now be closed",
        )
        return empty
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
                    f"'{tool}' is not a Noru queue tool this piece reads"
                    + suggest(tool, vocab["queue_tools"]),
                )

    if queue.get("source") != FINDING_SOURCE:
        rep.err(
            "queue_snapshot.source",
            f"expected '{FINDING_SOURCE}', found '{queue.get('source')}' — the open set has to have "
            "been fetched for this piece's own source, or closing against it would close findings "
            "this piece never wrote",
        )

    open_findings = {}
    rows = queue.get("open_findings")
    if rows is None:
        rep.err(
            "queue_snapshot.open_findings",
            "missing required `open_findings` (an empty list is a valid answer and means nothing "
            "has been landed yet)",
        )
        rows = []
    elif not isinstance(rows, list):
        rep.err("queue_snapshot.open_findings", "must be a list")
        rows = []
    for i, row in enumerate(rows):
        opath = f"queue_snapshot.open_findings[{i}]"
        if not isinstance(row, dict):
            rep.err(opath, "open finding must be a mapping")
            continue
        check_unknown_keys(rep, opath, row, QUEUE_OPEN_KEYS)
        external_id = row.get("external_id")
        if not external_id or not isinstance(external_id, str):
            rep.err(f"{opath}.external_id", "missing required `external_id`")
            continue
        # Closing a finding means sending the whole record back, so anything the upsert requires has
        # to be in the snapshot. A snapshot missing them cannot be reconciled from.
        for field, allowed in (
            ("check_name", None),
            ("title", None),
            ("severity", vocab["severity"]),
            ("status", vocab["finding_status"]),
            ("category", vocab["category"]),
        ):
            value = row.get(field)
            if value is None or not isinstance(value, str) or value == "":
                rep.err(
                    f"{opath}.{field}",
                    f"missing required `{field}` — closing a finding sends the whole record back, "
                    "so the snapshot has to carry every field the upsert requires",
                )
            elif allowed is not None and value not in allowed:
                rep.err(f"{opath}.{field}", f"unknown {field} '{value}'" + suggest(value, allowed))
        open_findings[external_id] = row

    assets = set()
    rows = queue.get("assets")
    if rows is None:
        rep.err("queue_snapshot.assets", "missing required `assets` (an empty list is valid)")
        rows = []
    elif not isinstance(rows, list):
        rep.err("queue_snapshot.assets", "must be a list")
        rows = []
    for i, row in enumerate(rows):
        apath = f"queue_snapshot.assets[{i}]"
        if not isinstance(row, dict):
            rep.err(apath, "asset must be a mapping")
            continue
        check_unknown_keys(rep, apath, row, QUEUE_ASSET_KEYS)
        if not row.get("id"):
            rep.err(f"{apath}.id", "missing required `id`")
        if isinstance(row.get("external_id"), str) and row["external_id"] != "":
            assets.add(row["external_id"])

    risks = set()
    rows = queue.get("risks")
    if rows is None:
        rep.err("queue_snapshot.risks", "missing required `risks` (an empty list is valid)")
        rows = []
    elif not isinstance(rows, list):
        rep.err("queue_snapshot.risks", "must be a list")
        rows = []
    for i, row in enumerate(rows):
        rpath = f"queue_snapshot.risks[{i}]"
        if not isinstance(row, dict):
            rep.err(rpath, "risk must be a mapping")
            continue
        check_unknown_keys(rep, rpath, row, QUEUE_RISK_KEYS)
        if not row.get("id"):
            rep.err(f"{rpath}.id", "missing required `id`")
        else:
            risks.add(row["id"])

    return {"open": open_findings, "assets": assets, "risks": risks}


def check_refs(rep, path, finding):
    refs = finding.get("refs")
    if refs is None:
        rep.err(
            path,
            "missing required `refs` — say where the rule fired, as 'file:line'. The line is the "
            "whole evidence: this piece never copies what it matched",
        )
        return
    if not isinstance(refs, list) or len(refs) == 0:
        rep.err(f"{path}.refs", "must be a non-empty list — an uncited finding is an error")
        return
    declared = finding.get("file")
    for i, ref in enumerate(refs):
        if not isinstance(ref, str) or not REF_RE.match(ref):
            rep.err(f"{path}.refs[{i}]", f"'{ref}' is not a 'file:line' citation")
            continue
        cited = ref.rsplit(":", 1)[0]
        if cited.startswith("/") or ".." in cited.split("/"):
            rep.err(
                f"{path}.refs[{i}]",
                f"'{cited}' must be a path inside the repository, not absolute and not traversing "
                "out of it",
            )
        elif isinstance(declared, str) and declared != "" and cited != declared:
            # A warning and not an error: the citation is what a reader opens, so a mismatch is
            # worth saying out loud, but a manifest whose paths were rewritten wholesale (a move, a
            # rename, a test harness re-pointing them) is still a manifest somebody can read.
            rep.warn(
                f"{path}.refs[{i}]",
                f"cites '{cited}' but the finding says it is about '{declared}' — one of the two is "
                "out of date, and the citation is the half a reader can actually open",
            )


def check_interpretation(rep, path, finding, vocab, observed_on, as_of):
    """Requirement 8, anchored on when the configuration was seen rather than on when it was signed."""
    block = finding.get("interpretation")
    if block is None:
        rep.err(
            path,
            "missing required `interpretation` — a scanner cannot decide whether a rule that fired "
            "is real in your environment. Name the person who decided, the day they decided, the "
            "day the decision stops being current, and what they looked at",
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
            "missing or too short — name the person who decided, not a team alias. A team cannot "
            "be asked what it was looking at",
        )
    elif TODO_RE.search(owner):
        rep.err(f"{ipath}.owner", "still a TODO — a placeholder cannot decide anything")

    decided_at = check_date(rep, f"{ipath}.decided_at", block.get("decided_at"), True, "decided_at")

    expires_at = None
    if block.get("expires_at") is None:
        rep.err(
            f"{ipath}.expires_at",
            "missing required `expires_at` — a judgement about a configuration goes stale when the "
            "configuration changes, and nothing else in this file says when somebody has to look "
            "again",
        )
    else:
        expires_at = check_date(
            rep, f"{ipath}.expires_at", block.get("expires_at"), True, "expires_at"
        )

    status = finding.get("status")
    rationale = block.get("rationale")
    if not rationale or not isinstance(rationale, str) or len(rationale.strip()) < 10:
        rep.err(
            f"{ipath}.rationale",
            "missing or too short — say what you looked at and why this stands",
        )
    elif TODO_RE.search(rationale):
        rep.err(f"{ipath}.rationale", "still a TODO — write the decision before signing it")
    elif status in DECIDED_TO_LEAVE and len(rationale.strip()) < REASONING_MIN_CHARS:
        rep.err(
            f"{ipath}.rationale",
            f"status is '{status}', so this is a decision to leave the configuration as it is — "
            "accepting a misconfiguration, or calling it a false positive, is exactly the judgement "
            "that has to be written down. Say what makes it safe here, in a sentence somebody can "
            "disagree with",
        )

    if decided_at is not None and observed_on is not None and decided_at < observed_on:
        rep.err(
            f"{ipath}.decided_at",
            f"'{decided_at.isoformat()}' is before observed_on '{observed_on.isoformat()}' — a "
            "configuration cannot be judged before it was observed",
        )

    if expires_at is not None and decided_at is not None and expires_at <= decided_at:
        rep.err(
            f"{ipath}.expires_at",
            f"'{expires_at.isoformat()}' is not after decided_at '{decided_at.isoformat()}' — a "
            "decision cannot expire before it was made",
        )

    horizon = vocab["status_horizon_days"].get(status)
    if expires_at is not None and observed_on is not None and horizon is not None:
        days = (expires_at - observed_on).days
        if days > horizon:
            rep.err(
                f"{ipath}.expires_at",
                f"stands for {days} day(s) after the configuration was observed on "
                f"{observed_on.isoformat()}, and a finding in status '{status}' may stand for at "
                f"most {horizon}. The anchor is observed_on and not decided_at on purpose: signing "
                "late does not make an old observation current",
            )

    if as_of is not None and expires_at is not None and expires_at < as_of:
        rep.err(
            f"{ipath}.expires_at",
            f"this decision expired on {expires_at.isoformat()}, before the --as-of date "
            f"{as_of.isoformat()} — re-scan and decide again rather than pushing the old judgement",
        )

    refs = block.get("refs")
    if refs is not None:
        if not isinstance(refs, list):
            rep.err(f"{ipath}.refs", "must be a list of 'file:line' strings")
        else:
            for i, ref in enumerate(refs):
                if not isinstance(ref, str) or not REF_RE.match(ref):
                    rep.err(f"{ipath}.refs[{i}]", f"'{ref}' is not a 'file:line' citation")


def check_finding(rep, path, finding, vocab, checks, queue, seen_keys, as_of):
    if not isinstance(finding, dict):
        rep.err(path, "finding must be a mapping")
        return
    check_unknown_keys(rep, path, finding, FINDING_KEYS)

    key = finding.get("key")
    if not key or not isinstance(key, str) or not KEY_RE.match(key):
        rep.err(
            f"{path}.key",
            f"'{key}' is not a stable lowercase key (letters, digits, '.', '_', '-')",
        )
    elif key in seen_keys:
        rep.err(
            f"{path}.key",
            f"duplicate key '{key}' — the key is what the upsert is addressed by, so two findings "
            "sharing one would overwrite each other on every push",
        )
    else:
        seen_keys.add(key)

    check_id = finding.get("check")
    rule = None
    if check_id is None:
        rep.err(f"{path}.check", "missing required `check`")
    elif check_id not in checks:
        rep.err(
            f"{path}.check",
            f"unknown check '{check_id}' — it is not one of the bundled rules"
            + suggest(check_id, sorted(checks)),
        )
    else:
        rule = checks[check_id]

    technology = finding.get("technology")
    if technology is None:
        rep.err(f"{path}.technology", "missing required `technology`")
    elif technology not in vocab["technology"]:
        rep.err(
            f"{path}.technology",
            f"unknown technology '{technology}'" + suggest(technology, vocab["technology"]),
        )
    elif rule is not None and rule["technology"] != technology:
        rep.err(
            f"{path}.technology",
            f"'{check_id}' only fires on {rule['technology']} configuration, but this finding says "
            f"'{technology}'. One of the two is wrong, and the scan does not produce this pairing",
        )

    for field, allowed in (
        ("severity", vocab["severity"]),
        ("category", vocab["category"]),
        ("status", vocab["finding_status"]),
    ):
        value = finding.get(field)
        if value is None:
            rep.err(f"{path}.{field}", f"missing required `{field}`")
        elif value not in allowed:
            rep.err(f"{path}.{field}", f"unknown {field} '{value}'" + suggest(value, allowed))

    if not finding.get("title") or not isinstance(finding.get("title"), str):
        rep.err(f"{path}.title", "missing required `title`")

    file_path = finding.get("file")
    if not file_path or not isinstance(file_path, str):
        rep.err(f"{path}.file", "missing required `file`")
    elif file_path.startswith("/") or ".." in file_path.split("/"):
        rep.err(
            f"{path}.file",
            f"'{file_path}' must be a path inside the repository, not absolute and not traversing "
            "out of it",
        )

    resource = finding.get("resource")
    if resource is not None and not isinstance(resource, str):
        rep.err(f"{path}.resource", "must be a string or null")

    observed_on = check_date(
        rep, f"{path}.observed_on", finding.get("observed_on"), True, "observed_on"
    )

    asset = finding.get("asset_external_id")
    if asset is not None:
        if not isinstance(asset, str) or asset == "":
            rep.err(f"{path}.asset_external_id", "must be a non-empty string when present")
        elif asset not in queue["assets"]:
            rep.err(
                f"{path}.asset_external_id",
                f"'{asset}' is not in the queue snapshot — a finding may only be attached to an "
                "asset the organization already has, because this piece cannot know whether the "
                "thing a configuration block describes is the thing the register already holds"
                + suggest(asset, sorted(queue["assets"])),
            )

    risk = finding.get("risk_id")
    if risk is not None:
        if not isinstance(risk, str) or risk == "":
            rep.err(f"{path}.risk_id", "must be a non-empty string when present")
        elif risk not in queue["risks"]:
            rep.err(
                f"{path}.risk_id",
                f"'{risk}' is not in the queue snapshot — filing a finding against a risk the "
                "organization does not carry would invent a register entry"
                + suggest(risk, sorted(queue["risks"])),
            )

    owner_email = finding.get("owner_email")
    if owner_email is not None and (not isinstance(owner_email, str) or len(owner_email) < 3):
        rep.err(f"{path}.owner_email", "must be a non-empty string when present")

    check_refs(rep, path, finding)
    check_interpretation(rep, path, finding, vocab, observed_on, as_of)

    if finding.get("needs_review") is True:
        rep.err(
            f"{path}.needs_review",
            "still true — nobody has decided whether this rule firing means anything here; resolve "
            "it and remove the flag before pushing",
        )


def validate(doc, vocab, checks, as_of=None):
    rep = Report()
    counts = {"findings": 0, "open_in_noru": 0, "to_close": 0, "configuration_files": 0}
    if not isinstance(doc, dict):
        rep.err(
            "<root>",
            "manifest must be a mapping with `version`, `piece`, `source`, `queue_snapshot`, "
            "`findings`",
        )
        return rep, counts

    check_unknown_keys(rep, "<root>", doc, TOP_LEVEL_KEYS)

    version = doc.get("version")
    if not version or not isinstance(version, str) or not SEMVER_RE.match(version):
        rep.err("version", f"'{version}' is not a semantic version (for example 0.1.0)")
    if doc.get("piece") != PIECE:
        rep.err("piece", f"expected '{PIECE}', found '{doc.get('piece')}'")

    slug = check_source(rep, doc)
    queue = check_queue(rep, doc, vocab)
    counts["open_in_noru"] = len(queue["open"])

    findings = doc.get("findings")
    if findings is None:
        rep.err(
            "findings",
            "missing required `findings` (an empty list is a valid answer — it means the rules "
            "found nothing, and :diff will still close what no longer reproduces)",
        )
        findings = []
    elif not isinstance(findings, list):
        rep.err("findings", "must be a list")
        findings = []

    seen_keys = set()
    for i, finding in enumerate(findings):
        check_finding(rep, f"findings[{i}]", finding, vocab, checks, queue, seen_keys, as_of)
    counts["findings"] = len(findings)
    counts["configuration_files"] = len(
        {f.get("file") for f in findings if isinstance(f, dict) and isinstance(f.get("file"), str)}
    )

    # What :diff will close. Not an error — closing a finding whose rule stopped firing is the point
    # of re-running — but the reviewer should meet it here rather than in the plan.
    if slug is not None:
        mine = f"{slug}:"
        for external_id in sorted(queue["open"]):
            if not external_id.startswith(mine):
                continue
            if external_id[len(mine):] in seen_keys:
                continue
            counts["to_close"] += 1
            rep.warn(
                "queue_snapshot.open_findings",
                f"'{external_id}' is open in Noru and no rule reproduced it here, so :diff will "
                "plan to resolve it. If that is wrong, the rule changed and not the configuration",
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
        checks = load_checks()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"error: could not load the bundled vocabulary or rules ({exc})\n")
        return 2

    rep, counts = validate(doc, vocab, checks, as_of)
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
            f"\nOK: {counts['findings']} finding(s) across {counts['configuration_files']} "
            f"configuration file(s); {counts['open_in_noru']} already open in Noru, "
            f"{counts['to_close']} of which no longer reproduce ({len(rep.warnings)} warning(s))."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
