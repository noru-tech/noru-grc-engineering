#!/usr/bin/env python3
"""Validate .noru/privacy-datamap.yml against the BUNDLED vocabulary.

Self-contained and atomic: Python standard library only, no pip install, no network.

Contract requirement 8 is the rule that makes this piece worth trusting: every item must carry
refs[] citing the lines that produced it AND a complete interpretation block naming the person who
stands behind it. An unattributed claim is an ERROR, never a warning.

Two things anchor a claim here, and the pair is the point.

`structure_digest` pins WHAT a signature was given for: the field names of the collection, not their
categories, so resolving a classification keeps the signature and adding a column breaks it. That is
the fourth anchor in contract/README.md requirement 8, and this is the piece that uses it.

`expires_at` pins HOW LONG nobody has looked. It is measured from `decided_at`, which is an honest
anchor here and is not in most pieces: elsewhere it rewards signing late, and here it cannot,
because a signature cannot outlive the structure it was given for. Special-category data gets a
shorter horizon. `--as-of=YYYY-MM-DD` turns an already-expired claim into an error; leave it off and
the file is judged on its own terms, so nothing here reads the clock by itself.

A collection may also be owned by an agent, and only to RENEW it: the collection must be, apart from
its interpretation and its citations, exactly what a person signed at the base revision, the agent
must act under a grant in the repository's committed .noru/interpretation-policy.yml, and a named
person answers for it as `operator`. The next renewal after an agent's is a person's. Declarations
carry no structural anchor, so they stay a person's to sign. `--base` is the manifest at the base
revision and `--interpretation-policy` the policy as it stood there; CI mode passes both. Without
`--base` the lineage cannot be checked here and is reported as a warning, never as a pass.

Usage:
    python3 validate_manifest.py <manifest.yml> [--as-of=YYYY-MM-DD] [--output=json] [--quiet]
        [--emit-parsed=<path>] [--base=<manifest.yml>] [--interpretation-policy=<path>]
Exit codes: 0 = valid (warnings allowed), 1 = validation errors, 2 = usage / load error.
"""
import datetime
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dependencies
import relationships
import analysis_cache
import dataset_inputs

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
PIECE = "privacy-datamap"

