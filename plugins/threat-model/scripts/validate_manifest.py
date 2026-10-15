#!/usr/bin/env python3
"""Validate .noru/threat-model.yml against the BUNDLED vocabulary.

Self-contained and atomic:
  * Python standard library only. No pip install, no venv, no network.
  * Risk statuses, categories, likelihood and impact values, the per-status expiry horizons and the
    queue tool names come from ../references/vocabulary.json, which contains no framework content
    and never will.
  * Parses YAML with PyYAML if it happens to be importable, otherwise a bundled fallback loader.

What this validator is really for: a threat model lists what someone thought could go wrong. A
register needs more: whether each threat applies as modelled, how likely and how bad it is here,
what is being done about it, who decided, and until when that stands. This file insists on those.

The rules that are specific to this piece:

  * Every risk names the model it came from, and `controls` may only name controls the queue
    snapshot says the organization has. This piece never invents a control.
  * `interpretation.expires_at` is REQUIRED and measured from `observed_on`, the commit date of the
    model it judges. How long it may stand depends on the status: accepting a risk gets longer, never
    for ever, and needs its reasoning written down.
  * Every component another piece found that the model does not name (`undeclared`) needs a
    decision: add it to the model and re-scan, or decide it out of scope and say why.
  * `needs_review: true` blocks the push.

Requirement 9 is enforced the way the other pieces enforce it: nothing may reference something that
is not in the `queue_snapshot` that came from Noru.

`--as-of=YYYY-MM-DD` turns an already-expired judgement into an error. Nothing here reads the clock.

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
import json
import re

_BLOCK_SCALAR_RE = re.compile(r"^[>|](?:[+-]\d*|\d*[+-]?)$")  # >, |, >-, |+, >2, |2-, |-2, …

# The YAML 1.1 boolean spellings PyYAML's SafeLoader resolves, so `ci_gated: yes` is a bool under
# both loaders. Deliberately not `y`/`n`: YAML 1.1 lists them but PyYAML's resolver does not match
# them, so accepting them here would invent a divergence in the other direction. Mixed case
# (`yEs`) is a plain string to PyYAML too, hence exact membership rather than a lowercased compare.
_TRUE_WORDS = frozenset(("true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"))
_FALSE_WORDS = frozenset(("false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"))


def load_yaml(text):
    """Return (document, loader_name).

    The two loaders must agree on types, or a manifest that validates on one machine fails on
    another. PyYAML resolves an unquoted `2026-08-01` to a datetime.date and an unquoted
    timestamp to a datetime.datetime; the fallback loader leaves both as strings. Every date in
    our manifests is an ISO string by contract, so we strip the timestamp resolver rather than
    converting after the fact -- that keeps the author's exact text, which the error messages
    quote back at them.

    Booleans converge the other way round. PyYAML resolves the YAML 1.1 spellings too -- `yes`,
    `no`, `on`, `off` -- and `ci_gated`, `needs_review`, `profiling` and
    `testing_guidance_available` are booleans by contract, not strings, so here it is the fallback
    that learns PyYAML's set (see _TRUE_WORDS). Same rule in both directions: converge on the type
    the contract asks for.
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
    if s in _TRUE_WORDS:
        return True
    if s in _FALSE_WORDS:
        return False
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        if s[0] == '"':
            try:
                return json.loads(s)
            except ValueError:
                pass  # Retain the existing subset behavior for non-JSON YAML escapes.
        if s[0] == "'":
            return s[1:-1].replace("''", "'")
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
PIECE = "threat-model"

