#!/usr/bin/env python3
"""Validate .noru/change-control.yml against the BUNDLED vocabulary.

Self-contained and atomic: Python standard library only, no pip install, no network.

Contract requirement 8 is the rule that makes this piece worth trusting: every item must carry
refs[] citing the lines that produced it AND a complete interpretation block naming the person who
stands behind it. An unattributed claim is an ERROR, never a warning.

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
PIECE = "change-control"

REF_RE = re.compile(r"^[^:\s][^:]*:[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

TOP_LEVEL_KEYS = {
    "version", "piece", "source", "window", "queue_snapshot", "controls",
    "control_mappings", "changes",
}
QUEUE_KEYS = {"fetched_at", "via", "controls"}
QUEUE_CONTROL_KEYS = {"control_id", "display_id", "name", "unmet_evidence_items"}
EVIDENCE_ITEM_KEYS = {"id", "title", "type"}
MAPPING_KEYS = {"control_id", "evidence_item_ids"}
SOURCE_KEYS = {"slug", "commit_sha", "branch", "generated_by", "forge", "derived_digest"}
WINDOW_KEYS = {"opens_on", "closes_on", "complete"}
CONTROLS_KEYS = {
    "default_branch", "observed_on", "protected", "required_approvals", "dismiss_stale_reviews",
    "require_code_owner_review", "enforce_admins", "allow_force_push", "required_status_checks",
    "codeowners_present", "deploy_environments", "refs", "interpretation", "needs_review",
}
ENVIRONMENT_KEYS = {"name", "required_reviewers", "prevent_self_review"}
CHANGE_KEYS = {
    "key", "kind", "title", "authored_by", "author_kind", "agent_operator", "opened_on",
    "approvals", "merged_by", "merged_on", "deployed_by", "deployed_on", "artifact_digest",
    "bypass", "exceptions", "refs", "interpretation", "needs_review",
}
APPROVAL_KEYS = {"by", "state", "reviewed_on"}
BYPASS_KEYS = {"used", "kind", "by", "reason"}
EXCEPTION_KEYS = {"rule", "disposition", "owner", "note", "resolved_on"}
INTERPRETATION_KEYS = {"owner", "decided_at", "expires_at", "rationale", "refs"}

# Horizons, measured from the END of the window rather than from the signature — the audit-pack
# anchor. A conclusion about July, signed in December, does not cover more of July for being signed
# late, and anchoring on `decided_at` would quietly reward filing the paperwork slowly.
HORIZON_DAYS = 400
HORIZON_DAYS_DEFERRED = 120


def load_vocabulary():
    return json.loads((REFERENCES / "vocabulary.json").read_text(encoding="utf-8"))


def days_between(later, earlier):
    return (datetime.date.fromisoformat(later) - datetime.date.fromisoformat(earlier)).days


def check_unknown_keys(rep, path, obj, allowed):
    for key in obj:
        if key not in allowed:
            rep.err(f"{path}.{key}", f"unknown key '{key}'" + suggest(key, allowed))


def check_person(rep, path, value, hint):
    if not isinstance(value, str) or len(value.strip()) < 3:
        rep.err(path, f"missing or too short — {hint}")
        return None
    return value.strip()


def check_refs(rep, path, obj):
    refs = obj.get("refs")
    if refs is None:
        rep.err(path, "missing required `refs` — every claim must cite the record that produced it")
        return
    if not isinstance(refs, list) or len(refs) == 0:
        rep.err(f"{path}.refs", "must be a non-empty list — an unattributed claim is an error")
        return
    for i, ref in enumerate(refs):
        if not isinstance(ref, str) or not REF_RE.match(ref):
            rep.err(f"{path}.refs[{i}]", f"'{ref}' is not a 'file:line' citation")


def check_interpretation(rep, path, obj, window_close):
    """Requirement 8, with `expires_at` mandatory and anchored on the window.

    Nothing here reads the clock. `--as-of` is deliberately absent, as docs/ci-mode.md describes:
    time is checked once, in CI mode's expiry step, so one stale claim cannot come back as two
    findings with two different exit codes.
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
                "missing required `expires_at` — an account of a period that never lapses is one "
                "nobody will ever renew"
                if field == "expires_at"
                else "missing required `decided_at` (YYYY-MM-DD)",
            )
        elif not isinstance(value, str) or not DATE_RE.match(value):
            rep.err(f"{ipath}.{field}", f"'{value}' is not an ISO date (YYYY-MM-DD)")

    rationale = block.get("rationale")
    if not rationale or not isinstance(rationale, str) or len(rationale.strip()) < 10:
        rep.err(f"{ipath}.rationale", "missing or too short — say why this claim holds")

    decided, expires = block.get("decided_at"), block.get("expires_at")
    if not (window_close and isinstance(decided, str) and DATE_RE.match(decided)):
        return
    if days_between(decided, window_close) < 0:
        rep.err(
            f"{ipath}.decided_at",
            f"{decided} is before the window closed on {window_close} — a conclusion about a "
            "period cannot be drawn while the period is still running",
        )
    if not (isinstance(expires, str) and DATE_RE.match(expires)):
        return
    if days_between(expires, window_close) <= 0:
        rep.err(
            f"{ipath}.expires_at",
            f"{expires} is not after the window closed on {window_close} — a conclusion that "
            "expires inside its own period never asserted anything",
        )
        return
    deferred = any(
        isinstance(e, dict) and e.get("disposition") == "deferred"
        for e in (obj.get("exceptions") or [])
    )
    horizon = HORIZON_DAYS_DEFERRED if deferred else HORIZON_DAYS
    span = days_between(expires, window_close)
    if span > horizon:
        rep.err(
            f"{ipath}.expires_at",
            f"{expires} is {span} days after the window closed, past the {horizon}-day horizon"
            + (
                " for a record carrying a deferred exception — something nobody has fixed yet is "
                "not something to sign off for a year"
                if deferred
                else " measured from the end of the window"
            ),
        )