REF_RE = re.compile(r"^[^:\s][^:]*:[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# Fideslang's own key grammar, from the upstream FidesKey type.
FIDES_KEY_RE = re.compile(r"^[A-Za-z0-9_.<>-]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

import migration_support

TOP_LEVEL_KEYS = {"migration_support", "version", "piece", "source", "dataset", "system", "evidence_dependencies", "relationship_graph"}
SOURCE_KEYS = {"slug", "commit_sha", "branch", "generated_by", "derived_digest"}
INTERPRETATION_KEYS = {
    "owner", "owner_kind", "operator", "policy_ref", "renews", "decided_at", "expires_at",
    "rationale", "refs",
}
RENEWS_KEYS = {"owner", "decided_at"}
POLICY_KEYS = {"kind", "version", "grants"}
GRANT_KEYS = {"id", "piece", "claims", "agents", "operators", "max_days"}
# The only claim class an agent may hold. The validator enforces it, not the policy file: a policy
# that grants anything else is itself invalid.
AGENT_CLAIMS = ["renewal"]
# Pieces whose owners hold a duty that exists to be held apart from someone else's. No policy can
# grant an agent anything in them.
NEVER_AGENT = ("change-control", "audit-pack", "review-signoff")
# The shapes a team alias usually takes: a forge team handle, a path, or a word naming a group.
# It catches the common mistakes; the named list of operators in a reviewed policy is the control.
TEAM_RE = re.compile(
    r"^@|/|\b(team|teams|squad|group|guild|crew|department|dept|committee|board|admins?|owners|"
    r"everyone|all|staff|bots?|agents?)\b",
    re.IGNORECASE,
)
DATASET_KEYS = {"fides_key", "name", "description", "collections", "meta"}
COLLECTION_KEYS = {
    "name", "description", "refs", "interpretation", "needs_review", "fields",
    "non_personal_fields", "structure_digest",
}
FIELD_KEYS = {"name", "description", "data_categories", "refs", "needs_review", "fields", "meta"}
SYSTEM_KEYS = {
    "fides_key", "name", "description", "system_type", "dataset_references",
    "privacy_declarations", "meta", "ingress", "egress",
}
DECLARATION_KEYS = {
    "name", "data_use", "data_categories", "data_subjects", "refs", "interpretation",
    "needs_review", "ingress", "egress",
}


def load_vocabulary():
    """The bundled vocabulary plus the Fideslang snapshot the piece validates keys against.

    Offline by construction (requirement 3). Where the piece can reach Noru, getPrivacyTaxonomy is
    authoritative and :scan reconciles against it — a key this snapshot has never heard of is a
    stale snapshot, not necessarily an invalid key. That reconciliation is the skill's job; this
    file's job is to be right about the snapshot it has.
    """
    vocab = json.loads((REFERENCES / "vocabulary.json").read_text(encoding="utf-8"))
    for name, key in (
        ("data_categories", "data_categories"),
        ("data_uses", "data_use"),
        ("data_subjects", "data_subjects"),
    ):
        rows = json.loads((REFERENCES / "taxonomy" / f"{name}.json").read_text(encoding="utf-8"))
        vocab[key] = [row["fides_key"] for row in rows]
    table = json.loads((REFERENCES / "classification.json").read_text(encoding="utf-8"))
    vocab["special_categories"] = table["special_categories"]
    return vocab


def check_unknown_keys(rep, path, obj, allowed):
    for key in obj:
        if key not in allowed:
            rep.err(f"{path}.{key}", f"unknown key '{key}'" + suggest(key, allowed))


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


def check_interpretation(rep, path, obj, anchored=False):
    """Requirement 8. `anchored`: the claim carries a structure_digest, so an agent may renew it."""
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

    owner = block.get("owner")
    kind = block.get("owner_kind", "person")
    if kind not in ("person", "agent"):
        rep.err(f"{ipath}.owner_kind", f"'{kind}' is not an owner kind — use person or agent")
    elif kind == "agent":
        check_agent_owner(rep, ipath, block, anchored)
    else:
        for key in ("operator", "policy_ref", "renews"):
            if key in block:
                rep.err(
                    f"{ipath}.{key}",
                    f"`{key}` belongs to an agent's renewal; a person who owns a claim answers for "
                    "it directly",
                )
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
        rep.err(f"{ipath}.rationale", "missing or too short — say why this claim holds")
    if block.get("expires_at") is None:
        rep.warn(f"{ipath}.expires_at", "no expiry set; acceptable only for a point-in-time claim")


def check_agent_owner(rep, ipath, block, anchored):
    """The parts of an agent's renewal the block alone can answer for; check_agent_claims does the rest."""
    if not anchored:
        rep.err(
            f"{ipath}.owner_kind",
            "an agent may only renew a claim anchored to a structure, and a privacy declaration "
            "has no structure_digest. A declaration is a person's to sign",
        )
        return
    operator = block.get("operator")
    if operator is None:
        rep.err(
            f"{ipath}.operator",
            "missing required `operator` — an agent owner needs a named person who can be asked "
            "what it was thinking",
        )
    elif not isinstance(operator, str) or len(operator.strip()) < 3:
        rep.err(f"{ipath}.operator", "too short — name the person who operates the agent")
    elif TEAM_RE.search(operator):
        rep.err(
            f"{ipath}.operator",
            f"'{operator}' reads as a team alias — the operator must be a named person, because a "
            "team cannot be asked anything",
        )
    elif operator.strip() == str(block.get("owner", "")).strip():
        rep.err(f"{ipath}.operator", "is the agent itself — an agent cannot operate itself")
    if not isinstance(block.get("policy_ref"), str) or not block.get("policy_ref"):
        rep.err(
            f"{ipath}.policy_ref",
            "missing required `policy_ref` — name the grant in .noru/interpretation-policy.yml "
            "the agent acted under",
        )
    renews = block.get("renews")
    if not isinstance(renews, dict):
        rep.err(
            f"{ipath}.renews",
            "missing required `renews` — an agent may only renew, so name the person's decision "
            "it renews (owner and decided_at)",
        )
    else:
        check_unknown_keys(rep, f"{ipath}.renews", renews, RENEWS_KEYS)
        if not isinstance(renews.get("owner"), str) or len(renews["owner"].strip()) < 3:
            rep.err(f"{ipath}.renews.owner", "missing or too short — name the person who signed")
        renewed = parse_date(renews.get("decided_at"))
        decided = parse_date(block.get("decided_at"))
        if renewed is None:
            rep.err(f"{ipath}.renews.decided_at", "missing or not an ISO date (YYYY-MM-DD)")
        elif decided is not None and decided < renewed:
            rep.err(
                f"{ipath}.decided_at",
                "is before the decision it renews — a renewal comes after what it renews",
            )


def check_source(rep, doc):
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



def field_names(fields, prefix=""):
    names = []
    for field in fields or []:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if not isinstance(name, str):
            continue
        path = f"{prefix}{name}"
        names.append(path)
        if isinstance(field.get("fields"), list):
            names.extend(field_names(field["fields"], f"{path}."))
    return names


def structure_digest(fields, non_personal_fields=None):
    """Mirror of structureDigest() in collect.mjs. The two must agree.

    Kept deliberately trivial for exactly that reason: the sorted dotted field names, newline
    joined, sha256. Categories are not in it — resolving a classification must not invalidate a
    signature, but adding, removing or renaming a column must.
    """
    names = set(field_names(fields))
    names.update(name for name in (non_personal_fields or []) if isinstance(name, str))
    return hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()


def parse_date(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def check_horizon(rep, path, block, horizon_days, as_of, agent_days=None):
    """expires_at is required, bounded, and — with --as-of — must not already have passed.

    `agent_days` is the shorter ceiling an agent's renewal gets, when the owner is an agent.
    """
    ipath = f"{path}.interpretation"
    decided = parse_date(block.get("decided_at"))
    expires_raw = block.get("expires_at")
    if expires_raw is None:
        rep.err(
            f"{ipath}.expires_at",
            "missing required `expires_at` — a classification nobody has re-owned is not current, "
            "and only a date says when that starts being true",
        )
        return
    expires = parse_date(expires_raw)
    if expires is None or decided is None:
        return
    if expires <= decided:
        rep.err(
            f"{ipath}.expires_at",
            f"expires on {expires.isoformat()}, on or before it was decided "
            f"({decided.isoformat()}) — a claim that expires before it is made asserts nothing",
        )
        return
    days = (expires - decided).days
    if block.get("owner_kind") == "agent" and agent_days is not None:
        if days > agent_days:
            rep.err(
                f"{ipath}.expires_at",
                f"stands for {days} days from decided_at; an agent's renewal may stand for at most "
                f"{agent_days}. The next look after an agent's is a person's, and this ceiling is "
                "what bounds how long that waits",
            )
    elif days > horizon_days:
        rep.err(
            f"{ipath}.expires_at",
            f"stands for {days} days from decided_at; this claim may stand for at most "
            f"{horizon_days}. The anchor is decided_at rather than the day the schema was read, "
            "which is honest here only because structure_digest pins what the claim is about: a "
            "signature cannot outlive the structure it was given for",
        )
    if as_of is not None and expires < as_of:
        rep.err(
            f"{ipath}.expires_at",
            f"this claim expired on {expires.isoformat()}, before the --as-of date "
            f"{as_of.isoformat()} — review it and sign again rather than pushing the old one",
        )


def check_categories(rep, path, node, vocab):
    """Every data category must be a real Fideslang key. A typo is an error, not a new category."""
    cats = node.get("data_categories")
    if cats is None:
        return []
    if not isinstance(cats, list):
        rep.err(f"{path}.data_categories", "must be a list")
        return []
    for i, cat in enumerate(cats):
        if not isinstance(cat, str) or cat not in vocab["data_categories"]:
            rep.err(
                f"{path}.data_categories[{i}]",
                f"unknown fideslang data category '{cat}'"
                + suggest(str(cat), vocab["data_categories"]),
            )
    return cats


def check_fields(rep, path, fields, vocab, counts):
    if not isinstance(fields, list):
        rep.err(f"{path}.fields", "must be a list")
        return
    seen = set()
    for i, field in enumerate(fields):
        fpath = f"{path}.fields[{i}]"
        if not isinstance(field, dict):
            rep.err(fpath, "field must be a mapping")
            continue
        check_unknown_keys(rep, fpath, field, FIELD_KEYS)
        name = field.get("name")
        if not name or not isinstance(name, str):
            rep.err(f"{fpath}.name", "missing required `name`")
        elif name in seen:
            rep.err(f"{fpath}.name", f"duplicate field '{name}' in this collection")
        else:
            seen.add(name)
        counts["fields"] += 1

        cats = check_categories(rep, fpath, field, vocab)
        # A field says where it came from or it is not a claim about this repository.
        check_refs(rep, fpath, field)
        if field.get("needs_review") is True:
            counts["needs_review"] += 1
            rep.err(
                f"{fpath}.needs_review",
                "still true — the collector could not classify this field and nobody has. "
                "Classify the field, record scoped accepted uncertainty, or accept it as non-personal so reconciliation can compact "
                "it into `non_personal_fields`. Then remove the flag",
            )
        elif not cats:
            has_review_child = any(
                child.get("needs_review") is True
                or bool(child.get("data_categories"))
                or bool(child.get("fields"))
                for child in (field.get("fields") or [])
                if isinstance(child, dict)
            )
            if not has_review_child and not field.get("meta", {}).get("accepted_uncertainties"):
                # Accepted 0.8.0 manifests used full empty-category objects. Keep them valid so
                # reconciliation can compact them without discarding the accepted decision.
                rep.warn(
                    f"{fpath}.data_categories",
                    "empty — accepted as non-personal; the next reconciliation compacts this name",
                )
        # Nested fields: a JSON column, an embedded document, a protobuf sub-message.
        if "fields" in field:
            check_fields(rep, fpath, field["fields"], vocab, counts)


def check_datasets(rep, doc, vocab, counts, as_of):
    datasets = doc.get("dataset")
    if datasets is None:
        rep.err("dataset", "missing required `dataset` (an empty list is a valid answer)")
        return []
    if not isinstance(datasets, list):
        rep.err("dataset", "must be a list")
        return []

    keys = []
    seen = set()
    for i, dataset in enumerate(datasets):
        path = f"dataset[{i}]"
        if not isinstance(dataset, dict):
            rep.err(path, "dataset must be a mapping")
            continue
        check_unknown_keys(rep, path, dataset, DATASET_KEYS)
        key = dataset.get("fides_key")
        if not key or not isinstance(key, str) or not FIDES_KEY_RE.match(key):
            rep.err(
                f"{path}.fides_key",
                f"'{key}' does not match the Fides key pattern [A-Za-z0-9_.<>-]",
            )
        elif key in seen:
            rep.err(f"{path}.fides_key", f"duplicate dataset key '{key}'")
        else:
            seen.add(key)
            keys.append(key)
        if not dataset.get("name"):
            rep.err(f"{path}.name", "missing required `name` — a key nobody can read is not a map")

        collections = dataset.get("collections")
        if collections is None:
            rep.err(f"{path}.collections", "missing required `collections`")
            continue
        if not isinstance(collections, list):
            rep.err(f"{path}.collections", "must be a list")
            continue
        for j, collection in enumerate(collections):
            cpath = f"{path}.collections[{j}]"
            if not isinstance(collection, dict):
                rep.err(cpath, "collection must be a mapping")
                continue
            check_unknown_keys(rep, cpath, collection, COLLECTION_KEYS)
            if not collection.get("name"):
                rep.err(f"{cpath}.name", "missing required `name`")
            counts["collections"] += 1
            # The collection is the claim unit: one person signs for "these are the categories in
            # this table". Per-field attribution would be five hundred blocks on a five-hundred
            # column schema, which is a form nobody fills in.
            check_refs(rep, cpath, collection)
            check_interpretation(rep, cpath, collection, anchored=True)

            compact = collection.get("non_personal_fields", [])
            if not isinstance(compact, list):
                rep.err(f"{cpath}.non_personal_fields", "must be a list of dotted field names")
                compact = []
            else:
                seen_compact = set()
                verbose_names = set(field_names(collection.get("fields", [])))
                for k, name in enumerate(compact):
                    npath = f"{cpath}.non_personal_fields[{k}]"
                    if not isinstance(name, str) or not name or name.startswith(".") or name.endswith("."):
                        rep.err(npath, "must be a non-empty dotted field name")
                    elif name in seen_compact:
                        rep.err(npath, f"duplicate compact non-personal field '{name}'")
                    elif name in verbose_names:
                        rep.err(npath, f"'{name}' is also represented in fields")
                    else:
                        seen_compact.add(name)
                counts["non_personal_fields"] += len(seen_compact)

            # The structural anchor. A signature is given for a set of columns; if that set has
            # changed, the signature is not a statement about this table any more. Recomputed here
            # rather than trusted, so editing the fields without re-running :scan is caught in the
            # same breath as editing the digest by hand.
            stamped = collection.get("structure_digest")
            actual = structure_digest(collection.get("fields", []), compact)
            if not isinstance(stamped, str) or not DIGEST_RE.match(stamped or ""):
                rep.err(
                    f"{cpath}.structure_digest",
                    "missing or malformed — every collection carries the digest of the field names "
                    "its signature was given for. Re-run :scan to stamp it",
                )
            elif stamped != actual:
                rep.err(
                    f"{cpath}.structure_digest",
                    f"does not match this collection's fields (stamped {stamped[:12]}, computed "
                    f"{actual[:12]}) — a column was added, removed or renamed since this was "
                    "signed, so the signature is no longer a statement about this table. Re-run "
                    ":scan, review what changed, and sign again",
                )

            block = collection.get("interpretation")
            if isinstance(block, dict):
                def categories(items):
                    found = []
                    for field in items or []:
                        if not isinstance(field, dict):
                            continue
                        found.extend(field.get("data_categories") or [])
                        found.extend(categories(field.get("fields") or []))
                    return found

                cats = categories(collection.get("fields", []))
                special = any(c in vocab["special_categories"] for c in cats)
                tier = "special_category" if special else "standard"
                check_horizon(
                    rep, cpath, block, vocab["horizon_days"][tier], as_of,
                    vocab["agent_horizon_days"][tier],
                )

            if collection.get("needs_review") is True:
                counts["needs_review"] += 1
                rep.err(
                    f"{cpath}.needs_review",
                    "still true — nobody has reviewed this collection's classification. Review it "
                    "and remove the flag before pushing",
                )
            check_fields(rep, cpath, collection.get("fields", []), vocab, counts)
    return keys


def check_systems(rep, doc, vocab, dataset_keys, counts, as_of):
    systems = doc.get("system")
    if systems is None:
        rep.err("system", "missing required `system` (an empty list is a valid answer)")
        return
    if not isinstance(systems, list):
        rep.err("system", "must be a list")
        return

    seen = set()
    for i, system in enumerate(systems):
        path = f"system[{i}]"
        if not isinstance(system, dict):
            rep.err(path, "system must be a mapping")
            continue
        check_unknown_keys(rep, path, system, SYSTEM_KEYS)
        key = system.get("fides_key")
        if not key or not isinstance(key, str) or not FIDES_KEY_RE.match(key):
            rep.err(
                f"{path}.fides_key",
                f"'{key}' does not match the Fides key pattern [A-Za-z0-9_.<>-]",
            )
        elif key in seen:
            rep.err(f"{path}.fides_key", f"duplicate system key '{key}'")
        else:
            seen.add(key)
        if not system.get("name"):
            rep.err(f"{path}.name", "missing required `name`")
        counts["systems"] += 1

        # A reference to a dataset this manifest does not define is a dangling edge in the map:
        # the graph would show a system reading from a store nothing describes.
        refs = system.get("dataset_references") or []
        if not isinstance(refs, list):
            rep.err(f"{path}.dataset_references", "must be a list")
        else:
            for j, ref in enumerate(refs):
                if ref not in dataset_keys:
                    rep.err(
                        f"{path}.dataset_references[{j}]",
                        f"'{ref}' is not a dataset defined in this manifest"
                        + suggest(str(ref), dataset_keys),
                    )

        for direction in ("ingress", "egress"):
            flows = system.get(direction, [])
            if not isinstance(flows, list):
                rep.err(f"{path}.{direction}", "must be a list")
                continue
            for flow in flows:
                if not isinstance(flow, dict) or set(flow) - {"fides_key", "type", "data_categories"} or flow.get("type") != "system" or flow.get("fides_key") not in {s.get("fides_key") for s in systems if isinstance(s, dict)}:
                    rep.err(f"{path}.{direction}", "flow must reference a system defined in this manifest")
                elif not isinstance(flow.get("data_categories", []), list) or any(c not in vocab["data_categories"] for c in flow.get("data_categories", [])):
                    rep.err(f"{path}.{direction}", "flow contains invalid data categories")

        declarations = system.get("privacy_declarations")
        if declarations is None:
            rep.err(f"{path}.privacy_declarations", "missing required `privacy_declarations`")
            continue
        if not isinstance(declarations, list):
            rep.err(f"{path}.privacy_declarations", "must be a list")
            continue
        for j, decl in enumerate(declarations):
            dpath = f"{path}.privacy_declarations[{j}]"
            if not isinstance(decl, dict):
                rep.err(dpath, "privacy declaration must be a mapping")
                continue
            check_unknown_keys(rep, dpath, decl, DECLARATION_KEYS)
            if not decl.get("name"):
                rep.err(f"{dpath}.name", "missing required `name` — say what this processing is for")
            for direction in ("ingress", "egress"):
                links = decl.get(direction, [])
                if not isinstance(links, list) or any(link not in {s.get("fides_key") for s in systems if isinstance(s, dict)} for link in links):
                    rep.err(f"{dpath}.{direction}", "must list systems defined in this manifest")
            use = decl.get("data_use")
            if not use:
                rep.err(f"{dpath}.data_use", "missing required `data_use`")
            elif use not in vocab["data_use"]:
                rep.err(
                    f"{dpath}.data_use",
                    f"unknown fideslang data use '{use}'" + suggest(str(use), vocab["data_use"]),
                )
            subjects = decl.get("data_subjects")
            if not subjects:
                rep.err(f"{dpath}.data_subjects", "missing required `data_subjects`")
            elif not isinstance(subjects, list):
                rep.err(f"{dpath}.data_subjects", "must be a list")
            else:
                for k, subject in enumerate(subjects):
                    if subject not in vocab["data_subjects"]:
                        rep.err(
                            f"{dpath}.data_subjects[{k}]",
                            f"unknown fideslang data subject '{subject}'"
                            + suggest(str(subject), vocab["data_subjects"]),
                        )
            cats = check_categories(rep, dpath, decl, vocab)
            if not cats:
                rep.err(f"{dpath}.data_categories", "identify the data categories used for this processing purpose before acceptance")
            check_refs(rep, dpath, decl)
            check_interpretation(rep, dpath, decl, anchored=False)
            block = decl.get("interpretation")
            if isinstance(block, dict):
                special = any(c in vocab["special_categories"] for c in cats)
                horizon = vocab["horizon_days"][
                    "special_category" if special else "standard"
                ]
                check_horizon(rep, dpath, block, horizon, as_of)
            if decl.get("needs_review") is True:
                counts["needs_review"] += 1
                rep.err(
                    f"{dpath}.needs_review",
                    "still true — the collector cannot know what a system uses data for. Name the "
                    "purpose, the data use and the subjects, then remove the flag",
                )


def observed_structures(derived):
    structures = {}
    for dataset in derived.get("datasets") or []:
        key = dataset.get("fides_key")
        for collection in dataset.get("collections") or []:
            identity = f"{key}/{collection.get('name')}"
            structures[identity] = set(field_names(collection.get("fields") or []))
    return structures


def manifest_structures(doc):
    structures = {}
    for dataset in doc.get("dataset") or []:
        key = dataset.get("fides_key")
        for collection in dataset.get("collections") or []:
            identity = f"{key}/{collection.get('name')}"
            names = set(field_names(collection.get("fields") or []))
            names.update(
                name for name in (collection.get("non_personal_fields") or [])
                if isinstance(name, str)
            )
            structures[identity] = names
    return structures


# `--base=` with no path: a base revision was required and could not be read.
BASE_UNAVAILABLE = object()


def without(node, keys):
    """`node` with every mapping key in `keys` removed, at any depth."""
    if isinstance(node, dict):
        return {k: without(v, keys) for k, v in node.items() if k not in keys}
    if isinstance(node, list):
        return [without(v, keys) for v in node]
    return node


def same(left, right, keys):
    return json.dumps(without(left, keys), sort_keys=True) == json.dumps(
        without(right, keys), sort_keys=True
    )


def policy_grants(rep, policy, vocab):
    """Validate the interpretation policy and return this piece's usable grants, by id.

    Judged only when a claim relies on it. A grant for one of the NEVER_AGENT pieces is an error
    whichever piece reads the file; a grant for any other piece is that piece's validator's to judge.
    """
    document, label = policy
    if not isinstance(document, dict):
        rep.err(label, "must be a mapping with `kind`, `version` and `grants`")
        return {}
    check_unknown_keys(rep, label, document, POLICY_KEYS)
    if document.get("kind") != "interpretation-policy":
        rep.err(f"{label}.kind", "must be 'interpretation-policy'")
    if document.get("version") != 1:
        rep.err(f"{label}.version", "must be 1")
    grants = document.get("grants")
    if not isinstance(grants, list):
        rep.err(f"{label}.grants", "must be a list")
        return {}
    ceiling = vocab["agent_horizon_days"]["standard"]
    usable = {}
    seen = set()
    for i, grant in enumerate(grants):
        gpath = f"{label}.grants[{i}]"
        if not isinstance(grant, dict):
            rep.err(gpath, "must be a mapping")
            continue
        check_unknown_keys(rep, gpath, grant, GRANT_KEYS)
        ok = True
        gid = grant.get("id")
        if not isinstance(gid, str) or not KEY_RE.match(gid):
            rep.err(f"{gpath}.id", f"'{gid}' is not a key ([a-z0-9][a-z0-9._-]*)")
            ok = False
        elif gid in seen:
            rep.err(f"{gpath}.id", f"duplicate grant id '{gid}'")
            ok = False
        else:
            seen.add(gid)
        piece = grant.get("piece")
        if piece in NEVER_AGENT:
            rep.err(
                f"{gpath}.piece",
                f"{piece} never accepts an agent owner: its owners hold a duty that exists to be "
                "held apart from someone else's, and an agent cannot hold it",
            )
            continue
        if piece != PIECE:
            continue
        if grant.get("claims") != AGENT_CLAIMS:
            rep.err(
                f"{gpath}.claims",
                "must be [renewal]. An agent may only renew a claim whose structure a person "
                "signed; new claims, changed claims and drift acknowledgements are a person's, "
                "and no policy can grant them",
            )
            ok = False
        agents = grant.get("agents")
        if not isinstance(agents, list) or not agents or not all(
            isinstance(a, str) and len(a.strip()) >= 3 for a in agents
        ):
            rep.err(f"{gpath}.agents", "must list the agent identities this grant covers")
            ok = False
            agents = []
        operators = grant.get("operators")
        if not isinstance(operators, list) or not operators:
            rep.err(
                f"{gpath}.operators",
                "must name at least one person who answers for the agents under this grant",
            )
            ok = False
        else:
            for k, operator in enumerate(operators):
                opath = f"{gpath}.operators[{k}]"
                if not isinstance(operator, str) or len(operator.strip()) < 3:
                    rep.err(opath, "missing or too short — name a person")
                    ok = False
                elif TEAM_RE.search(operator):
                    rep.err(
                        opath,
                        f"'{operator}' reads as a team alias — an operator must be a named person",
                    )
                    ok = False
                elif operator in agents:
                    rep.err(opath, f"'{operator}' is also listed as an agent")
                    ok = False
        max_days = grant.get("max_days")
        if isinstance(max_days, bool) or not isinstance(max_days, int) or max_days < 1:
            rep.err(f"{gpath}.max_days", "must be a whole number of days, at least 1")
            ok = False
        elif max_days > ceiling:
            rep.err(
                f"{gpath}.max_days",
                f"{max_days} exceeds this piece's agent ceiling of {ceiling} days; a policy may "
                "shorten it, never lengthen it",
            )
            ok = False
        if ok:
            usable[gid] = grant
    return usable


def check_agent_claims(rep, doc, vocab, policy, base):
    """Every agent-owned collection: inside a grant, and a renewal of what a person signed."""
    claims = []
    for i, dataset in enumerate(doc.get("dataset") or []):
        if not isinstance(dataset, dict):
            continue
        for j, collection in enumerate(dataset.get("collections") or []):
            block = collection.get("interpretation") if isinstance(collection, dict) else None
            if isinstance(block, dict) and block.get("owner_kind") == "agent":
                key = (dataset.get("fides_key"), collection.get("name"))
                claims.append((f"dataset[{i}].collections[{j}].interpretation", key, collection))
    if not claims:
        return

    grants = None if policy is None else policy_grants(rep, policy, vocab)
    before = {}
    if isinstance(base, dict):
        for dataset in base.get("dataset") or []:
            if not isinstance(dataset, dict):
                continue
            for collection in dataset.get("collections") or []:
                if isinstance(collection, dict):
                    before[(dataset.get("fides_key"), collection.get("name"))] = collection

    for ipath, key, collection in claims:
        block = collection["interpretation"]
        check_grant(rep, ipath, block, grants, policy)
        check_lineage(rep, ipath, collection, before.get(key), base)


def check_grant(rep, ipath, block, grants, policy):
    if grants is None:
        rep.err(
            f"{ipath}.policy_ref",
            "outside the interpretation policy: there is no committed "
            ".noru/interpretation-policy.yml at the base revision, so no agent may own a claim",
        )
        return
    ref = block.get("policy_ref")
    grant = grants.get(ref)
    if grant is None:
        rep.err(
            f"{ipath}.policy_ref",
            f"outside the interpretation policy: {policy[1]} has no valid {PIECE} grant '{ref}'",
        )
        return
    owner = block.get("owner")
    if owner not in grant["agents"]:
        rep.err(
            f"{ipath}.owner",
            f"outside the interpretation policy: grant '{ref}' does not cover the agent '{owner}'",
        )
    operator = block.get("operator")
    if operator is not None and operator not in grant["operators"]:
        rep.err(
            f"{ipath}.operator",
            f"outside the interpretation policy: grant '{ref}' does not name '{operator}' as an "
            "operator",
        )
    decided = parse_date(block.get("decided_at"))
    expires = parse_date(block.get("expires_at"))
    if decided and expires and (expires - decided).days > grant["max_days"]:
        rep.err(
            f"{ipath}.expires_at",
            f"stands for {(expires - decided).days} days; grant '{ref}' lets an agent's renewal "
            f"stand for at most {grant['max_days']}",
        )


def check_lineage(rep, ipath, collection, prior, base):
    """The renewal rule. Needs the manifest as it stood at the base revision."""
    if base is None:
        rep.warn(
            ipath,
            "an agent's renewal is not checked against the base revision without --base. CI mode "
            "checks it; do not publish it until that check has passed",
        )
        return
    if base is BASE_UNAVAILABLE:
        rep.err(
            ipath,
            "an agent's renewal can only be checked against the base revision, and none could be "
            "read. Fetch the history the check needs (the base branch, or the parent commit)",
        )
        return
    signed = prior.get("interpretation") if isinstance(prior, dict) else None
    if not isinstance(signed, dict):
        rep.err(
            ipath,
            "an agent cannot own a new claim: this collection has no signed claim at the base "
            "revision, so a person must sign it first",
        )
        return
    if same(prior, collection, ("refs",)):
        # Carried forward unchanged. It was judged against its own base when it was made.
        return
    if signed.get("owner_kind") == "agent":
        rep.err(
            ipath,
            "renews an agent's renewal — the next renewal after an agent's is a person's",
        )
        return
    if prior.get("structure_digest") != collection.get("structure_digest"):
        rep.err(
            ipath,
            f"the structure changed since a person last owned it (structure_digest "
            f"{str(prior.get('structure_digest'))[:12]} at the base revision, "
            f"{str(collection.get('structure_digest'))[:12]} now) — a changed claim is a person's "
            "to sign",
        )
        return
    if not same(prior, collection, ("refs", "interpretation")):
        rep.err(
            ipath,
            "the classification changed since a person last owned it — an agent may only renew a "
            "claim exactly as a person signed it",
        )
        return
    renews = collection["interpretation"].get("renews")
    if not isinstance(renews, dict) or (renews.get("owner"), renews.get("decided_at")) != (
        signed.get("owner"), signed.get("decided_at")
    ):
        rep.err(
            f"{ipath}.renews",
            f"does not name the decision it renews: at the base revision this claim is "
            f"{signed.get('owner')}'s, decided {signed.get('decided_at')}",
        )


def check_observed_structure(rep, doc, derived):
    observed = observed_structures(derived)
    represented = manifest_structures(doc)
    for identity in sorted(set(observed) | set(represented)):
        if identity not in represented:
            rep.err("dataset", f"observed collection '{identity}' is missing from the manifest")
            continue
        if identity not in observed:
            rep.err("dataset", f"manifest collection '{identity}' is not in the current observation")
            continue
        missing = sorted(observed[identity] - represented[identity])
        extra = sorted(name for name in represented[identity] - observed[identity] if not any(name.startswith(parent + ".") for parent in observed[identity]))
        if missing:
            rep.err(identity, f"observed field(s) missing from fields/non_personal_fields: {', '.join(missing)}")
        if extra:
            rep.err(identity, f"unobserved field(s) present in fields/non_personal_fields: {', '.join(extra)}")


def validate(doc, vocab, as_of=None, observed=None, repo=None, allow_discovery_review=False, allow_semantic_review=False, policy=None, base=None):
    rep = Report()
    counts = {
        "datasets": 0,
        "collections": 0,
        "fields": 0,
        "non_personal_fields": 0,
        "systems": 0,
        "needs_review": 0,
    }
    if not isinstance(doc, dict):
        rep.err(
            "<root>",
            "manifest must be a mapping with `version`, `piece`, `source`, `dataset`, `system`",
        )
        return rep, counts

    check_unknown_keys(rep, "<root>", doc, TOP_LEVEL_KEYS)
    version = doc.get("version")
    if not version or not isinstance(version, str) or not SEMVER_RE.match(version):
        rep.err("version", f"'{version}' is not a semantic version (for example 0.1.0)")
    if doc.get("piece") != PIECE:
        rep.err("piece", f"expected '{PIECE}', found '{doc.get('piece')}'")

    check_source(rep, doc)
    for error in migration_support.errors(doc.get("migration_support", [])):
        rep.err("migration_support", error)
    for error in dataset_inputs.dependency_gaps(doc):
        rep.err("evidence_dependencies", error)
    dataset_keys = check_datasets(rep, doc, vocab, counts, as_of)
    check_systems(rep, doc, vocab, dataset_keys, counts, as_of)
    check_agent_claims(rep, doc, vocab, policy, base)
    if isinstance(observed, dict):
        if doc.get("migration_support", []) != observed.get("migration_support", []):
            rep.err("migration_support", "source-role declarations differ from collected evidence; rerun collection")
        check_observed_structure(rep, doc, observed)
    if "relationship_graph" in doc:
        for error in relationships.validate(doc["relationship_graph"], doc.get("evidence_dependencies", [])):
            rep.err("relationship_graph", error)
    if "evidence_dependencies" in doc and not rep.errors:
        targets = {"system:" + row["fides_key"] for row in doc.get("system", []) if isinstance(row, dict) and isinstance(row.get("fides_key"), str)}
        for dataset in doc.get("dataset", []):
            for collection in dataset.get("collections", []):
                prefix = f"{dataset.get('fides_key')}/{collection.get('name')}"
                targets.add(prefix)
                def add_fields(fields, parent):
                    for field in fields:
                        targets.add(parent + field["name"])
                        add_fields(field.get("fields", []), parent + field["name"] + ".")
                add_fields(collection.get("fields", []), prefix + "/")
                targets.update(prefix + "/" + name for name in collection.get("non_personal_fields", []))
        targets.update(relationships.targets(doc.get("relationship_graph")))
        specs = doc.get("evidence_dependencies", [])
        dependency_errors = dependencies.validate_specs(specs, targets)
        for error in dependency_errors:
            rep.err("evidence_dependencies", error)
        if repo is not None and not dependency_errors:
            _observations, changes = dependencies.reconcile(repo, specs, {})
            for change in changes:
                if change["action"] != "refresh_evidence":
                    rep.err("evidence_dependencies", f"{change['id']}: {change['action']} — inspect the evidence and record its reviewed fingerprint")
    if repo is not None:
        try:
            for change in analysis_cache.check_accepted_discovery(repo, doc.get("evidence_dependencies", []), allow_discovery_review):
                rep.err("discovery", f"{change['path']}: investigate new code/configuration scope and seal the reviewed baseline before export")
        except (OSError, ValueError, TypeError, KeyError) as error:
            rep.err("discovery", f"cannot verify discovery baseline: {error}")
    if repo is not None and not allow_semantic_review:
        try:
            lock_path = pathlib.Path(repo) / ".noru" / "privacy-datamap.lock.json"
            lock = json.loads(lock_path.read_text()) if lock_path.is_file() else {}
            if analysis_cache.semantic_changes(lock.get("semantic_snapshot"), analysis_cache.semantic_snapshot(doc)):
                rep.err("semantic_review", "privacy meaning narrowed: investigate, review and seal the changed baseline before export")
        except (OSError, ValueError, TypeError, KeyError) as error:
            rep.err("semantic_review", f"cannot verify accepted meaning: {error}")
    if repo is not None:
        try:
            import field_review
            import analysis_storage
            classification_document = analysis_storage.load(pathlib.Path(repo) / ".noru/.cache/privacy-datamap.analysis.json") or {}
            classification_observed = observed or field_review.derived_for(classification_document, repo)
            if classification_observed is None:
                rep.err("classification_review", "Current classification observations are missing; recollect and investigate before acceptance or export")
            else:
                for error in field_review.validate_repository(classification_document, classification_observed, pathlib.Path(repo), require_complete=True, candidate=doc):
                    rep.err("classification_review", error)
        except (OSError, ValueError, TypeError, KeyError) as error:
            rep.err("classification_review", "Cannot verify canonical classification: " + str(error))
    counts["datasets"] = len(doc.get("dataset") or [])
    return rep, counts


USAGE = (
    "usage: validate_manifest.py <manifest.yml> [--as-of=YYYY-MM-DD] [--output=json] [--quiet] "
    "[--emit-parsed=<path.json> | --schema-only] [--base=<manifest.yml>] "
    "[--interpretation-policy=<path>]\n"
)


def load_side_document(path):
    """A base manifest or a policy file. An empty file stands for one that did not exist there."""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    if not text.strip():
        return None
    return load_yaml(text)[0]


def main(argv):
    output_json = False
    quiet = False
    emit_parsed = None
    schema_only = False
    as_of = None
    base_arg = None
    policy_arg = None
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
        elif arg == "--schema-only":
            schema_only = True
        elif arg.startswith("--base="):
            base_arg = arg.split("=", 1)[1]
        elif arg.startswith("--interpretation-policy="):
            policy_arg = arg.split("=", 1)[1]
        elif arg.startswith("--as-of="):
            as_of = parse_date(arg.split("=", 1)[1])
            if as_of is None:
                sys.stderr.write(
                    f"error: --as-of must be an ISO date (YYYY-MM-DD), got "
                    f"'{arg.split('=', 1)[1]}'\n"
                )
                return 2
        elif arg in ("-h", "--help"):
            sys.stdout.write(USAGE)
            return 0
        elif arg.startswith("-"):
            sys.stderr.write(f"error: unknown option '{arg}'\n" + USAGE)
            return 2
        else:
            positional.append(arg)

    if schema_only and emit_parsed is not None:
        sys.stderr.write("error: --schema-only cannot emit exportable parsed output; use repository validation for acceptance/export\n")
        return 2
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

    # The policy defaults to the one beside the manifest. CI mode passes the base revision's copy,
    # so a pull request that widens the policy is judged by the policy it would replace.
    base = None
    policy = None
    policy_path = path.parent / "interpretation-policy.yml" if policy_arg is None else policy_arg
    try:
        if base_arg == "":
            base = BASE_UNAVAILABLE
        elif base_arg is not None:
            base = load_side_document(base_arg) or {}
        if policy_path and pathlib.Path(policy_path).is_file():
            document = load_side_document(policy_path)
            if document is not None:
                policy = (document, "interpretation-policy")
        elif policy_arg:
            raise OSError(f"no such file: {policy_arg}")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"error: could not read the base manifest or interpretation policy ({exc})\n")
        return 2

    observed = None
    derived_path = path.parent / ".cache" / "privacy-datamap.derived.json"
    if not schema_only and derived_path.is_file():
        try:
            observed = json.loads(derived_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"error: could not load current derived facts ({exc})\n")
            return 2

    rep, counts = validate(
        doc, vocab, as_of, observed, None if schema_only else path.resolve().parent.parent,
        policy=policy, base=base,
    )
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
                    "scope": "schema_only" if schema_only else "repository_classification",
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
        if schema_only:
            print("Schema-only validation: this does not validate classification or permit acceptance/export.")
        for p, m in rep.warnings:
            print(f"  WARN  {p}: {m}")
    for p, m in rep.errors:
        print(f"  ERROR {p}: {m}")
    if not ok:
        print(f"\nFAILED: {len(rep.errors)} error(s), {len(rep.warnings)} warning(s).")
        return 1
    if not quiet:
        print(
            f"\nOK: {counts['datasets']} dataset(s), {counts['collections']} collection(s), "
            f"{counts['fields']} review field(s), {counts['non_personal_fields']} compact "
            f"non-personal field(s), {counts['systems']} system(s), all keys valid "
            f"({len(rep.warnings)} warning(s))."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