REF_RE = re.compile(r"^[^#:\s][^#\s]*(?:#/[^\s]*|:[0-9]+)?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

TOP_LEVEL_KEYS = {"version", "piece", "source", "models", "queue_snapshot", "risks", "undeclared"}
SOURCE_KEYS = {"slug", "commit_sha", "branch", "generated_by", "derived_digest"}
MODEL_KEYS = {"file", "otm_version", "project", "digest"}
QUEUE_KEYS = {"fetched_at", "via", "open_risks", "controls"}
QUEUE_RISK_KEYS = {"id", "external_id", "title", "status"}
QUEUE_CONTROL_KEYS = {"id", "display_id", "name"}
RISK_KEYS = {
    "key", "model", "target", "threat", "otm", "mitigations", "title", "category", "likelihood",
    "impact", "status", "controls", "observed_on", "refs", "interpretation", "needs_review",
}
UNDECLARED_KEYS = {"kind", "key", "source", "refs", "decision", "interpretation", "needs_review"}
INTERPRETATION_KEYS = {"owner", "decided_at", "expires_at", "rationale", "refs"}

DECIDED_TO_LIVE_WITH = ("accepted",)
REASONING_MIN_CHARS = 60
TODO_RE = re.compile(r"\bTODO\b")


def load_vocabulary():
    vocab = json.loads((REFERENCES / "vocabulary.json").read_text(encoding="utf-8"))
    missing = sorted(set(vocab["risk_status"]) - set(vocab["status_horizon_days"]))
    if missing:
        raise ValueError(
            f"status_horizon_days has no entry for {missing} — every status needs an expiry horizon "
            "or the horizon check passes without checking anything"
        )
    return vocab


def parse_date(value):
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


def nonempty(value):
    return isinstance(value, str) and value.strip() != ""


def check_source(rep, doc):
    src = doc.get("source")
    if not isinstance(src, dict):
        rep.err("source", "missing required `source` block (slug, commit_sha, branch, generated_by)")
        return None
    check_unknown_keys(rep, "source", src, SOURCE_KEYS)
    for field in ("slug", "commit_sha", "branch", "generated_by"):
        if not nonempty(src.get(field)):
            rep.err(
                f"source.{field}",
                f"missing required `{field}` — push provenance is not optional (requirement 4)",
            )
    sha = src.get("commit_sha")
    if isinstance(sha, str) and 0 < len(sha) < 7:
        rep.err("source.commit_sha", f"'{sha}' is too short to identify a commit")
    return src.get("slug") if isinstance(src.get("slug"), str) else None


def check_models(rep, doc):
    files = set()
    rows = doc.get("models")
    if not isinstance(rows, list):
        rep.err("models", "missing required `models` — list the threat models this manifest judges")
        return files
    for i, row in enumerate(rows):
        path = f"models[{i}]"
        if not isinstance(row, dict):
            rep.err(path, "model must be a mapping")
            continue
        check_unknown_keys(rep, path, row, MODEL_KEYS)
        if not nonempty(row.get("file")):
            rep.err(f"{path}.file", "missing required `file`")
            continue
        project = row.get("project")
        if not isinstance(project, dict) or not nonempty(project.get("id")):
            rep.err(f"{path}.project", "missing required `project` with an `id`")
        if not isinstance(row.get("digest"), str) or not DIGEST_RE.match(row["digest"]):
            rep.err(f"{path}.digest", "missing or malformed `digest` (sha256:<64 hex>)")
        files.add(row["file"])
    return files


def check_queue(rep, doc, vocab):
    queue = doc.get("queue_snapshot")
    empty = {"controls": set(), "open": {}}
    if not isinstance(queue, dict):
        rep.err(
            "queue_snapshot",
            "missing required `queue_snapshot` — this piece works Noru's queue and must record the "
            "queue it worked (requirement 9)",
        )
        return empty
    check_unknown_keys(rep, "queue_snapshot", queue, QUEUE_KEYS)
    if not queue.get("fetched_at"):
        rep.err("queue_snapshot.fetched_at", "missing required `fetched_at`")
    via = queue.get("via")
    if not isinstance(via, list) or len(via) == 0:
        rep.err("queue_snapshot.via", "missing required `via` — name the Noru tools the snapshot came from")
    else:
        for tool in via:
            if tool not in vocab["queue_tools"]:
                rep.err(
                    "queue_snapshot.via",
                    f"'{tool}' is not a Noru queue tool this piece reads"
                    + suggest(str(tool), vocab["queue_tools"]),
                )
    controls = set()
    for i, row in enumerate(queue.get("controls") or []):
        path = f"queue_snapshot.controls[{i}]"
        if not isinstance(row, dict) or not nonempty(row.get("id")):
            rep.err(path, "control must be a mapping with an `id`")
            continue
        check_unknown_keys(rep, path, row, QUEUE_CONTROL_KEYS)
        controls.add(row["id"])
    open_risks = {}
    for i, row in enumerate(queue.get("open_risks") or []):
        path = f"queue_snapshot.open_risks[{i}]"
        if not isinstance(row, dict) or not nonempty(row.get("external_id")):
            rep.err(path, "open risk must be a mapping with an `external_id`")
            continue
        check_unknown_keys(rep, path, row, QUEUE_RISK_KEYS)
        open_risks[row["external_id"]] = row
    return {"controls": controls, "open": open_risks}


def check_refs(rep, path, refs, model):
    if not isinstance(refs, list) or len(refs) == 0:
        rep.err(f"{path}.refs", "must be a non-empty list — say where in the model this comes from")
        return
    for i, ref in enumerate(refs):
        if not isinstance(ref, str) or not REF_RE.match(ref):
            rep.err(f"{path}.refs[{i}]", f"'{ref}' is not a 'file#/pointer' or 'file:line' citation")
            continue
        cited = re.split(r"[#:]", ref, maxsplit=1)[0]
        if cited.startswith("/") or ".." in cited.split("/"):
            rep.err(f"{path}.refs[{i}]", f"'{cited}' must be a path inside the repository")
        elif model is not None and cited != model:
            rep.warn(
                f"{path}.refs[{i}]",
                f"cites '{cited}' but the risk says it comes from '{model}' — one of the two is out of date",
            )


def check_interpretation(rep, path, item, status, vocab, observed_on, as_of, leaving):
    block = item.get("interpretation")
    if block is None:
        rep.err(
            path,
            "missing required `interpretation` — a threat model cannot decide whether a threat applies "
            "here or how bad it is. Name the person who decided, when, until when, and why",
        )
        return
    ipath = f"{path}.interpretation"
    if not isinstance(block, dict):
        rep.err(ipath, "must be a mapping")
        return
    check_unknown_keys(rep, ipath, block, INTERPRETATION_KEYS)
    owner = block.get("owner")
    if not isinstance(owner, str) or len(owner) < 3:
        rep.err(f"{ipath}.owner", "missing or too short — name the person who decided, not a team alias")
    elif TODO_RE.search(owner):
        rep.err(f"{ipath}.owner", "still a TODO — a placeholder cannot decide anything")
    decided_at = check_date(rep, f"{ipath}.decided_at", block.get("decided_at"), True, "decided_at")
    expires_at = None
    if block.get("expires_at") is None:
        rep.err(
            f"{ipath}.expires_at",
            "missing required `expires_at` — a judgement about a threat goes stale as the system "
            "changes, and nothing else in this file says when somebody has to look again",
        )
    else:
        expires_at = check_date(rep, f"{ipath}.expires_at", block.get("expires_at"), True, "expires_at")
    rationale = block.get("rationale")
    if not isinstance(rationale, str) or len(rationale.strip()) < 10:
        rep.err(f"{ipath}.rationale", "missing or too short — say what you looked at and why this stands")
    elif TODO_RE.search(rationale):
        rep.err(f"{ipath}.rationale", "still a TODO — write the decision before signing it")
    elif leaving and len(rationale.strip()) < REASONING_MIN_CHARS:
        rep.err(
            f"{ipath}.rationale",
            f"this is a decision to live with it ('{status}'), and that is exactly the judgement that "
            "has to be written down. Say what makes it acceptable here, in a sentence somebody can "
            "disagree with",
        )
    if decided_at and observed_on and decided_at < observed_on:
        rep.err(
            f"{ipath}.decided_at",
            f"'{decided_at.isoformat()}' is before observed_on '{observed_on.isoformat()}' — a model "
            "cannot be judged before it was observed",
        )
    if expires_at and decided_at and expires_at <= decided_at:
        rep.err(f"{ipath}.expires_at", "is not after decided_at — a decision cannot expire before it was made")
    horizon = vocab["status_horizon_days"].get(status)
    if expires_at and observed_on and horizon is not None:
        days = (expires_at - observed_on).days
        if days > horizon:
            rep.err(
                f"{ipath}.expires_at",
                f"stands for {days} day(s) after the model was observed on {observed_on.isoformat()}, "
                f"and a judgement in status '{status}' may stand for at most {horizon}",
            )
    if as_of is not None and expires_at is not None and expires_at < as_of:
        rep.err(
            f"{ipath}.expires_at",
            f"this decision expired on {expires_at.isoformat()}, before the --as-of date "
            f"{as_of.isoformat()} — look at the threat again rather than pushing the old judgement",
        )


def check_risk(rep, path, risk, vocab, models, queue, seen, as_of):
    if not isinstance(risk, dict):
        rep.err(path, "risk must be a mapping")
        return
    check_unknown_keys(rep, path, risk, RISK_KEYS)
    key = risk.get("key")
    if not isinstance(key, str) or not KEY_RE.match(key):
        rep.err(f"{path}.key", f"'{key}' is not a stable lowercase key (letters, digits, '.', '_', '-')")
    elif key in seen:
        rep.err(
            f"{path}.key",
            f"duplicate key '{key}' — the key is what the risk's externalId is built from, so two risks "
            "sharing one would overwrite each other on every push",
        )
    else:
        seen.add(key)
    model = risk.get("model")
    if model not in models:
        rep.err(
            f"{path}.model",
            f"'{model}' is not one of the models this manifest lists" + suggest(str(model), sorted(models)),
        )
    target = risk.get("target")
    if not isinstance(target, dict) or target.get("kind") not in vocab["target_kind"] or not nonempty(target.get("id")):
        rep.err(f"{path}.target", "must name the component or dataflow (`kind`, `id`, `name`) the threat is on")
    threat = risk.get("threat")
    if not isinstance(threat, dict) or not nonempty(threat.get("id")):
        rep.err(f"{path}.threat", "must name the threat (`id`, `name`)")
    if not nonempty(risk.get("title")):
        rep.err(f"{path}.title", "missing required `title`")
    for field, allowed, label in (
        ("category", vocab["risk_category"], "category"),
        ("likelihood", vocab["likelihood"], "likelihood"),
        ("impact", vocab["impact"], "impact"),
        ("status", vocab["risk_status"], "status"),
    ):
        value = risk.get(field)
        if value is None:
            rep.err(f"{path}.{field}", f"missing required `{field}`")
        elif isinstance(value, str) and TODO_RE.search(value):
            rep.err(
                f"{path}.{field}",
                f"still a TODO — the model gives no score for this threat, so its {label} here has to be decided",
            )
        elif value not in allowed:
            rep.err(f"{path}.{field}", f"unknown {label} '{value}'" + suggest(str(value), allowed))
    controls = risk.get("controls", [])
    if not isinstance(controls, list):
        rep.err(f"{path}.controls", "must be a list of control ids from the queue snapshot")
    else:
        for i, control in enumerate(controls):
            if control not in queue["controls"]:
                rep.err(
                    f"{path}.controls[{i}]",
                    f"'{control}' is not in the queue snapshot — a risk may only be linked to a "
                    "control the organization already has; this piece never invents one"
                    + suggest(str(control), sorted(queue["controls"])),
                )
    observed_on = check_date(rep, f"{path}.observed_on", risk.get("observed_on"), True, "observed_on")
    check_refs(rep, path, risk.get("refs"), model if isinstance(model, str) else None)
    status = risk.get("status")
    check_interpretation(
        rep, path, risk, status, vocab, observed_on, as_of, status in DECIDED_TO_LIVE_WITH
    )
    if risk.get("needs_review") is True:
        rep.err(
            f"{path}.needs_review",
            "still true — nobody has decided whether this threat applies here; resolve it and remove "
            "the flag before pushing",
        )


def check_undeclared(rep, path, item, vocab, observed_on_default, as_of):
    if not isinstance(item, dict):
        rep.err(path, "undeclared component must be a mapping")
        return
    check_unknown_keys(rep, path, item, UNDECLARED_KEYS)
    if item.get("kind") not in vocab["signal_kind"]:
        rep.err(f"{path}.kind", f"unknown kind '{item.get('kind')}'" + suggest(str(item.get("kind")), vocab["signal_kind"]))
    decision = item.get("decision")
    if decision not in vocab["undeclared_decision"]:
        rep.err(
            f"{path}.decision",
            f"'{decision}' — an undeclared component has to be added to the threat model as a component "
            "(then re-scan, and this entry goes away) or decided `out_of_scope` with a reason",
        )
    check_interpretation(rep, path, item, "out_of_scope", vocab, observed_on_default, as_of, True)
    if item.get("needs_review") is True:
        rep.err(
            f"{path}.needs_review",
            "still true — nobody has decided about this undeclared component; resolve it and remove "
            "the flag before pushing",
        )


def validate(doc, vocab, as_of=None):
    rep = Report()
    counts = {"models": 0, "risks": 0, "undeclared": 0, "open_in_noru": 0, "to_resolve": 0}
    if not isinstance(doc, dict):
        rep.err("<root>", "manifest must be a mapping")
        return rep, counts
    check_unknown_keys(rep, "<root>", doc, TOP_LEVEL_KEYS)
    version = doc.get("version")
    if not isinstance(version, str) or not SEMVER_RE.match(version):
        rep.err("version", f"'{version}' is not a semantic version (for example 0.1.0)")
    if doc.get("piece") != PIECE:
        rep.err("piece", f"expected '{PIECE}', found '{doc.get('piece')}'")
    slug = check_source(rep, doc)
    models = check_models(rep, doc)
    counts["models"] = len(models)
    queue = check_queue(rep, doc, vocab)
    counts["open_in_noru"] = len(queue["open"])

    risks = doc.get("risks")
    if not isinstance(risks, list):
        rep.err("risks", "missing required `risks` (an empty list is valid: the model names no threat)")
        risks = []
    seen = set()
    observed = None
    for i, risk in enumerate(risks):
        check_risk(rep, f"risks[{i}]", risk, vocab, models, queue, seen, as_of)
        if isinstance(risk, dict) and observed is None:
            observed = parse_date(risk.get("observed_on"))
    counts["risks"] = len(risks)

    undeclared = doc.get("undeclared")
    if undeclared is None:
        undeclared = []
    if not isinstance(undeclared, list):
        rep.err("undeclared", "must be a list")
        undeclared = []
    for i, item in enumerate(undeclared):
        check_undeclared(rep, f"undeclared[{i}]", item, vocab, observed, as_of)
    counts["undeclared"] = len(undeclared)

    if slug is not None:
        prefix = f"threat-model:{slug}:"
        for external_id in sorted(queue["open"]):
            if external_id.startswith(prefix) and external_id[len(prefix):] not in seen:
                counts["to_resolve"] += 1
                rep.warn(
                    "queue_snapshot.open_risks",
                    f"'{external_id}' is in Noru and no longer in the model, so :diff will plan to "
                    "resolve it. Taken out of the model is not the same as treated",
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
            parsed_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
            f"\nOK: {counts['risks']} risk(s) from {counts['models']} model(s), "
            f"{counts['undeclared']} undeclared component(s) decided; {counts['to_resolve']} risk(s) "
            f"in Noru no longer modelled ({len(rep.warnings)} warning(s))."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
