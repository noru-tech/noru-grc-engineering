#!/usr/bin/env python3
"""Validate .noru/dev-agents.yml against the BUNDLED vocabulary.

Self-contained and atomic: Python standard library only, no pip install, no network.

The manifest inventories the development process's suppliers: the coding agents that wrote changes
(from an agent-change-control manifest) and the MCP servers and plugins the repository's agent
configuration declares. The facts are the collector's; the claims are the suppliers — which
organization each one is — and the attestation that a person reviewed the inventory. Every claim
carries refs[] and an interpretation block, and an unattributed claim is an ERROR (requirement 8).

Two things are refused wherever they appear, because the manifest is committed and its content is
pushed: per-person agent usage (who ran which agent), and anything from a configuration file that
can carry a credential (env, headers, args, URLs).

Usage:
    python3 validate_manifest.py <manifest.yml> [--output=json] [--quiet] [--emit-parsed=<path>]
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
PIECE = "dev-agents"

REF_RE = re.compile(r"^[^:\s][^:]*:[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SPECIAL = ("(variable)", "(unrecognised)")
COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
HOST_RE = re.compile(r"^[a-z0-9.-]+$|^\[[0-9a-f:.]+\]$")

TOP_LEVEL_KEYS = {
    "version", "piece", "source", "window", "changes", "agents", "mcp_servers", "plugins",
    "config_problems", "suppliers", "queue_snapshot", "control_mappings", "attestation",
}
SOURCE_KEYS = {
    "slug", "commit_sha", "branch", "generated_by", "input", "acp_version", "acc_manifest_sha256",
    "derived_digest",
}
WINDOW_KEYS = {"opens_on", "closes_on", "complete"}
CHANGES_KEYS = {"total", "agent_authored", "refs"}
AGENT_KEYS = {"agent", "actor_id", "vendor", "changes_authored", "reviews", "supplier", "refs"}
SERVER_KEYS = {"key", "file", "tool", "name", "transport", "command", "host", "supplier", "refs"}
PLUGIN_KEYS = {
    "key", "file", "tool", "plugin", "marketplace", "marketplace_source", "marketplace_repo",
    "marketplace_host", "supplier", "refs",
}
PROBLEM_KEYS = {"message", "refs"}
SUPPLIER_KEYS = {
    "key", "vendor_name", "category", "website", "refs", "needs_review", "interpretation",
}
ATTESTATION_KEYS = {"refs", "needs_review", "interpretation"}
QUEUE_KEYS = {"fetched_at", "via", "controls"}
QUEUE_CONTROL_KEYS = {"control_id", "display_id", "name"}
MAPPING_KEYS = {"control_id"}
INTERPRETATION_KEYS = {"owner", "decided_at", "expires_at", "rationale", "refs"}

# What a configuration file can hold that this manifest never records, because any of them can
# carry a credential: an environment block, an Authorization header, an `--api-key=` argument, a
# token in a URL's query string or user-info.
NEVER_RECORDED = {"env", "envFile", "headers", "args", "url", "httpUrl", "cwd", "inputs"}
# Who ran an agent, counted per person, is employee activity data. It is never in this manifest.
PER_PERSON_KEYS = {
    "operator", "operators", "agent_operator", "operator_counts", "by_operator", "operated_by",
}

# A supplier claim is about which organization stands behind something now, not about a period, so
# it is anchored on the day a person decided it. The attestation is about the window, so it is
# anchored on the window's end, like change-control: an account of July signed in December does not
# cover more of July for being signed late. Both get the same ceiling.
HORIZON_DAYS = 400


def load_vocabulary():
    return json.loads((REFERENCES / "vocabulary.json").read_text(encoding="utf-8"))


def days_between(later, earlier):
    return (datetime.date.fromisoformat(later) - datetime.date.fromisoformat(earlier)).days


def is_date(value):
    if not isinstance(value, str) or not DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_unknown_keys(rep, path, obj, allowed):
    for key in obj:
        if key in allowed or key in PER_PERSON_KEYS:
            continue
        if key in NEVER_RECORDED:
            rep.err(
                f"{path}.{key}",
                f"`{key}` is never recorded — a configuration's environment, headers, arguments "
                "and URLs can hold a credential. Record the command's name or the host only",
            )
            continue
        rep.err(f"{path}.{key}", f"unknown key '{key}'" + suggest(key, allowed))


def check_no_person_data(rep, node, path="<root>"):
    """Refuse per-person agent usage anywhere in the manifest, under any of its usual names."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path != "<root>" else key
            if key in PER_PERSON_KEYS:
                rep.err(
                    child,
                    "per-person agent usage is employee activity data, and this manifest never "
                    "records who ran an agent. `--operator-counts` writes it to "
                    ".noru/.cache/dev-agents.operators.json, which is neither committed nor pushed",
                )
            check_no_person_data(rep, value, child)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            check_no_person_data(rep, item, f"{path}[{i}]")