def check_queue(rep, doc):
    """Requirement 9: the control ids come from Noru, and this plugin ships none.

    Returns (control ids, evidence item ids) the snapshot offered, so a mapping can be checked
    against them. A piece that let a manifest name any control id would be inventing a queue.
    """
    snapshot = doc.get("queue_snapshot")
    if not isinstance(snapshot, dict):
        rep.err(
            "queue_snapshot",
            "missing required `queue_snapshot` — which change-management separations this "
            "organization must hold to is Noru's answer, and this plugin ships none of it",
        )
        return set(), set()
    check_unknown_keys(rep, "queue_snapshot", snapshot, QUEUE_KEYS)
    if not snapshot.get("fetched_at"):
        rep.err("queue_snapshot.fetched_at", "missing required `fetched_at`")
    via = snapshot.get("via")
    if not isinstance(via, list) or not via:
        rep.err("queue_snapshot.via", "must name the MCP tools this snapshot came from")

    controls, items = set(), set()
    queue_controls = snapshot.get("controls")
    if not isinstance(queue_controls, list):
        rep.err("queue_snapshot.controls", "must be a list (an empty list is a valid answer)")
        return controls, items
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
        for j, item in enumerate(control.get("unmet_evidence_items") or []):
            ipath = f"{path}.unmet_evidence_items[{j}]"
            if not isinstance(item, dict):
                rep.err(ipath, "must be a mapping")
                continue
            check_unknown_keys(rep, ipath, item, EVIDENCE_ITEM_KEYS)
            if item.get("id"):
                items.add(item["id"])
            else:
                rep.err(f"{ipath}.id", "missing required `id`")
    return controls, items


def check_control_mappings(rep, doc, queue_controls, queue_items):
    mappings = doc.get("control_mappings")
    if mappings is None:
        rep.warn(
            "control_mappings",
            "this window is mapped to no control, so the record lands in Noru attached to nothing "
            "— evidence nobody can find is evidence nobody will use",
        )
        return
    if not isinstance(mappings, list):
        rep.err("control_mappings", "must be a list")
        return
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
        if control_id not in queue_controls:
            rep.err(
                f"{path}.control_id",
                f"'{control_id}' is not in the queue snapshot" + suggest(control_id, queue_controls),
            )
        for j, item_id in enumerate(mapping.get("evidence_item_ids") or []):
            if item_id not in queue_items:
                rep.err(
                    f"{path}.evidence_item_ids[{j}]",
                    f"'{item_id}' is not an unmet item the queue offered for this control"
                    + suggest(item_id, queue_items),
                )


