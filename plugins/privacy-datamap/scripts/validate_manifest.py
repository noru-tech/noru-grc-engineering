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

Usage:
    python3 validate_manifest.py <manifest.yml> [--as-of=YYYY-MM-DD] [--output=json] [--quiet]
        [--emit-parsed=<path>]
Exit codes: 0 = valid (warnings allowed), 1 = validation errors, 2 = usage / load error.
"""
import datetime
import hashlib
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
PIECE = "privacy-datamap"

REF_RE = re.compile(r"^[^:\s][^:]*:[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# Fideslang's own key grammar, from the upstream FidesKey type.
FIDES_KEY_RE = re.compile(r"^[A-Za-z0-9_.<>-]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

TOP_LEVEL_KEYS = {"version", "piece", "source", "dataset", "system"}
SOURCE_KEYS = {"slug", "commit_sha", "branch", "generated_by", "derived_digest"}
INTERPRETATION_KEYS = {"owner", "decided_at", "expires_at", "rationale", "refs"}
DATASET_KEYS = {"fides_key", "name", "description", "collections"}
COLLECTION_KEYS = {
    "name", "description", "refs", "interpretation", "needs_review", "fields",
    "structure_digest",
}
FIELD_KEYS = {"name", "description", "data_categories", "refs", "needs_review", "fields"}
SYSTEM_KEYS = {
    "fides_key", "name", "description", "system_type", "dataset_references",
    "privacy_declarations",
}
DECLARATION_KEYS = {
    "name", "data_use", "data_categories", "data_subjects", "refs", "interpretation",
    "needs_review",
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


def check_interpretation(rep, path, obj):
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



def structure_digest(fields):
    """Mirror of structureDigest() in collect.mjs. The two must agree.

    Kept deliberately trivial for exactly that reason: the sorted dotted field names, newline
    joined, sha256. Categories are not in it — resolving a classification must not invalidate a
    signature, but adding, removing or renaming a column must.
    """
    names = []

    def walk(items, prefix):
        for field in items or []:
            if not isinstance(field, dict):
                continue
            name = field.get("name")
            if not isinstance(name, str):
                continue
            names.append(prefix + name)
            if isinstance(field.get("fields"), list):
                walk(field["fields"], f"{prefix}{name}.")

    walk(fields, "")
    return hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()


def parse_date(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def check_horizon(rep, path, block, horizon_days, as_of):
    """expires_at is required, bounded, and — with --as-of — must not already have passed."""
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
    if days > horizon_days:
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
                "still true — the collector could not classify this field and nobody has. Give it "
                "a data category, or delete the field if it holds no personal data, then remove "
                "the flag",
            )
        elif not cats:
            # Not an error: plenty of columns are operational. But it is worth saying out loud,
            # because a data map that quietly omits a field looks identical to one that considered
            # it and found nothing.
            rep.warn(f"{fpath}.data_categories", "empty — recorded as holding no personal data")
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
            check_interpretation(rep, cpath, collection)

            # The structural anchor. A signature is given for a set of columns; if that set has
            # changed, the signature is not a statement about this table any more. Recomputed here
            # rather than trusted, so editing the fields without re-running :scan is caught in the
            # same breath as editing the digest by hand.
            stamped = collection.get("structure_digest")
            actual = structure_digest(collection.get("fields", []))
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
                cats = [
                    c
                    for field in collection.get("fields", []) or []
                    if isinstance(field, dict)
                    for c in (field.get("data_categories") or [])
                ]
                special = any(c in vocab["special_categories"] for c in cats)
                horizon = vocab["horizon_days"][
                    "special_category" if special else "standard"
                ]
                check_horizon(rep, cpath, block, horizon, as_of)

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
            check_refs(rep, dpath, decl)
            check_interpretation(rep, dpath, decl)
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


def validate(doc, vocab, as_of=None):
    rep = Report()
    counts = {"datasets": 0, "collections": 0, "fields": 0, "systems": 0, "needs_review": 0}
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
    dataset_keys = check_datasets(rep, doc, vocab, counts, as_of)
    check_systems(rep, doc, vocab, dataset_keys, counts, as_of)
    counts["datasets"] = len(doc.get("dataset") or [])
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
            f"\nOK: {counts['datasets']} dataset(s), {counts['collections']} collection(s), "
            f"{counts['fields']} field(s), {counts['systems']} system(s), all keys valid "
            f"({len(rep.warnings)} warning(s))."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