def check_person(rep, path, value, hint):
    if not isinstance(value, str) or len(value.strip()) < 3:
        rep.err(path, f"missing or too short — {hint}")
        return None
    if "TODO" in value:
        rep.err(path, f"'{value}' is still a TODO — {hint}")
        return None
    return value.strip()


def check_refs(rep, path, obj):
    refs = obj.get("refs")
    if refs is None:
        rep.err(path, "missing required `refs` — every claim must cite the lines that produced it")
        return
    if not isinstance(refs, list) or len(refs) == 0:
        rep.err(f"{path}.refs", "must be a non-empty list — an unattributed claim is an error")
        return
    for i, ref in enumerate(refs):
        if not isinstance(ref, str) or not REF_RE.match(ref):
            rep.err(f"{path}.refs[{i}]", f"'{ref}' is not a 'file:line' citation")


def check_needs_review(rep, path, obj):
    if obj.get("needs_review") is True:
        rep.err(
            f"{path}.needs_review",
            "still true — a person has not decided this yet; decide it, sign for it, and remove "
            "the flag before pushing",
        )


def check_interpretation(rep, path, obj, anchor=None, anchor_label=None):
    """Requirement 8, with `expires_at` mandatory and a 400-day ceiling.

    `anchor` is the date the horizon is measured from: the window's end for the attestation, and
    `decided_at` for a supplier. Nothing here reads the clock; staleness is CI mode's expiry step.
    """
    block = obj.get("interpretation")
    if block is None:
        rep.err(
            path,
            "missing required `interpretation` — name the person who decided this, when, "
            "until when, and why",
        )
        return
    ipath = f"{path}.interpretation"
    if not isinstance(block, dict):
        rep.err(ipath, "must be a mapping")
        return
    check_unknown_keys(rep, ipath, block, INTERPRETATION_KEYS)
    check_person(rep, f"{ipath}.owner", block.get("owner"), "must name a person, not a team alias")

    for field in ("decided_at", "expires_at"):
        value = block.get(field)
        if value is None:
            rep.err(
                f"{ipath}.{field}",
                "missing required `expires_at` — a supplier decision that never lapses is one "
                "nobody will ever revisit"
                if field == "expires_at"
                else "missing required `decided_at` (YYYY-MM-DD)",
            )
        elif not is_date(value):
            rep.err(f"{ipath}.{field}", f"'{value}' is not an ISO date (YYYY-MM-DD)")

    rationale = block.get("rationale")
    if not rationale or not isinstance(rationale, str) or len(rationale.strip()) < 10:
        rep.err(f"{ipath}.rationale", "missing or too short — say why this claim holds")
    elif "TODO" in rationale:
        rep.err(f"{ipath}.rationale", "is still a TODO — say why this claim holds")

    decided, expires = block.get("decided_at"), block.get("expires_at")
    if not is_date(decided):
        return
    if anchor and days_between(decided, anchor) < 0:
        rep.err(
            f"{ipath}.decided_at",
            f"{decided} is before the window closed on {anchor} — a conclusion about a period "
            "cannot be drawn while the period is still running",
        )
    if not is_date(expires):
        return
    start = anchor or decided
    if days_between(expires, decided) <= 0:
        rep.err(f"{ipath}.expires_at", f"{expires} is not after decided_at {decided}")
        return
    span = days_between(expires, start)
    if span > HORIZON_DAYS:
        rep.err(
            f"{ipath}.expires_at",
            f"{expires} is {span} days after {anchor_label or 'decided_at'}, past the "
            f"{HORIZON_DAYS}-day horizon",
        )