def check_source(rep, doc, vocab):
    src = doc.get("source")
    if not isinstance(src, dict):
        rep.err("source", "missing required `source` block")
        return
    check_unknown_keys(rep, "source", src, SOURCE_KEYS)
    for field in ("slug", "commit_sha", "branch", "generated_by"):
        if not src.get(field) or not isinstance(src.get(field), str):
            rep.err(
                f"source.{field}",
                f"missing required `{field}` — push provenance is not optional (requirement 4)",
            )
    forge = src.get("forge")
    if forge is not None and forge not in vocab["forge"]:
        rep.err("source.forge", f"unknown forge '{forge}'" + suggest(forge, vocab["forge"]))


def check_window(rep, doc):
    window = doc.get("window")
    if not isinstance(window, dict):
        rep.err(
            "window",
            "missing required `window` — every record here accounts for a period, and every "
            "expiry is measured from the day that period ended",
        )
        return None
    check_unknown_keys(rep, "window", window, WINDOW_KEYS)
    for field in ("opens_on", "closes_on"):
        value = window.get(field)
        if not isinstance(value, str) or not DATE_RE.match(value):
            rep.err(f"window.{field}", f"'{value}' is not an ISO date (YYYY-MM-DD)")
            return None
    if days_between(window["closes_on"], window["opens_on"]) < 0:
        rep.err("window.closes_on", "the window closes before it opens")
        return None
    if window.get("complete") is False:
        rep.warn(
            "window.complete",
            "this export is partial, so absence of a change here is not evidence it did not "
            "happen — say in the rationale how the subset was chosen",
        )
    return window["closes_on"]


def check_controls(rep, doc, window_close):
    controls = doc.get("controls")
    if controls is None:
        rep.warn(
            "controls",
            "no forge configuration recorded — the changes below say what happened, and nothing "
            "here says what was supposed to stop it. Run the exporter with settings access",
        )
        return
    if not isinstance(controls, dict):
        rep.err("controls", "must be a mapping")
        return
    check_unknown_keys(rep, "controls", controls, CONTROLS_KEYS)
    if not controls.get("default_branch"):
        rep.err("controls.default_branch", "missing required `default_branch`")
    observed = controls.get("observed_on")
    if not isinstance(observed, str) or not DATE_RE.match(observed):
        rep.err(
            "controls.observed_on",
            f"'{observed}' is not an ISO date — a configuration claim with no date is a claim "
            "about no particular day",
        )
    for i, env in enumerate(controls.get("deploy_environments") or []):
        if not isinstance(env, dict):
            rep.err(f"controls.deploy_environments[{i}]", "must be a mapping")
            continue
        check_unknown_keys(rep, f"controls.deploy_environments[{i}]", env, ENVIRONMENT_KEYS)
        if not env.get("name"):
            rep.err(f"controls.deploy_environments[{i}].name", "missing required `name`")

    # These are warnings, not errors, and deliberately so: this piece does not get to decide that
    # an organization must require two approvals. It reports what a reviewer will be asked about.
    if controls.get("protected") is False:
        rep.warn(
            "controls.protected",
            f"the default branch '{controls.get('default_branch')}' is not protected, so every "
            "separation recorded below held by convention rather than by configuration",
        )
    if controls.get("required_approvals") == 0:
        rep.warn("controls.required_approvals", "zero approvals are required to merge")
    if controls.get("enforce_admins") is False:
        rep.warn(
            "controls.enforce_admins",
            "administrators are exempt from branch protection, so the control does not bind the "
            "people most able to step around it",
        )
    if controls.get("allow_force_push") is True:
        rep.warn("controls.allow_force_push", "force push is permitted on the protected branch")
    check_refs(rep, "controls", controls)
    check_interpretation(rep, "controls", controls, window_close)
    if controls.get("needs_review") is True:
        rep.err(
            "controls.needs_review",
            "still true — a human has not confirmed this configuration; resolve it and remove "
            "the flag before pushing",
        )


