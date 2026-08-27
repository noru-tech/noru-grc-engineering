#!/usr/bin/env python3
"""Validate .noru/ai-inventory.yml against the BUNDLED vocabulary.

Self-contained and atomic:
  * Python standard library only. No pip install, no venv, no network.
  * Valid keys come from ../references/vocabulary.json and
    ../references/taxonomy/data_categories.json (provenance in taxonomy/SOURCE.md).
  * Parses YAML with PyYAML if it happens to be importable, otherwise a bundled fallback loader,
    so it runs anywhere python3 exists.

The rule that makes this piece worth trusting (contract requirement 8): every AI system, every
provider, every provider claim and every finding must carry `refs[]` citing the repository lines
that produced it AND a complete `interpretation` block naming the person who stands behind it. An
unattributed claim is an ERROR, never a warning.

The `findings` block is ordered, and this file enforces the order: prohibited practices, then
transparency obligations, then role and risk tier, then standards alignment. A manifest that leads
with a risk tier leads with the obligation that is furthest away, and the reader acts on what they
read first.

Usage:
    python3 validate_manifest.py <manifest.yml> [--output=json] [--quiet]
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
PIECE = "ai-inventory"

REF_RE = re.compile(r"^[^:\s][^:]*:[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

ARTICLE_RE = re.compile(r"^Article \d+(\([0-9a-z]+\))*$")
ART5_ARTICLE_RE = re.compile(r"^Article 5\(1\)\([a-h]\)$")

TOP_LEVEL_KEYS = {"version", "piece", "source", "ai_systems", "providers", "findings"}
SOURCE_KEYS = {"slug", "commit_sha", "branch", "generated_by", "derived_digest"}
INTERPRETATION_KEYS = {
    "owner", "decided_at", "expires_at", "next_review_due", "rationale", "refs",
}
SYSTEM_KEYS = {
    "key", "name", "purpose", "provider", "models", "deployment", "autonomy",
    "human_oversight", "inputs", "outputs", "retrieval", "evals", "data_categories",
    "refs", "interpretation", "needs_review",
}
PROVIDER_KEYS = {
    "key", "vendor_name", "category", "endpoints", "claims", "dpa_reference",
    "refs", "interpretation",
}
CLAIM_KEYS = {"kind", "value", "source", "interpretation"}
CLAIM_SOURCE_KEYS = {"type", "ref", "url", "document", "retrieved_on"}

PROHIBITED_KEYS = {
    "system", "practice", "article", "determination", "action", "status", "needs_review",
    "refs", "interpretation",
}
TRANSPARENCY_KEYS = {
    "system", "trigger", "article", "required_action", "applies_to_role", "disclosure", "status",
    "needs_review", "refs", "interpretation",
}
DISCLOSURE_KEYS = {"state", "mechanism", "gap", "searched", "refs"}
ROLE_RISK_KEYS = {
    "system", "role", "role_article", "tier", "tier_article", "annex_iii_area",
    "not_high_risk_assessment", "enforceable_from", "status", "needs_review", "refs",
    "interpretation",
}
ART_6_3_KEYS = {"ground", "profiling", "article", "refs"}
STANDARDS_KEYS = {
    "system", "scheme", "value", "reference", "status", "needs_review", "refs", "interpretation",
}

# Article 50 pairs a trigger with a specific required action. Recording a trigger without the
# action it demands is how a scan ends up reporting a label instead of an obligation.
TRIGGER_REQUIRED_ACTION = {
    "direct_human_interaction": {"inform_natural_person"},
    "synthetic_content_generation": {"machine_readable_marking"},
    "emotion_recognition": {"inform_natural_person"},
    "biometric_categorisation": {"inform_natural_person"},
    "deep_fake": {"disclose_artificial_content"},
    "public_interest_text": {"disclose_artificial_content"},
}

# Claim kinds whose truth depends on a configuration that can silently change. Requirement 8
# scopes `expires_at` to exactly these. A procedural claim may name `next_review_due` instead --
# same intent, a date a validator can compare -- but naming neither is an error, not a warning.
TECHNICAL_CLAIM_KINDS = {"no_training", "zero_retention", "retention_period", "data_residency"}


def load_vocabulary():
    vocab = json.loads((REFERENCES / "vocabulary.json").read_text(encoding="utf-8"))
    rows = json.loads(
        (REFERENCES / "taxonomy" / "data_categories.json").read_text(encoding="utf-8")
    )
    vocab["data_categories"] = [r["fides_key"] for r in rows]
    return vocab


def check_unknown_keys(rep, path, obj, allowed):
    for key in obj:
        if key not in allowed:
            rep.err(f"{path}.{key}", f"unknown key '{key}'" + suggest(key, allowed))


def check_refs(rep, path, obj, required=True):
    """refs[] is the citation trail. Requirement 8: no citation, no claim."""
    refs = obj.get("refs")
    if refs is None:
        if required:
            rep.err(
                path,
                "missing required `refs` — every claim must cite the repository lines "
                "(file:line) that produced it",
            )
        return
    if not isinstance(refs, list):
        rep.err(f"{path}.refs", "must be a list of 'file:line' strings")
        return
    if required and len(refs) == 0:
        rep.err(f"{path}.refs", "must not be empty — an unattributed claim is an error")
    for i, ref in enumerate(refs):
        if not isinstance(ref, str) or not REF_RE.match(ref):
            rep.err(
                f"{path}.refs[{i}]",
                f"'{ref}' is not a 'file:line' citation (for example packages/x/y.ts:42)",
            )


def check_interpretation(rep, path, obj, expiry_required):
    """Requirement 8: owner / decided_at / expires_at / rationale, plus refs."""
    block = obj.get("interpretation")
    if block is None:
        rep.err(
            path,
            "missing required `interpretation` — name the person who decided this, when, "
            "until when, and why (owner, decided_at, expires_at, rationale)",
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

    for field in ("decided_at", "expires_at", "next_review_due"):
        value = block.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not DATE_RE.match(value):
            rep.err(f"{ipath}.{field}", f"'{value}' is not an ISO date (YYYY-MM-DD)")

    if block.get("decided_at") is None:
        rep.err(f"{ipath}.decided_at", "missing required `decided_at` (YYYY-MM-DD)")

    rationale = block.get("rationale")
    if not rationale or not isinstance(rationale, str) or len(rationale.strip()) < 10:
        rep.err(
            f"{ipath}.rationale",
            "missing or too short — say why this claim holds, in a sentence a reviewer can argue with",
        )

    if block.get("expires_at") is None:
        if expiry_required:
            rep.err(
                f"{ipath}.expires_at",
                "missing required `expires_at` — this is a technical claim, and a technical claim "
                "goes stale when the configuration behind it changes",
            )
        elif block.get("next_review_due") is None:
            rep.err(
                f"{ipath}.expires_at",
                "no `expires_at` and no `next_review_due` — a procedural claim runs on a review "
                "cadence, so say when someone must look at this again. An open-ended claim is one "
                "nobody will ever revisit",
            )

    if isinstance(block.get("refs"), list):
        check_refs(rep, ipath, block, required=False)


def check_enum(rep, path, value, allowed, label, required=True):
    if value is None:
        if required:
            rep.err(path, f"missing required `{label}`")
        return
    if value not in allowed:
        rep.err(path, f"unknown {label} '{value}'" + suggest(value, allowed))


def check_data_categories(rep, path, obj, vocab):
    cats = obj.get("data_categories")
    if cats is None:
        return
    if not isinstance(cats, list):
        rep.err(f"{path}.data_categories", "must be a list")
        return
    for c in cats:
        if c not in vocab["data_categories"]:
            rep.err(
                f"{path}.data_categories",
                f"unknown fideslang data category '{c}'" + suggest(c, vocab["data_categories"]),
            )


def check_source(rep, doc):
    src = doc.get("source")
    if not isinstance(src, dict):
        rep.err("source", "missing required `source` block (slug, commit_sha, branch, generated_by)")
        return
    check_unknown_keys(rep, "source", src, SOURCE_KEYS)
    for field in ("slug", "commit_sha", "branch", "generated_by"):
        value = src.get(field)
        if not value or not isinstance(value, str):
            rep.err(
                f"source.{field}",
                f"missing required `{field}` — push provenance is not optional (requirement 4)",
            )
    sha = src.get("commit_sha")
    if isinstance(sha, str) and 0 < len(sha) < 7:
        rep.err("source.commit_sha", f"'{sha}' is too short to identify a commit")
    digest = src.get("derived_digest")
    if digest is not None and (
        not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        rep.err("source.derived_digest", "must be a lowercase sha256 hex digest")


def check_system(rep, path, sysobj, vocab, provider_keys):
    if not isinstance(sysobj, dict):
        rep.err(path, "ai system must be a mapping")
        return None
    check_unknown_keys(rep, path, sysobj, SYSTEM_KEYS)

    key = sysobj.get("key")
    if not key or not isinstance(key, str) or not KEY_RE.match(key):
        rep.err(
            f"{path}.key",
            f"'{key}' is not a stable lowercase key — it becomes half of the Noru asset upsert key, "
            "so it must not change between scans",
        )
    if not sysobj.get("name"):
        rep.err(f"{path}.name", "missing required `name`")
    purpose = sysobj.get("purpose")
    if not purpose or not isinstance(purpose, str) or len(purpose.strip()) < 10:
        rep.err(f"{path}.purpose", "missing or too short — say what this system is for")

    check_enum(rep, f"{path}.deployment", sysobj.get("deployment"), vocab["deployment"], "deployment")
    check_enum(rep, f"{path}.autonomy", sysobj.get("autonomy"), vocab["autonomy"], "autonomy")

    provider = sysobj.get("provider")
    if provider is not None and provider not in provider_keys:
        rep.err(
            f"{path}.provider",
            f"'{provider}' is not declared in providers[]" + suggest(provider, provider_keys),
        )

    models = sysobj.get("models")
    if models is not None and not isinstance(models, list):
        rep.err(f"{path}.models", "must be a list of model identifiers")

    for i, point in enumerate(rep.aslist(sysobj.get("human_oversight"))):
        opath = f"{path}.human_oversight[{i}]"
        if not isinstance(point, dict):
            rep.err(opath, "oversight point must be a mapping")
            continue
        check_enum(rep, f"{opath}.type", point.get("type"), vocab["oversight_type"], "oversight type")
        if not point.get("description"):
            rep.err(f"{opath}.description", "missing required `description`")
        check_refs(rep, opath, point)

    if sysobj.get("autonomy") == "autonomous" and not rep.aslist(sysobj.get("human_oversight")):
        rep.warn(
            f"{path}.human_oversight",
            "an autonomous system with no declared oversight point is exactly what an auditor asks about",
        )

    for flow in ("inputs", "outputs"):
        node = sysobj.get(flow)
        if node is None:
            continue
        if not isinstance(node, dict):
            rep.err(f"{path}.{flow}", "must be a mapping")
            continue
        check_data_categories(rep, f"{path}.{flow}", node, vocab)

    for i, source in enumerate(rep.aslist(sysobj.get("retrieval"))):
        rpath = f"{path}.retrieval[{i}]"
        if not isinstance(source, dict):
            rep.err(rpath, "retrieval source must be a mapping")
            continue
        if not source.get("name"):
            rep.err(f"{rpath}.name", "missing required `name`")
        check_enum(rep, f"{rpath}.kind", source.get("kind"), vocab["retrieval_kind"], "retrieval kind")
        check_data_categories(rep, rpath, source, vocab)
        check_refs(rep, rpath, source)

    evals = sysobj.get("evals")
    if evals is not None:
        epath = f"{path}.evals"
        if not isinstance(evals, dict):
            rep.err(epath, "must be a mapping with `suites` and `ci_gated`")
        else:
            if not isinstance(evals.get("ci_gated"), bool):
                rep.err(
                    f"{epath}.ci_gated",
                    "missing required boolean `ci_gated` — an eval suite nothing gates on is not a control",
                )
            for i, suite in enumerate(rep.aslist(evals.get("suites"))):
                spath = f"{epath}.suites[{i}]"
                if not isinstance(suite, dict):
                    rep.err(spath, "eval suite must be a mapping")
                    continue
                if not suite.get("name"):
                    rep.err(f"{spath}.name", "missing required `name`")
                if not suite.get("path"):
                    rep.err(f"{spath}.path", "missing required `path`")

    check_data_categories(rep, path, sysobj, vocab)
    check_refs(rep, path, sysobj)
    check_interpretation(rep, path, sysobj, expiry_required=True)

    if sysobj.get("needs_review") is True:
        rep.err(
            f"{path}.needs_review",
            "still true — the collector could not derive a field here and a human has not resolved it; "
            "resolve it and remove the flag before pushing",
        )
    return key


def check_provider(rep, path, provider, vocab):
    if not isinstance(provider, dict):
        rep.err(path, "provider must be a mapping")
        return None
    check_unknown_keys(rep, path, provider, PROVIDER_KEYS)

    key = provider.get("key")
    if not key or not isinstance(key, str) or not KEY_RE.match(key):
        rep.err(f"{path}.key", f"'{key}' is not a stable lowercase key")
    if not provider.get("vendor_name"):
        rep.err(
            f"{path}.vendor_name",
            "missing required `vendor_name` — Noru deduplicates vendors on name, so it must be stable",
        )
    if provider.get("category") is not None:
        check_enum(
            rep, f"{path}.category", provider.get("category"),
            vocab["provider_category"], "vendor category",
        )

    for i, claim in enumerate(rep.aslist(provider.get("claims"))):
        cpath = f"{path}.claims[{i}]"
        if not isinstance(claim, dict):
            rep.err(cpath, "claim must be a mapping")
            continue
        check_unknown_keys(rep, cpath, claim, CLAIM_KEYS)
        kind = claim.get("kind")
        check_enum(rep, f"{cpath}.kind", kind, vocab["claim_kind"], "claim kind")
        if not claim.get("value"):
            rep.err(f"{cpath}.value", "missing required `value`")

        csource = claim.get("source")
        if not isinstance(csource, dict):
            rep.err(
                f"{cpath}.source",
                "missing required `source` — a retention or no-training claim must say where it "
                "came from; 'the provider says so' and 'we configured it' are different claims",
            )
        else:
            check_unknown_keys(rep, f"{cpath}.source", csource, CLAIM_SOURCE_KEYS)
            stype = csource.get("type")
            check_enum(
                rep, f"{cpath}.source.type", stype, vocab["claim_source_type"], "claim source type"
            )
            if stype == "repo_config":
                ref = csource.get("ref")
                if not isinstance(ref, str) or not REF_RE.match(ref):
                    rep.err(
                        f"{cpath}.source.ref",
                        "a repo_config claim must cite the configuration line as file:line",
                    )
            elif stype in ("vendor_documentation", "vendor_assertion"):
                if not csource.get("url") and not csource.get("document"):
                    rep.err(
                        f"{cpath}.source",
                        f"a {stype} claim must record a `url` or `document` a reviewer can open",
                    )
                if not csource.get("retrieved_on"):
                    rep.warn(
                        f"{cpath}.source.retrieved_on",
                        "no retrieval date; vendor pages change without notice",
                    )
            elif stype == "unverified":
                rep.warn(
                    cpath,
                    "claim is marked unverified — it will land in Noru as a suggestion only",
                )

        check_interpretation(
            rep, cpath, claim, expiry_required=kind in TECHNICAL_CLAIM_KINDS
        )

    check_refs(rep, path, provider)
    check_interpretation(rep, path, provider, expiry_required=False)
    return key


def check_finding_common(rep, path, item, vocab, system_keys):
    """Every finding, in every category, answers the same three questions."""
    system = item.get("system")
    if system not in system_keys:
        rep.err(
            f"{path}.system",
            f"'{system}' is not declared in ai_systems[]" + suggest(system, system_keys),
        )
    check_enum(rep, f"{path}.status", item.get("status"), vocab["finding_status"], "finding status")
    if item.get("status") in ("accepted", "rejected"):
        rep.warn(
            f"{path}.status",
            "the piece only ever emits 'suggested'; accepted/rejected is a human's call in Noru",
        )
    if item.get("needs_review") is True:
        rep.err(
            f"{path}.needs_review",
            "still true — the collector proposed this finding but nobody has decided it; decide it "
            "and remove the flag before pushing",
        )
    check_refs(rep, path, item)
    check_interpretation(rep, path, item, expiry_required=True)


def check_article(rep, path, value, label, regex=ARTICLE_RE):
    if not value:
        rep.err(path, f"missing required `{label}` — cite the provision that drives this finding")
        return
    if not isinstance(value, str) or not regex.match(value):
        rep.err(
            path,
            f"'{value}' is not an article citation. Write it as it appears in the instrument, for "
            "example 'Article 50(1)' or 'Article 5(1)(f)'",
        )


def check_prohibited_practice(rep, path, item, vocab, system_keys):
    """Article 5. The one category where the answer is stop, not document."""
    if not isinstance(item, dict):
        rep.err(path, "prohibited-practice finding must be a mapping")
        return
    check_unknown_keys(rep, path, item, PROHIBITED_KEYS)

    practice = item.get("practice")
    check_enum(rep, f"{path}.practice", practice, vocab["prohibited_practice"], "practice")

    article = item.get("article")
    check_article(rep, f"{path}.article", article, "article", ART5_ARTICLE_RE)
    expected = vocab["prohibited_practice_article"].get(practice)
    if expected and article and article != expected:
        rep.err(
            f"{path}.article",
            f"'{practice}' is {expected}, not '{article}' — the citation and the practice must be "
            "the same provision or the finding cannot be checked",
        )

    determination = item.get("determination")
    check_enum(
        rep, f"{path}.determination", determination,
        vocab["prohibited_determination"], "determination",
    )
    if determination in ("indicated", "needs_legal_review"):
        action = item.get("action")
        if not action or not isinstance(action, str) or len(action.strip()) < 10:
            rep.err(
                f"{path}.action",
                "missing required `action` — a determination of "
                f"'{determination}' means someone has to do something, and this is where it is "
                "written down. Article 5 is a prohibition: the answer is to stop the practice, not "
                "to document it",
            )

    check_finding_common(rep, path, item, vocab, system_keys)


def check_disclosure(rep, path, node, vocab, trigger):
    """The Article 50 check that is actually enforceable: is the disclosure there or is it not."""
    if node is None:
        rep.err(
            path,
            "missing required `disclosure` — an Article 50 trigger recorded without saying whether "
            "the required disclosure or marking is present is the failure this category exists to "
            "prevent. Finding the model call is not the finding",
        )
        return
    if not isinstance(node, dict):
        rep.err(path, "must be a mapping with at least `state`")
        return
    check_unknown_keys(rep, path, node, DISCLOSURE_KEYS)

    state = node.get("state")
    check_enum(rep, f"{path}.state", state, vocab["disclosure_state"], "disclosure state")

    refs = node.get("refs")
    if state == "present":
        mechanism = node.get("mechanism")
        if not mechanism or not isinstance(mechanism, str) or len(mechanism.strip()) < 5:
            rep.err(
                f"{path}.mechanism",
                "missing required `mechanism` — say how the disclosure or mark is produced, so the "
                "next reader can check it rather than trust it",
            )
        if not isinstance(refs, list) or not refs:
            rep.err(
                f"{path}.refs",
                "missing required `refs` — 'the disclosure is present' is a claim about a line of "
                "code, so cite the line",
            )
    if state in ("absent", "unclear"):
        gap = node.get("gap")
        if not gap or not isinstance(gap, str) or len(gap.strip()) < 10:
            rep.err(
                f"{path}.gap",
                f"missing required `gap` — say what is missing for a '{state}' disclosure and what "
                "would close it",
            )
    if state == "unclear" and (not isinstance(refs, list) or not refs):
        rep.err(
            f"{path}.refs",
            "missing required `refs` — 'unclear' means something disclosure-shaped was found but "
            "nothing ties it to this surface, so cite what was found",
        )
    if state == "absent":
        searched = node.get("searched")
        if not isinstance(searched, list) or not searched:
            rep.err(
                f"{path}.searched",
                "missing required `searched` — a disclosure is only ever absent from somewhere. A "
                "notice rendered by a design system, a CMS, a mobile client or another repository "
                "is invisible to a repository scan, so list where the check looked before calling "
                "it a gap",
            )
    if isinstance(refs, list):
        check_refs(rep, path, node, required=False)

    if state == "present" and trigger == "synthetic_content_generation":
        rep.warn(
            path,
            "Article 50(2) asks for a machine-readable mark on the output, not a label in the "
            "interface — confirm the mark travels with the artifact",
        )


def check_transparency(rep, path, item, vocab, system_keys):
    """Article 50. Applicable since 2 August 2026, and the gap a repository scan can actually see."""
    if not isinstance(item, dict):
        rep.err(path, "transparency finding must be a mapping")
        return
    check_unknown_keys(rep, path, item, TRANSPARENCY_KEYS)

    trigger = item.get("trigger")
    check_enum(rep, f"{path}.trigger", trigger, vocab["transparency_trigger"], "Article 50 trigger")
    check_article(rep, f"{path}.article", item.get("article"), "article")

    action = item.get("required_action")
    check_enum(rep, f"{path}.required_action", action, vocab["required_action"], "required action")
    expected = TRIGGER_REQUIRED_ACTION.get(trigger)
    if expected and action and action not in expected:
        rep.err(
            f"{path}.required_action",
            f"a '{trigger}' trigger requires {' or '.join(sorted(expected))}, not '{action}' — "
            "informing a person and marking an output are different duties under different "
            "paragraphs and one does not satisfy the other",
        )

    if item.get("applies_to_role") is not None:
        check_enum(
            rep, f"{path}.applies_to_role", item.get("applies_to_role"),
            vocab["eu_ai_act_role"], "role",
        )

    check_disclosure(rep, f"{path}.disclosure", item.get("disclosure"), vocab, trigger)
    check_finding_common(rep, path, item, vocab, system_keys)


def check_role_and_risk(rep, path, item, vocab, system_keys):
    """Role and tier. Real, and dated: enforceable_from keeps a future deadline from reading as now."""
    if not isinstance(item, dict):
        rep.err(path, "role-and-risk finding must be a mapping")
        return
    check_unknown_keys(rep, path, item, ROLE_RISK_KEYS)

    check_enum(rep, f"{path}.role", item.get("role"), vocab["eu_ai_act_role"], "role")
    check_article(rep, f"{path}.role_article", item.get("role_article"), "role_article")

    tier = item.get("tier")
    check_enum(rep, f"{path}.tier", tier, vocab["eu_ai_act_tier"], "risk tier")
    check_article(rep, f"{path}.tier_article", item.get("tier_article"), "tier_article")

    area = item.get("annex_iii_area")
    if area is not None:
        check_enum(rep, f"{path}.annex_iii_area", area, vocab["annex_iii_area"], "Annex III area")

    enforceable = item.get("enforceable_from")
    if enforceable is None:
        rep.err(
            f"{path}.enforceable_from",
            "missing required `enforceable_from` — say the date from which the obligations that "
            "follow from this tier apply, so that a finding serving a future deadline is not read "
            "as one that is due now",
        )
    elif not isinstance(enforceable, str) or not DATE_RE.match(enforceable):
        rep.err(f"{path}.enforceable_from", f"'{enforceable}' is not an ISO date (YYYY-MM-DD)")

    assessment = item.get("not_high_risk_assessment")
    in_annex_iii = area is not None and area != "none"
    if tier == "not_high_risk" and in_annex_iii and assessment is None:
        rep.err(
            f"{path}.not_high_risk_assessment",
            "missing required `not_high_risk_assessment` — concluding that a system in an Annex III "
            "area is not high-risk is the Article 6(3) derogation, and Article 6(4) expects the "
            "assessment behind it to be documented",
        )
    if assessment is not None:
        apath = f"{path}.not_high_risk_assessment"
        if not isinstance(assessment, dict):
            rep.err(apath, "must be a mapping")
        else:
            check_unknown_keys(rep, apath, assessment, ART_6_3_KEYS)
            check_enum(
                rep, f"{apath}.ground", assessment.get("ground"),
                vocab["art_6_3_ground"], "Article 6(3) ground",
            )
            check_article(rep, f"{apath}.article", assessment.get("article"), "article")
            profiling = assessment.get("profiling")
            if not isinstance(profiling, bool):
                rep.err(
                    f"{apath}.profiling",
                    "missing required boolean `profiling` — Article 6(3) turns on whether the "
                    "system profiles natural persons, so the answer cannot be left implicit",
                )
            elif profiling is True and tier == "not_high_risk":
                rep.err(
                    f"{apath}.profiling",
                    "is true while the tier is not_high_risk — Article 6(3) does not allow that "
                    "conclusion for a system that performs profiling of natural persons",
                )
            if isinstance(assessment.get("refs"), list):
                check_refs(rep, apath, assessment, required=False)

    check_finding_common(rep, path, item, vocab, system_keys)


def check_standards(rep, path, item, vocab, system_keys):
    """ISO/IEC 42001 references and NIST AI RMF function tags."""
    if not isinstance(item, dict):
        rep.err(path, "standards finding must be a mapping")
        return
    check_unknown_keys(rep, path, item, STANDARDS_KEYS)

    scheme = item.get("scheme")
    check_enum(rep, f"{path}.scheme", scheme, vocab["standards_scheme"], "scheme")

    value = item.get("value")
    if not value:
        rep.err(f"{path}.value", "missing required `value`")
    elif scheme == "nist_ai_rmf" and value not in vocab["nist_ai_rmf_function"]:
        rep.err(
            f"{path}.value",
            f"unknown NIST AI RMF function '{value}'" + suggest(value, vocab["nist_ai_rmf_function"]),
        )

    check_finding_common(rep, path, item, vocab, system_keys)


CATEGORY_CHECKS = {
    "prohibited_practices": check_prohibited_practice,
    "transparency_obligations": check_transparency,
    "role_and_risk": check_role_and_risk,
    "standards_alignment": check_standards,
}


def check_findings(rep, doc, vocab, system_keys, counts):
    findings = doc.get("findings")
    if findings is None:
        return
    if not isinstance(findings, dict):
        rep.err(
            "findings",
            "must be a mapping of finding categories, in order: "
            + ", ".join(vocab["finding_categories"]),
        )
        return

    order = vocab["finding_categories"]
    check_unknown_keys(rep, "findings", findings, set(order))

    # The order is the change, not a formatting preference. A manifest that opens with a risk tier
    # opens with the obligation that is furthest away, and readers act on what they read first.
    present = [key for key in findings if key in order]
    if present != [key for key in order if key in findings]:
        rep.err(
            "findings",
            f"categories are out of order ({', '.join(present)}). Write them as "
            f"{', '.join(order)}: what is enforceable today comes before what is enforceable later",
        )

    for category in order:
        items = rep.aslist(findings.get(category))
        counts[category] = len(items)
        check = CATEGORY_CHECKS[category]
        for i, item in enumerate(items):
            check(rep, f"findings.{category}[{i}]", item, vocab, system_keys)


def validate(doc, vocab):
    rep = Report()
    counts = {"ai_systems": 0, "providers": 0}
    for category in vocab["finding_categories"]:
        counts[category] = 0
    if not isinstance(doc, dict):
        rep.err("<root>", "manifest must be a mapping with `version`, `piece`, `source`, `ai_systems`")
        return rep, counts

    check_unknown_keys(rep, "<root>", doc, TOP_LEVEL_KEYS)

    version = doc.get("version")
    if not version or not isinstance(version, str) or not SEMVER_RE.match(version):
        rep.err("version", f"'{version}' is not a semantic version (for example 0.1.0)")
    if doc.get("piece") != PIECE:
        rep.err("piece", f"expected '{PIECE}', found '{doc.get('piece')}'")

    check_source(rep, doc)

    providers = rep.aslist(doc.get("providers"))
    provider_keys = []
    for i, provider in enumerate(providers):
        key = check_provider(rep, f"providers[{i}]", provider, vocab)
        if key:
            provider_keys.append(key)
    counts["providers"] = len(providers)

    systems = doc.get("ai_systems")
    if systems is None:
        rep.err("ai_systems", "missing required `ai_systems` (an empty list is a valid answer)")
        systems = []
    elif not isinstance(systems, list):
        rep.err("ai_systems", "must be a list")
        systems = []
    system_keys = []
    for i, sysobj in enumerate(systems):
        key = check_system(rep, f"ai_systems[{i}]", sysobj, vocab, provider_keys)
        if key:
            system_keys.append(key)
    counts["ai_systems"] = len(systems)

    for dupes, label in ((system_keys, "ai_systems"), (provider_keys, "providers")):
        seen = set()
        for key in dupes:
            if key in seen:
                rep.err(label, f"duplicate key '{key}' — keys must be unique and stable")
            seen.add(key)

    check_findings(rep, doc, vocab, system_keys, counts)

    return rep, counts


def collect_alerts(doc):
    """The two things a reader must not scroll past.

    An Article 5 hit and a missing Article 50 disclosure are both enforceable now, so neither is
    left as a row in a table: they are lifted out of the findings block and printed above
    everything else, and carried in --output=json so CI can fail on them.
    """
    alerts = []
    findings = doc.get("findings") if isinstance(doc, dict) else None
    if not isinstance(findings, dict):
        return alerts

    for item in findings.get("prohibited_practices") or []:
        if not isinstance(item, dict):
            continue
        determination = item.get("determination")
        if determination not in ("indicated", "needs_legal_review"):
            continue
        # An indicated practice and an unclear one are not the same claim, and the banner must not
        # flatten them into one. Article 5 is a prohibition either way, but only one of the two is
        # an assertion that the prohibition is engaged.
        tail = (
            "Article 5 is a prohibition: the answer is to stop the practice and take legal advice, "
            "not to document it."
            if determination == "indicated"
            else "A signal was found and the code does not settle it. Take legal advice before this "
            "runs again."
        )
        alerts.append(
            {
                "severity": "stop" if determination == "indicated" else "review",
                "category": "prohibited_practices",
                "system": item.get("system"),
                "message": (
                    f"{item.get('article')} {item.get('practice')} — {determination} on "
                    f"'{item.get('system')}'. {tail}"
                ),
            }
        )

    for item in findings.get("transparency_obligations") or []:
        if not isinstance(item, dict):
            continue
        disclosure = item.get("disclosure")
        state = disclosure.get("state") if isinstance(disclosure, dict) else None
        if state not in ("absent", "unclear"):
            continue
        alerts.append(
            {
                "severity": "gap" if state == "absent" else "unresolved",
                "category": "transparency_obligations",
                "system": item.get("system"),
                "message": (
                    f"{item.get('article')} {item.get('trigger')} on '{item.get('system')}': the "
                    f"required {item.get('required_action')} is {state}."
                ),
            }
        )
    return alerts


def render_alerts(alerts):
    if not alerts:
        return []
    width = max(len(a["message"]) for a in alerts)
    rule = "=" * min(max(width + 4, 40), 96)
    lines = [rule]
    for alert in alerts:
        lines.append(f"  {alert['severity'].upper()}: {alert['message']}")
    lines.append(rule)
    lines.append("")
    return lines


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
            # Writes the parsed document as JSON, but only when the manifest is valid. The diff
            # step consumes that file instead of carrying a second YAML parser, which also means
            # an invalid manifest can never reach :diff or :push.
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

    alerts = collect_alerts(doc)

    if output_json:
        sys.stdout.write(
            json.dumps(
                {
                    "piece": PIECE,
                    "manifest": str(path),
                    "ok": ok,
                    "loader": loader,
                    "counts": counts,
                    "alerts": alerts,
                    "errors": [{"path": p, "message": m} for p, m in rep.errors],
                    "warnings": [{"path": p, "message": m} for p, m in rep.warnings],
                },
                indent=None if quiet else 2,
                sort_keys=True,
            )
            + "\n"
        )
        return 0 if ok else 1

    for line in render_alerts(alerts):
        print(line)
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
            f"\nOK: {counts['ai_systems']} AI system(s), {counts['providers']} provider(s), "
            f"all keys valid ({len(rep.warnings)} warning(s))."
        )
        print(
            "     findings: "
            + ", ".join(
                f"{counts[c]} {c.replace('_', ' ')}" for c in vocab["finding_categories"]
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