def check_source(rep, doc, vocab):
    src = doc.get("source")
    if not isinstance(src, dict):
        rep.err("source", "missing required `source` block")
        return None
    check_unknown_keys(rep, "source", src, SOURCE_KEYS)
    for field in ("slug", "commit_sha", "branch", "generated_by"):
        if not src.get(field) or not isinstance(src.get(field), str):
            rep.err(
                f"source.{field}",
                f"missing required `{field}` — push provenance is not optional (requirement 4)",
            )
    digest = src.get("derived_digest")
    if digest is not None and not (isinstance(digest, str) and SHA256_RE.match(digest)):
        rep.err("source.derived_digest", "must be a 64-character lowercase hex SHA-256")
    acc_input = src.get("input")
    if acc_input is None:
        for field in ("acp_version", "acc_manifest_sha256"):
            if field in src:
                rep.err(f"source.{field}", "only a manifest built from an acc manifest carries this")
        return None
    if acc_input != "agent-change-control":
        rep.err("source.input", f"'{acc_input}' is not agent-change-control")
        return None
    version = src.get("acp_version")
    if version not in vocab["acp_versions"]:
        rep.err(
            "source.acp_version",
            f"'{version}' is not an ACP version this reads ({', '.join(vocab['acp_versions'])})"
            + suggest(version, vocab["acp_versions"]),
        )
    if not (isinstance(src.get("acc_manifest_sha256"), str) and SHA256_RE.match(src["acc_manifest_sha256"])):
        rep.err(
            "source.acc_manifest_sha256",
            "missing — a manifest built from acc records which acc manifest, so drift covers it",
        )
    return acc_input


def check_window(rep, doc, from_acc):
    window = doc.get("window")
    if window is None:
        if from_acc:
            rep.err(
                "window",
                "missing required `window` — the agents here were counted over the acc manifest's "
                "window, and the evidence record is an account of that period",
            )
        return None
    if not isinstance(window, dict):
        rep.err("window", "must be a mapping")
        return None
    check_unknown_keys(rep, "window", window, WINDOW_KEYS)
    for field in ("opens_on", "closes_on"):
        if not is_date(window.get(field)):
            rep.err(f"window.{field}", f"'{window.get(field)}' is not an ISO date (YYYY-MM-DD)")
            return None
    if days_between(window["closes_on"], window["opens_on"]) < 0:
        rep.err("window.closes_on", "the window closes before it opens")
        return None
    if window.get("complete") is False:
        rep.warn(
            "window.complete",
            "the acc collection was incomplete, so an agent missing here may still have written "
            "code in this window — say so in the attestation's rationale",
        )
    return window["closes_on"]