# --------------------------------------------------------------------------------------------- #
# Segregation of duties.
#
# This MUST agree with violationsOf() in scripts/collect.mjs. Two implementations of one rule is a
# thing that drifts, so both are kept deliberately trivial — comparisons between lowercased names,
# nothing else — and scripts/test_collectors.py asserts they agree on the fixture export.
#
# The manifest records what happened and this validator does not refuse the truth. A change that
# was genuinely self-approved is recorded as self-approved; what is refused is an *unowned* one.
# That is review-signoff's precedent (every exception needs a disposition and a named owner), which
# fits an observed history in a way audit-pack's "reviewed_by cannot be prepared_by" does not.

def _person(value):
    return value.strip().lower() if isinstance(value, str) else None


def violations_of(change):
    """Every separation that did not hold for one change, in a fixed order."""
    out = []
    author = _person(change.get("authored_by"))
    operator = _person(change.get("agent_operator"))
    approvers = [
        _person(a.get("by"))
        for a in (change.get("approvals") or [])
        if isinstance(a, dict) and a.get("state") == "approved" and _person(a.get("by"))
    ]
    independent = [who for who in approvers if who != author]

    if author and author in approvers:
        out.append(("approver_is_author", f"{change.get('authored_by')} approved their own change"))
    if not independent:
        out.append((
            "merged_without_independent_approval",
            "no approval from anybody"
            if not approvers
            else f"the only approval came from {change.get('authored_by')}, who wrote it",
        ))
    deployer = _person(change.get("deployed_by"))
    if deployer and author and deployer == author:
        out.append((
            "deployer_is_author",
            f"{change.get('authored_by')} wrote the change and also put it in production",
        ))
    if change.get("author_kind") == "agent" and not [w for w in independent if w != operator]:
        out.append((
            "agent_change_without_independent_human",
            f"written by an agent and reviewed by nobody other than "
            f"{change.get('agent_operator')}, who ran it"
            if operator
            else "written by an agent with no independent human approval",
        ))
    bypass = change.get("bypass")
    if isinstance(bypass, dict) and bypass.get("used") is True:
        out.append((
            "bypass_used",
            f"{bypass.get('kind')} was used to get this change in"
            if bypass.get("kind")
            else "a control was stepped around",
        ))
    return out


def check_exceptions(rep, path, change, vocab):
    exceptions = change.get("exceptions")
    if exceptions is None:
        exceptions = []
    elif not isinstance(exceptions, list):
        rep.err(f"{path}.exceptions", "must be a list")
        return
    owned = set()
    for i, exception in enumerate(exceptions):
        epath = f"{path}.exceptions[{i}]"
        if not isinstance(exception, dict):
            rep.err(epath, "exception must be a mapping")
            continue
        check_unknown_keys(rep, epath, exception, EXCEPTION_KEYS)
        rule = exception.get("rule")
        if rule not in vocab["sod_rule"]:
            rep.err(f"{epath}.rule", f"unknown rule '{rule}'" + suggest(rule, vocab["sod_rule"]))
        else:
            owned.add(rule)
        disposition = exception.get("disposition")
        if disposition not in vocab["disposition"]:
            rep.err(
                f"{epath}.disposition",
                f"unknown disposition '{disposition}'" + suggest(disposition, vocab["disposition"]),
            )
        check_person(
            rep, f"{epath}.owner", exception.get("owner"),
            "an exception nobody owns will still be there next quarter",
        )
        note = exception.get("note")
        if not note or not isinstance(note, str) or len(note.strip()) < 10:
            rep.err(
                f"{epath}.note",
                "missing or too short — say what happened and why it was allowed to stand",
            )
        resolved = exception.get("resolved_on")
        if resolved is not None and (not isinstance(resolved, str) or not DATE_RE.match(resolved)):
            rep.err(f"{epath}.resolved_on", f"'{resolved}' is not an ISO date (YYYY-MM-DD)")
        if disposition == "remediated" and resolved is None:
            rep.err(
                f"{epath}.resolved_on",
                "a remediated exception must say when it was put right, or it is a deferred one "
                "wearing a better word",
            )

    fired = violations_of(change)
    for rule, detail in fired:
        if rule not in owned:
            rep.err(
                f"{path}.exceptions",
                f"{detail} — that is `{rule}`, and nothing in this record owns it. This manifest "
                "records what happened and will not ask you to pretend otherwise; it asks for a "
                "disposition and a named owner",
            )
    fired_rules = {rule for rule, _ in fired}
    for rule in sorted(owned - fired_rules):
        rep.err(
            f"{path}.exceptions",
            f"an exception is recorded for `{rule}`, but nothing in this change triggers it — a "
            "blanket exception written ahead of time is how a control stops meaning anything",
        )


def check_changes(rep, doc, vocab, window_close, counts):
    changes = doc.get("changes")
    if changes is None:
        rep.err("changes", "missing required `changes` (an empty list is a valid answer)")
        return
    if not isinstance(changes, list):
        rep.err("changes", "must be a list")
        return

    seen = set()
    for i, change in enumerate(changes):
        path = f"changes[{i}]"
        if not isinstance(change, dict):
            rep.err(path, "change must be a mapping")
            continue
        check_unknown_keys(rep, path, change, CHANGE_KEYS)

        key = change.get("key")
        if not key or not isinstance(key, str) or not KEY_RE.match(key):
            rep.err(f"{path}.key", f"'{key}' is not a stable lowercase key")
        elif key in seen:
            rep.err(f"{path}.key", f"duplicate key '{key}' — keys must be unique and stable")
        else:
            seen.add(key)

        if not change.get("title"):
            rep.err(f"{path}.title", "missing required `title`")
        kind = change.get("kind")
        if kind not in vocab["change_kind"]:
            rep.err(
                f"{path}.kind",
                f"unknown change kind '{kind}'" + suggest(kind, vocab["change_kind"]),
            )
        check_person(
            rep, f"{path}.authored_by", change.get("authored_by"),
            "name the person who wrote this, not a team alias",
        )

        author_kind = change.get("author_kind")
        if author_kind not in vocab["author_kind"]:
            rep.err(
                f"{path}.author_kind",
                f"unknown author kind '{author_kind}'" + suggest(author_kind, vocab["author_kind"]),
            )
        elif author_kind == "agent":
            counts["agent_authored"] += 1
            check_person(
                rep, f"{path}.agent_operator", change.get("agent_operator"),
                "an agent-authored change must name the person who ran it: somebody pressed go, "
                "and they are not an independent reviewer of what came back",
            )
        elif change.get("agent_operator") is not None:
            rep.err(
                f"{path}.agent_operator",
                "only an agent-authored change has an operator",
            )

        opened = change.get("opened_on")
        if not isinstance(opened, str) or not DATE_RE.match(opened):
            rep.err(f"{path}.opened_on", f"'{opened}' is not an ISO date (YYYY-MM-DD)")
            opened = None

        for j, approval in enumerate(change.get("approvals") or []):
            apath = f"{path}.approvals[{j}]"
            if not isinstance(approval, dict):
                rep.err(apath, "approval must be a mapping")
                continue
            check_unknown_keys(rep, apath, approval, APPROVAL_KEYS)
            check_person(rep, f"{apath}.by", approval.get("by"), "name the reviewer")
            state = approval.get("state")
            if state not in vocab["approval_state"]:
                rep.err(
                    f"{apath}.state",
                    f"unknown approval state '{state}'" + suggest(state, vocab["approval_state"]),
                )
            reviewed = approval.get("reviewed_on")
            if reviewed is not None and (not isinstance(reviewed, str) or not DATE_RE.match(reviewed)):
                rep.err(f"{apath}.reviewed_on", f"'{reviewed}' is not an ISO date (YYYY-MM-DD)")
            elif reviewed and opened and days_between(reviewed, opened) < 0:
                rep.err(
                    f"{apath}.reviewed_on",
                    f"approved on {reviewed}, before the change was opened on {opened}",
                )

        for field, label in (("merged_by", "merged"), ("deployed_by", "deployed")):
            if change.get(field) is not None:
                check_person(rep, f"{path}.{field}", change.get(field), f"name who {label} it")
        for field in ("merged_on", "deployed_on"):
            value = change.get(field)
            if value is not None and (not isinstance(value, str) or not DATE_RE.match(value)):
                rep.err(f"{path}.{field}", f"'{value}' is not an ISO date (YYYY-MM-DD)")
        merged_on, deployed_on = change.get("merged_on"), change.get("deployed_on")
        if (
            isinstance(merged_on, str) and DATE_RE.match(merged_on)
            and isinstance(deployed_on, str) and DATE_RE.match(deployed_on)
            and days_between(deployed_on, merged_on) < 0
        ):
            rep.err(
                f"{path}.deployed_on",
                f"deployed on {deployed_on}, before it was merged on {merged_on}",
            )
        if opened and isinstance(merged_on, str) and DATE_RE.match(merged_on):
            if days_between(merged_on, opened) < 0:
                rep.err(
                    f"{path}.merged_on",
                    f"merged on {merged_on}, before it was opened on {opened}",
                )
        if window_close and opened and days_between(window_close, opened) < 0:
            rep.err(
                f"{path}.opened_on",
                f"{opened} falls outside the window, which closed on {window_close}",
            )

        bypass = change.get("bypass")
        if bypass is not None:
            if not isinstance(bypass, dict):
                rep.err(f"{path}.bypass", "must be a mapping")
            else:
                check_unknown_keys(rep, f"{path}.bypass", bypass, BYPASS_KEYS)
                if bypass.get("used") is True:
                    if bypass.get("kind") not in vocab["bypass_kind"]:
                        rep.err(
                            f"{path}.bypass.kind",
                            f"unknown bypass kind '{bypass.get('kind')}'"
                            + suggest(bypass.get("kind"), vocab["bypass_kind"]),
                        )
                    reason = bypass.get("reason")
                    if not reason or not isinstance(reason, str) or len(reason.strip()) < 10:
                        rep.err(
                            f"{path}.bypass.reason",
                            "a control was stepped around and nothing says why — a bypass nobody "
                            "wrote down is indistinguishable from a control that held",
                        )

        check_exceptions(rep, path, change, vocab)
        counts["exceptions"] += len(change.get("exceptions") or [])
        check_refs(rep, path, change)
        check_interpretation(rep, path, change, window_close)
        if change.get("needs_review") is True:
            rep.err(
                f"{path}.needs_review",
                "still true — a human has not dispositioned what the collector found; resolve it "
                "and remove the flag before pushing",
            )
    counts["changes"] = len(changes)


def validate(doc, vocab):
    rep = Report()
    counts = {"changes": 0, "exceptions": 0, "agent_authored": 0}
    if not isinstance(doc, dict):
        rep.err("<root>", "manifest must be a mapping with `version`, `piece`, `source`, `window`, `changes`")
        return rep, counts

    check_unknown_keys(rep, "<root>", doc, TOP_LEVEL_KEYS)
    version = doc.get("version")
    if not version or not isinstance(version, str) or not SEMVER_RE.match(version):
        rep.err("version", f"'{version}' is not a semantic version (for example 0.1.0)")
    if doc.get("piece") != PIECE:
        rep.err("piece", f"expected '{PIECE}', found '{doc.get('piece')}'")

    check_source(rep, doc, vocab)
    window_close = check_window(rep, doc)
    queue_controls, queue_items = check_queue(rep, doc)
    check_control_mappings(rep, doc, queue_controls, queue_items)
    check_controls(rep, doc, window_close)
    check_changes(rep, doc, vocab, window_close, counts)
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
            f"\nOK: {counts['changes']} change(s), {counts['exceptions']} owned exception(s), "
            f"{counts['agent_authored']} agent-authored ({len(rep.warnings)} warning(s))."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