def check_changes(rep, doc, from_acc, agents):
    changes = doc.get("changes")
    if changes is None:
        if from_acc:
            rep.err("changes", "missing required `changes` totals for the acc window")
        return
    if not isinstance(changes, dict):
        rep.err("changes", "must be a mapping")
        return
    check_unknown_keys(rep, "changes", changes, CHANGES_KEYS)
    total, authored = changes.get("total"), changes.get("agent_authored")
    for field, value in (("total", total), ("agent_authored", authored)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            rep.err(f"changes.{field}", f"'{value}' is not a count")
            return
    if authored > total:
        rep.err("changes.agent_authored", f"{authored} agent-authored of only {total} change(s)")
    summed = sum(
        a.get("changes_authored", 0) for a in agents
        if isinstance(a, dict) and isinstance(a.get("changes_authored"), int)
    )
    if summed != authored:
        rep.err(
            "changes.agent_authored",
            f"{authored} here, but the agents below authored {summed} — the totals and the rows "
            "come from one acc manifest and must agree",
        )
    check_refs(rep, "changes", changes)


def check_key(rep, path, key, seen):
    if not isinstance(key, str) or not KEY_RE.match(key):
        rep.err(f"{path}.key", f"'{key}' is not a stable lowercase key")
        return
    if key in seen:
        rep.err(f"{path}.key", f"duplicate key '{key}' — keys must be unique and stable")
    seen.add(key)


def check_supplier_ref(rep, path, row, supplier_keys, uses, agent):
    supplier = row.get("supplier")
    if not isinstance(supplier, str) or not supplier:
        rep.err(f"{path}.supplier", "missing required `supplier` — name the supplier key it belongs to")
        return
    if supplier not in supplier_keys:
        rep.err(
            f"{path}.supplier",
            f"'{supplier}' is not a supplier in this manifest" + suggest(supplier, supplier_keys),
        )
        return
    uses.setdefault(supplier, set()).add("agent" if agent else "tool")


def check_agents(rep, doc, supplier_keys, uses):
    agents = doc.get("agents")
    if not isinstance(agents, list):
        rep.err("agents", "missing required `agents` (an empty list is a valid answer)")
        return []
    seen = set()
    for i, agent in enumerate(agents):
        path = f"agents[{i}]"
        if not isinstance(agent, dict):
            rep.err(path, "must be a mapping")
            continue
        check_unknown_keys(rep, path, agent, AGENT_KEYS)
        name = agent.get("agent")
        if not isinstance(name, str) or not name:
            rep.err(f"{path}.agent", "missing required `agent`")
        elif name in seen:
            rep.err(f"{path}.agent", f"duplicate agent '{name}'")
        else:
            seen.add(name)
            if agent.get("actor_id") != f"agent:{name}" and name != "(unrecognised)":
                rep.err(f"{path}.actor_id", f"'{agent.get('actor_id')}' is not agent:{name}")
        vendor = agent.get("vendor")
        if vendor is not None and not isinstance(vendor, str):
            rep.err(f"{path}.vendor", "must be the acc vendor identifier or null")
        for field in ("changes_authored", "reviews"):
            value = agent.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                rep.err(f"{path}.{field}", f"'{value}' is not a count")
        check_supplier_ref(rep, path, agent, supplier_keys, uses, agent=True)
        check_refs(rep, path, agent)
    return agents


def check_servers(rep, doc, vocab, supplier_keys, uses):
    servers = doc.get("mcp_servers")
    if not isinstance(servers, list):
        rep.err("mcp_servers", "missing required `mcp_servers` (an empty list is a valid answer)")
        return 0
    seen = set()
    for i, server in enumerate(servers):
        path = f"mcp_servers[{i}]"
        if not isinstance(server, dict):
            rep.err(path, "must be a mapping")
            continue
        check_unknown_keys(rep, path, server, SERVER_KEYS)
        check_key(rep, path, server.get("key"), seen)
        file, tool = server.get("file"), server.get("tool")
        if file not in vocab["config_files"] or file == ".claude/settings.json":
            rep.err(
                f"{path}.file",
                f"'{file}' is not an MCP configuration file this piece reads"
                + suggest(file, [f for f in vocab["config_files"] if f != ".claude/settings.json"]),
            )
        elif tool != vocab["config_files"][file]:
            rep.err(f"{path}.tool", f"{file} is {vocab['config_files'][file]}'s file, not '{tool}'s")
        if not isinstance(server.get("name"), str) or not server.get("name"):
            rep.err(f"{path}.name", "missing required `name`")
        transport = server.get("transport")
        if transport not in vocab["transport"]:
            rep.err(
                f"{path}.transport",
                f"unknown transport '{transport}'" + suggest(transport, vocab["transport"]),
            )
        elif transport == "stdio":
            command = server.get("command")
            if "host" in server:
                rep.err(f"{path}.host", "a stdio server runs a command; it has no host")
            if not isinstance(command, str) or not (command in SPECIAL or COMMAND_RE.match(command)):
                rep.err(
                    f"{path}.command",
                    f"'{command}' must be the program's name only — no directory, no argument",
                )
        else:
            host = server.get("host")
            if "command" in server:
                rep.err(f"{path}.command", "a remote server has a host, not a command")
            if not isinstance(host, str) or not (host in SPECIAL or HOST_RE.match(host)):
                rep.err(
                    f"{path}.host",
                    f"'{host}' must be a host name only — no scheme, port, path, query or user-info, "
                    "any of which can carry a credential",
                )
        check_supplier_ref(rep, path, server, supplier_keys, uses, agent=False)
        check_refs(rep, path, server)
    return len(servers)


def check_plugins(rep, doc, supplier_keys, uses):
    plugins = doc.get("plugins")
    if not isinstance(plugins, list):
        rep.err("plugins", "missing required `plugins` (an empty list is a valid answer)")
        return 0
    seen = set()
    for i, plugin in enumerate(plugins):
        path = f"plugins[{i}]"
        if not isinstance(plugin, dict):
            rep.err(path, "must be a mapping")
            continue
        check_unknown_keys(rep, path, plugin, PLUGIN_KEYS)
        check_key(rep, path, plugin.get("key"), seen)
        if plugin.get("file") != ".claude/settings.json" or plugin.get("tool") != "claude-code":
            rep.err(f"{path}.file", "plugins are read from .claude/settings.json (claude-code) only")
        for field in ("plugin", "marketplace"):
            if not isinstance(plugin.get(field), str) or not plugin.get(field):
                rep.err(f"{path}.{field}", f"missing required `{field}`")
        host = plugin.get("marketplace_host")
        if host is not None and not (isinstance(host, str) and (host in SPECIAL or HOST_RE.match(host))):
            rep.err(f"{path}.marketplace_host", f"'{host}' must be a host name only")
        check_supplier_ref(rep, path, plugin, supplier_keys, uses, agent=False)
        check_refs(rep, path, plugin)
    return len(plugins)


def supplier_keys_of(doc):
    keys = set()
    for supplier in doc.get("suppliers") or []:
        if isinstance(supplier, dict) and isinstance(supplier.get("key"), str):
            keys.add(supplier["key"])
    return keys


def check_suppliers(rep, doc, vocab, uses):
    suppliers = doc.get("suppliers")
    if not isinstance(suppliers, list):
        rep.err("suppliers", "missing required `suppliers` (an empty list is a valid answer)")
        return 0
    seen, names = set(), {}
    for i, supplier in enumerate(suppliers):
        path = f"suppliers[{i}]"
        if not isinstance(supplier, dict):
            rep.err(path, "must be a mapping")
            continue
        check_unknown_keys(rep, path, supplier, SUPPLIER_KEYS)
        key = supplier.get("key")
        check_key(rep, path, key, seen)

        name = supplier.get("vendor_name")
        if not isinstance(name, str) or not name.strip():
            rep.err(f"{path}.vendor_name", "missing required `vendor_name`")
        elif "TODO" in name:
            rep.err(
                f"{path}.vendor_name",
                "is still TODO — a person has to decide which organization this is. Do not infer "
                "it from a hostname or a package scope",
            )
        else:
            folded = name.strip().casefold()
            if folded in names:
                rep.err(
                    f"{path}.vendor_name",
                    f"'{name}' is also suppliers[{names[folded]}] — both would land as one vendor "
                    "in Noru; merge them into one supplier and point everything at it",
                )
            else:
                names[folded] = i

        category = supplier.get("category")
        if category not in vocab["vendor_category"]:
            rep.err(
                f"{path}.category",
                f"unknown vendor category '{category}'" + suggest(category, vocab["vendor_category"]),
            )
        used = uses.get(key, set())
        if "agent" in used and category != vocab["agent_vendor_category"]:
            rep.err(
                f"{path}.category",
                f"'{category}' — an agent depends on this supplier, and the organization that builds "
                f"and operates a coding agent's model is an AI model provider: use "
                f"`{vocab['agent_vendor_category']}`",
            )
        if isinstance(key, str) and not used:
            rep.err(
                path,
                f"nothing in this manifest depends on supplier '{key}' — remove it, or point the "
                "agent, server or plugin it stands for at it",
            )
        website = supplier.get("website")
        if website is not None and not (isinstance(website, str) and website.startswith("https://")):
            rep.err(f"{path}.website", f"'{website}' is not an https:// URL")
        check_refs(rep, path, supplier)
        check_interpretation(rep, path, supplier)
        check_needs_review(rep, path, supplier)
    return len(suppliers)


def check_queue(rep, doc, vocab):
    """Requirement 9: the control ids come from Noru, and this plugin ships none."""
    snapshot = doc.get("queue_snapshot")
    if not isinstance(snapshot, dict):
        rep.err(
            "queue_snapshot",
            "missing required `queue_snapshot` — which controls this inventory is evidence for "
            "is Noru's answer, and this plugin ships none of it",
        )
        return set()
    check_unknown_keys(rep, "queue_snapshot", snapshot, QUEUE_KEYS)
    fetched = snapshot.get("fetched_at")
    if not isinstance(fetched, str) or len(fetched) < 10 or "TODO" in fetched:
        rep.err("queue_snapshot.fetched_at", "missing required `fetched_at` — when was Noru asked?")
    via = snapshot.get("via")
    if not isinstance(via, list) or not via:
        rep.err("queue_snapshot.via", "must name the MCP tools this snapshot came from")
    else:
        for j, tool in enumerate(via):
            if tool not in vocab["queue_tools"]:
                rep.err(
                    f"queue_snapshot.via[{j}]",
                    f"'{tool}' is not a queue tool this piece reads" + suggest(tool, vocab["queue_tools"]),
                )
    controls = set()
    queue_controls = snapshot.get("controls")
    if not isinstance(queue_controls, list):
        rep.err("queue_snapshot.controls", "must be a list (an empty list is a valid answer)")
        return controls
    for i, control in enumerate(queue_controls):
        path = f"queue_snapshot.controls[{i}]"
        if not isinstance(control, dict):
            rep.err(path, "must be a mapping")
            continue
        check_unknown_keys(rep, path, control, QUEUE_CONTROL_KEYS)
        control_id = control.get("control_id")
        if not control_id or not isinstance(control_id, str):
            rep.err(f"{path}.control_id", "missing required `control_id`")
            continue
        if control_id != control_id.lower():
            rep.err(
                f"{path}.control_id",
                f"'{control_id}' is the display id — use the lowercase canonical id Noru returns",
            )
        controls.add(control_id)
    return controls


def check_control_mappings(rep, doc, queue_controls):
    mappings = doc.get("control_mappings")
    if not mappings:
        rep.warn(
            "control_mappings",
            "the evidence record is mapped to no control, so it lands in Noru attached to nothing",
        )
        return 0
    if not isinstance(mappings, list):
        rep.err("control_mappings", "must be a list")
        return 0
    seen = set()
    for i, mapping in enumerate(mappings):
        path = f"control_mappings[{i}]"
        if not isinstance(mapping, dict):
            rep.err(path, "must be a mapping")
            continue
        check_unknown_keys(rep, path, mapping, MAPPING_KEYS)
        control_id = mapping.get("control_id")
        if not control_id or not isinstance(control_id, str):
            rep.err(f"{path}.control_id", "missing required `control_id`")
            continue
        if control_id in seen:
            rep.err(f"{path}.control_id", f"'{control_id}' is mapped twice")
        seen.add(control_id)
        if control_id not in queue_controls:
            rep.err(
                f"{path}.control_id",
                f"'{control_id}' is not in the queue snapshot — a piece works Noru's queue and does "
                "not invent one" + suggest(control_id, queue_controls),
            )
    return len(mappings)


def check_config_problems(rep, doc):
    problems = doc.get("config_problems")
    if problems is None:
        return
    if not isinstance(problems, list):
        rep.err("config_problems", "must be a list")
        return
    for i, problem in enumerate(problems):
        path = f"config_problems[{i}]"
        if not isinstance(problem, dict):
            rep.err(path, "must be a mapping")
            continue
        check_unknown_keys(rep, path, problem, PROBLEM_KEYS)
        check_refs(rep, path, problem)
        rep.warn(
            path,
            f"NOT in this inventory: {problem.get('message')}. Fix the configuration and re-run :scan",
        )


def check_attestation(rep, doc, window_close):
    attestation = doc.get("attestation")
    if not isinstance(attestation, dict):
        rep.err(
            "attestation",
            "missing required `attestation` — the evidence record says a named person reviewed "
            "this inventory, so a person has to",
        )
        return
    check_unknown_keys(rep, "attestation", attestation, ATTESTATION_KEYS)
    check_refs(rep, "attestation", attestation)
    check_interpretation(
        rep, "attestation", attestation, anchor=window_close,
        anchor_label="the window closed" if window_close else None,
    )
    check_needs_review(rep, "attestation", attestation)


def validate(doc, vocab):
    rep = Report()
    counts = {"agents": 0, "mcp_servers": 0, "plugins": 0, "suppliers": 0, "control_mappings": 0}
    if not isinstance(doc, dict):
        rep.err("<root>", "manifest must be a mapping with `version`, `piece`, `source`, `suppliers`")
        return rep, counts

    check_no_person_data(rep, doc)
    check_unknown_keys(rep, "<root>", doc, TOP_LEVEL_KEYS)
    version = doc.get("version")
    if not version or not isinstance(version, str) or not SEMVER_RE.match(version):
        rep.err("version", f"'{version}' is not a semantic version (for example 0.1.0)")
    if doc.get("piece") != PIECE:
        rep.err("piece", f"expected '{PIECE}', found '{doc.get('piece')}'")

    from_acc = check_source(rep, doc, vocab) is not None
    window_close = check_window(rep, doc, from_acc)
    supplier_keys = supplier_keys_of(doc)
    uses = {}
    agents = check_agents(rep, doc, supplier_keys, uses)
    counts["agents"] = len(agents)
    check_changes(rep, doc, from_acc, agents)
    if agents and not from_acc:
        rep.err("agents", "agents come from an acc manifest, and `source` names none")
    counts["mcp_servers"] = check_servers(rep, doc, vocab, supplier_keys, uses)
    counts["plugins"] = check_plugins(rep, doc, supplier_keys, uses)
    check_config_problems(rep, doc)
    counts["suppliers"] = check_suppliers(rep, doc, vocab, uses)
    queue_controls = check_queue(rep, doc, vocab)
    counts["control_mappings"] = check_control_mappings(rep, doc, queue_controls)
    check_attestation(rep, doc, window_close)
    if not from_acc:
        rep.warn(
            "source.input",
            "no agent-change-control manifest was read, so the coding agents that wrote code here "
            "are not in this inventory — only the configured MCP servers and plugins are",
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
            f"\nOK: {counts['agents']} agent(s), {counts['mcp_servers']} MCP server(s), "
            f"{counts['plugins']} plugin(s), {counts['suppliers']} supplier(s), "
            f"{counts['control_mappings']} control mapping(s) ({len(rep.warnings)} warning(s))."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
