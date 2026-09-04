#!/usr/bin/env python3
"""Offline whole-repository GRC enforcement and ratchet-baseline CLI.

The runner executes only entrypoints in the released registry. Repository files are inputs, never
commands. It removes credentials from every child environment and continues after a piece fails so
one broken check cannot hide another.
"""

import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
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


VERSION = 1
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = PLUGIN_ROOT.parent.parent / "actions" / "enforce" / "registry.json"
NEVER_BASELINE = {
    "credential_exposure",
    "expired_exception",
    "github_policy",
    "invalid",
    "invalid_baseline",
    "ruleset_drift",
    "stale_plan",
    "tooling",
    "workflow_drift",
}
REDACTIONS = (
    (re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1<redacted>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}"), "<redacted>"),
    (re.compile(r"\bnoru_[A-Za-z0-9]{6,}"), "<redacted>"),
    (
        re.compile(
            r"(\"?(?:api[_-]?key|authorization|token|secret|password)\"?\s*[:=]\s*\"?)([^\"\s,}]{6,})",
            re.I,
        ),
        r"\1<redacted>",
    ),
)
USAGE = """usage:
  enforce.py validate --repo=<path> --as-of=YYYY-MM-DD [--suite-root=<path>]
  enforce.py baseline propose --repo=<path> --as-of=YYYY-MM-DD [--suite-root=<path>]
  enforce.py baseline check --repo=<path> --as-of=YYYY-MM-DD [--suite-root=<path>]
  enforce.py baseline worklist --repo=<path> --as-of=YYYY-MM-DD [--suite-root=<path>]
  enforce.py baseline inspect --repo=<path> --as-of=YYYY-MM-DD --fingerprint=sha256:<hex>
  enforce.py policy --repo=<path>
Common: [--policy=<path>] [--registry=<path>] [--output=json|text] [--quiet]
"""


def redact(value):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def parse_date(value, label):
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD") from exc


def load_json(path, label):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"missing {label} at {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} at {path} must be a JSON object")
    return value


def load_policy(path):
    try:
        document, _loader = load_yaml(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"missing enforcement policy at {path}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not parse enforcement policy at {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("enforcement policy must be a YAML mapping")
    errors = validate_policy(document)
    if errors:
        raise ValueError("invalid enforcement policy: " + "; ".join(errors))
    return document


def validate_policy(policy):
    errors = []
    if policy.get("version") != 1:
        errors.append("version must be 1")
    adoption = policy.get("adoption")
    if not isinstance(adoption, dict) or adoption.get("mode") not in {"strict", "ratchet"}:
        errors.append("adoption.mode must be strict or ratchet")
    elif adoption["mode"] == "ratchet" and not isinstance(adoption.get("baseline"), str):
        errors.append("ratchet mode requires adoption.baseline")
    pieces = policy.get("pieces")
    if not isinstance(pieces, dict) or not pieces:
        errors.append("pieces must be a non-empty mapping")
    else:
        for name, value in pieces.items():
            if not isinstance(value, dict) or not isinstance(value.get("required"), bool):
                errors.append(f"pieces.{name}.required must be true or false")
            if isinstance(value, dict) and "fail_on" in value and not (
                isinstance(value["fail_on"], list)
                and all(isinstance(item, str) and item for item in value["fail_on"])
            ):
                errors.append(f"pieces.{name}.fail_on must be a string list")
    reviews = policy.get("reviews")
    if not isinstance(reviews, dict) or not isinstance(reviews.get("minimum_approvals"), int):
        errors.append("reviews.minimum_approvals must be an integer")
    else:
        if reviews["minimum_approvals"] < 1:
            errors.append("reviews.minimum_approvals must be at least 1")
        for key in (
            "dismiss_stale_approvals",
            "require_last_push_approval",
            "require_code_owner_review",
            "require_thread_resolution",
        ):
            if reviews.get(key) is not True:
                errors.append(f"reviews.{key} must be true")
    ownership = policy.get("ownership")
    if not isinstance(ownership, dict):
        errors.append("ownership must be a mapping")
    else:
        for key in ("grc_reviewers", "privacy_reviewers", "security_reviewers", "break_glass"):
            value = ownership.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"@[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
                errors.append(f"ownership.{key} must name a GitHub team as @org/team")
    exceptions = policy.get("exceptions")
    if not isinstance(exceptions, dict):
        errors.append("exceptions must be a mapping")
    else:
        days = exceptions.get("maximum_days")
        if not isinstance(days, int) or not 1 <= days <= 365:
            errors.append("exceptions.maximum_days must be between 1 and 365")
        for key in ("require_named_owner", "require_rationale"):
            if exceptions.get(key) is not True:
                errors.append(f"exceptions.{key} must be true")
    github = policy.get("github")
    if not isinstance(github, dict):
        errors.append("github must be a mapping")
    else:
        if github.get("scope") not in {"repository", "organization"}:
            errors.append("github.scope must be repository or organization")
        for key in ("target", "ruleset_name", "required_check"):
            if not isinstance(github.get(key), str) or not github[key].strip():
                errors.append(f"github.{key} is required")
        if not isinstance(github.get("action_sha"), str) or not re.fullmatch(
            r"[0-9a-f]{40}", github["action_sha"]
        ):
            errors.append("github.action_sha must be a full 40-character commit SHA")
    return errors


def parse_args(argv):
    if not argv:
        raise ValueError("missing command")
    command = argv[0]
    index = 1
    subcommand = None
    if command == "baseline":
        if len(argv) < 2 or argv[1] not in {"propose", "check", "worklist", "inspect"}:
            raise ValueError("baseline requires propose, check, worklist, or inspect")
        subcommand = argv[1]
        index = 2
    elif command not in {"validate", "policy"}:
        raise ValueError(f"unknown command '{command}'")
    opts = {
        "command": command,
        "subcommand": subcommand,
        "repo": pathlib.Path.cwd(),
        "suite_root": PLUGIN_ROOT.parent.parent,
        "policy": None,
        "registry": DEFAULT_REGISTRY,
        "as_of": None,
        "fingerprint": None,
        "json": False,
        "quiet": False,
    }
    for arg in argv[index:]:
        if arg.startswith("--repo="):
            opts["repo"] = pathlib.Path(arg.split("=", 1)[1]).resolve()
        elif arg.startswith("--suite-root="):
            opts["suite_root"] = pathlib.Path(arg.split("=", 1)[1]).resolve()
        elif arg.startswith("--policy="):
            opts["policy"] = pathlib.Path(arg.split("=", 1)[1]).resolve()
        elif arg.startswith("--registry="):
            opts["registry"] = pathlib.Path(arg.split("=", 1)[1]).resolve()
        elif arg.startswith("--as-of="):
            opts["as_of"] = parse_date(arg.split("=", 1)[1], "--as-of")
        elif arg.startswith("--fingerprint="):
            opts["fingerprint"] = arg.split("=", 1)[1]
        elif arg == "--output=json":
            opts["json"] = True
        elif arg == "--output=text":
            opts["json"] = False
        elif arg == "--quiet":
            opts["quiet"] = True
        elif arg in {"-h", "--help"}:
            opts["help"] = True
        else:
            raise ValueError(f"unknown option '{arg}'")
    opts["policy"] = opts["policy"] or opts["repo"] / ".noru" / "enforcement.yml"
    if command != "policy" and opts["as_of"] is None:
        raise ValueError("--as-of=YYYY-MM-DD is required for deterministic enforcement")
    if subcommand == "inspect" and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", opts["fingerprint"] or ""
    ):
        raise ValueError("baseline inspect requires --fingerprint=sha256:<64 lowercase hex>")
    return opts


def repository_info(repo):
    def git(*args):
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"

    return {
        "root": str(repo),
        "commit_sha": git("rev-parse", "HEAD"),
        "branch": git("branch", "--show-current"),
    }


def normalized_rule(finding):
    kind = str(finding.get("kind") or "unknown")
    path = str(finding.get("path") or "")
    message = str(finding.get("message") or "")
    if kind == "invalid" and path.endswith("needs_review"):
        return "needs_review"
    if kind == "invalid" and "interpretation" in message and "missing" in message:
        return "missing_interpretation"
    return kind


def normalize_value(value):
    if isinstance(value, dict):
        return {
            key: normalize_value(child)
            for key, child in sorted(value.items())
            if key not in {"message", "ref", "refs", "first_seen"}
        }
    if isinstance(value, list):
        return [normalize_value(child) for child in value]
    if isinstance(value, str):
        return re.sub(r":\d+(?=$|\b)", ":<line>", value)
    return value


def violation_from_finding(piece, finding):
    rule = normalized_rule(finding)
    subject = str(
        finding.get("subject")
        or finding.get("path")
        or finding.get("manifest")
        or "repository"
    )
    normalized = normalize_value({**finding, "kind": rule})
    evidence_refs = finding.get("refs") or ([finding["ref"]] if finding.get("ref") else [])
    identity = {"piece": piece, "rule": rule, "subject": subject, "violation": normalized}
    return {
        "piece": piece,
        "rule": rule,
        "subject": subject,
        "fingerprint": "sha256:" + digest(identity),
        "message": redact(finding.get("message", "")),
        "evidence_refs": [redact(value) for value in evidence_refs],
        "details": normalized,
        "baselineable": rule not in NEVER_BASELINE,
    }


def run_piece(repo, suite_root, registry_row, as_of):
    cache = repo / ".noru" / ".cache" / "enforcement"
    cache.mkdir(parents=True, exist_ok=True)
    report_path = cache / f"{registry_row['name']}.json"
    command = [
        sys.executable or "python3",
        str(suite_root / "scripts" / "ci_check.py"),
        f"--piece={registry_row['name']}",
        f"--repo={repo}",
        f"--plugins={suite_root / 'plugins'}",
        "--mode=warn",
        "--steps=scan,validate,expiry",
        f"--as-of={as_of.isoformat()}",
        f"--report={report_path}",
        "--output=json",
        "--quiet",
        "--on-missing-prerequisite=fail",
    ]
    env = dict(os.environ)
    for key in list(env):
        if re.search(r"(?:TOKEN|SECRET|PASSWORD|API_KEY|AUTHORIZATION)", key, re.I):
            env.pop(key, None)
    try:
        completed = subprocess.run(
            command,
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "piece": registry_row["name"],
            "status": "error",
            "findings": [
                {
                    "kind": "tooling",
                    "message": redact(str(exc)),
                    "path": registry_row["artifact"],
                }
            ],
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {
            "status": "error",
            "findings": [
                {
                    "kind": "tooling",
                    "message": redact(completed.stderr or completed.stdout or "no JSON report"),
                    "path": registry_row["artifact"],
                }
            ],
        }
    return {
        "piece": registry_row["name"],
        "status": payload.get("status", "error"),
        "steps": payload.get("steps", []),
        "findings": payload.get("findings", []),
    }


def load_baseline(path):
    if not path.is_file():
        return {"version": 1, "violations": []}, [f"baseline file is missing at {path}"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"version": 1, "violations": []}, [f"baseline is unreadable: {exc}"]
    errors = []
    if not isinstance(value, dict) or value.get("version") != 1:
        return {"version": 1, "violations": []}, ["baseline.version must be 1"]
    entries = value.get("violations")
    if not isinstance(entries, list):
        return {"version": 1, "violations": []}, ["baseline.violations must be a list"]
    seen = set()
    for index, entry in enumerate(entries):
        label = f"baseline.violations[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be an object")
            continue
        fingerprint = entry.get("fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint):
            errors.append(f"{label}.fingerprint is invalid")
        elif fingerprint in seen:
            errors.append(f"{label}.fingerprint is duplicated")
        seen.add(fingerprint)
        owner = entry.get("owner")
        if not isinstance(owner, str) or len(owner.split()) < 2 or owner.startswith("@"):
            errors.append(f"{label}.owner must name a person")
        if not isinstance(entry.get("rationale"), str) or len(entry["rationale"].strip()) < 10:
            errors.append(f"{label}.rationale must explain the temporary acceptance")
        try:
            decided = parse_date(entry.get("decided_at"), f"{label}.decided_at")
            expires = parse_date(entry.get("expires_at"), f"{label}.expires_at")
            if expires <= decided:
                errors.append(f"{label}.expires_at must be after decided_at")
        except ValueError as exc:
            errors.append(str(exc))
    return value, errors


def evaluate(opts):
    repo = opts["repo"]
    policy = load_policy(opts["policy"])
    registry = load_json(opts["registry"], "enforcement registry")
    rows = {row["name"]: row for row in registry.get("pieces", [])}
    piece_results = []
    violations = []
    coverage = []
    for name, config in policy["pieces"].items():
        if not config.get("required"):
            continue
        row = rows.get(name)
        if row is None:
            piece_result = {
                "piece": name,
                "status": "error",
                "findings": [
                    {
                        "kind": "tooling",
                        "message": "required piece is absent from the released registry",
                        "path": f"pieces.{name}",
                    }
                ],
            }
        else:
            piece_result = run_piece(repo, opts["suite_root"], row, opts["as_of"])
        piece_results.append(piece_result)
        allowed = set(config.get("fail_on") or [
            "drift", "invalid", "needs_review", "missing_interpretation", "expired",
            "cadence", "coverage", "tooling", "unparsable",
        ])
        # A per-piece fail_on list may make review policy stricter, but cannot turn malformed
        # records or a broken validator into a passing check.
        allowed.update({"invalid", "needs_review", "missing_interpretation", "tooling", "unparsable"})
        for finding in piece_result.get("findings", []):
            violation = violation_from_finding(name, finding)
            if violation["rule"] == "coverage":
                coverage.append(violation)
            if violation["rule"] in allowed:
                violations.append(violation)

    adoption = policy["adoption"]
    baseline_entries = []
    baseline_errors = []
    baseline_path = None
    if adoption["mode"] == "ratchet":
        baseline_path = repo / adoption["baseline"]
        baseline, baseline_errors = load_baseline(baseline_path)
        baseline_entries = baseline.get("violations", [])
        current_policy_digest = digest(policy)
        if baseline.get("policy_digest") != current_policy_digest:
            baseline_errors.append(
                "baseline.policy_digest does not match the current enforcement policy"
            )
    entry_by_fingerprint = {entry.get("fingerprint"): entry for entry in baseline_entries}
    current = {row["fingerprint"]: row for row in violations}
    baselined = []
    new = []
    expired = []
    for violation in violations:
        entry = entry_by_fingerprint.get(violation["fingerprint"])
        if entry and violation["baselineable"]:
            expiry = parse_date(entry["expires_at"], "baseline expires_at")
            maximum = policy["exceptions"]["maximum_days"]
            decided = parse_date(entry["decided_at"], "baseline decided_at")
            if expiry < opts["as_of"] or (expiry - decided).days > maximum:
                expired.append({**entry, "current_violation": violation})
            else:
                baselined.append({**violation, "acceptance": entry})
        else:
            new.append(violation)
    stale = [entry for entry in baseline_entries if entry.get("fingerprint") not in current]
    for message in baseline_errors:
        new.append(
            violation_from_finding(
                "repo-enforcement",
                {"kind": "invalid_baseline", "path": str(baseline_path), "message": message},
            )
        )
    report = {
        "version": VERSION,
        "ok": not new and not expired and not stale,
        "repository": repository_info(repo),
        "policy_digest": digest(policy),
        "pieces": piece_results,
        "new_violations": new,
        "baselined_violations": baselined,
        "expired_exceptions": expired,
        "stale_baseline_entries": stale,
        "coverage_failures": coverage,
        "github_policy_findings": [],
    }
    return policy, report


def proposal(opts, policy, report):
    maximum = policy["exceptions"]["maximum_days"]
    expires = opts["as_of"] + datetime.timedelta(days=maximum)
    candidate = {
        "version": 1,
        "status": "proposal_only",
        "policy_digest": report["policy_digest"],
        "violations": [
            {
                "piece": row["piece"],
                "rule": row["rule"],
                "subject": row["subject"],
                "fingerprint": row["fingerprint"],
                "owner": "",
                "decided_at": opts["as_of"].isoformat(),
                "expires_at": expires.isoformat(),
                "rationale": "",
            }
            for row in report["new_violations"]
            if row["baselineable"]
        ],
    }
    path = opts["repo"] / ".noru" / ".cache" / "enforcement-baseline.candidate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "candidate": str(path.relative_to(opts["repo"])), **candidate}


def review_command(piece):
    return f"/{piece}:scan" if piece != "repo-enforcement" else "/repo-enforcement:verify"


def work_item(status, as_of, violation=None, acceptance=None):
    violation = violation or {}
    acceptance = acceptance or {}
    piece = violation.get("piece") or acceptance.get("piece") or "repo-enforcement"
    expires_at = acceptance.get("expires_at")
    days_remaining = None
    if expires_at:
        try:
            days_remaining = (parse_date(expires_at, "expires_at") - as_of).days
        except ValueError:
            pass
    urgency = "blocking" if status in {"expired", "unbaselined"} else (
        "cleanup" if status == "stale" else
        "due_soon" if days_remaining is not None and days_remaining <= 7 else
        "scheduled"
    )
    next_actions = {
        "accepted": "Resolve the underlying review, then remove the stale baseline entry in the same reviewed PR.",
        "expired": "Resolve now; the temporary acceptance has expired and cannot permit merge.",
        "stale": "Verify why the violation disappeared, then remove this unused baseline entry in the same reviewed PR.",
        "unbaselined": "Resolve before merge; this new or mutated violation is not accepted by the baseline.",
    }
    return {
        "status": status,
        "urgency": urgency,
        "piece": piece,
        "rule": violation.get("rule") or acceptance.get("rule") or "unknown",
        "subject": violation.get("subject") or acceptance.get("subject") or "repository",
        "fingerprint": violation.get("fingerprint") or acceptance.get("fingerprint"),
        "owner": acceptance.get("owner"),
        "decided_at": acceptance.get("decided_at"),
        "expires_at": expires_at,
        "days_remaining": days_remaining,
        "message": violation.get("message", ""),
        "evidence_refs": violation.get("evidence_refs", []),
        "details": violation.get("details", {}),
        "review_command": review_command(piece),
        "inspect_command": (
            "/repo-enforcement:work "
            + (violation.get("fingerprint") or acceptance.get("fingerprint") or "")
        ),
        "next_action": next_actions[status],
    }


def make_worklist(opts, report):
    items = []
    for row in report["baselined_violations"]:
        items.append(work_item("accepted", opts["as_of"], row, row.get("acceptance")))
    for row in report["expired_exceptions"]:
        items.append(work_item("expired", opts["as_of"], row.get("current_violation"), row))
    for row in report["stale_baseline_entries"]:
        items.append(work_item("stale", opts["as_of"], acceptance=row))
    for row in report["new_violations"]:
        items.append(work_item("unbaselined", opts["as_of"], violation=row))
    priority = {"blocking": 0, "cleanup": 1, "due_soon": 2, "scheduled": 3}
    items.sort(
        key=lambda row: (
            priority[row["urgency"]],
            row["expires_at"] or "9999-12-31",
            row["piece"],
            row["rule"],
            row["subject"],
        )
    )
    by_piece = {}
    by_owner = {}
    for row in items:
        by_piece[row["piece"]] = by_piece.get(row["piece"], 0) + 1
        if row["owner"]:
            by_owner[row["owner"]] = by_owner.get(row["owner"], 0) + 1
    return {
        "ok": report["ok"],
        "repository": report["repository"],
        "policy_digest": report["policy_digest"],
        "as_of": opts["as_of"].isoformat(),
        "summary": {
            "baseline_debt": sum(row["status"] in {"accepted", "expired"} for row in items),
            "active": sum(row["status"] == "accepted" for row in items),
            "expired": sum(row["status"] == "expired" for row in items),
            "stale_cleanup": sum(row["status"] == "stale" for row in items),
            "unbaselined_blockers": sum(row["status"] == "unbaselined" for row in items),
            "due_within_7_days": sum(row["urgency"] == "due_soon" for row in items),
            "by_piece": dict(sorted(by_piece.items())),
            "by_owner": dict(sorted(by_owner.items())),
        },
        "items": items,
    }


def inspect_work_item(opts, report):
    worklist = make_worklist(opts, report)
    matches = [
        row for row in worklist["items"] if row["fingerprint"] == opts["fingerprint"]
    ]
    if not matches:
        return {
            "ok": False,
            "found": False,
            "fingerprint": opts["fingerprint"],
            "message": "No current violation or baseline entry has this fingerprint.",
        }
    return {
        "ok": True,
        "found": True,
        "item": matches[0],
        "workflow": [
            "Run the owning piece review command and resolve the underlying judgement.",
            "Re-run baseline check and confirm the old violation has disappeared.",
            "Review why the baseline entry became stale before removing it in the same PR.",
            "After merge, run the owning piece diff and explicitly confirm any Noru push.",
        ],
    }


def render_text(payload):
    if "summary" in payload and "items" in payload:
        summary = payload["summary"]
        lines = [
            f"Baseline debt: {summary['baseline_debt']}",
            f"Expired: {summary['expired']} | due within 7 days: {summary['due_within_7_days']} | "
            f"stale cleanup: {summary['stale_cleanup']} | new blockers: {summary['unbaselined_blockers']}",
        ]
        for index, row in enumerate(payload["items"], start=1):
            owner = f" | owner: {row['owner']}" if row["owner"] else ""
            expiry = f" | expires: {row['expires_at']}" if row["expires_at"] else ""
            lines.extend(
                [
                    f"{index}. [{row['urgency']}] {row['piece']} / {row['rule']}",
                    f"   {row['subject']}{owner}{expiry}",
                    f"   Next: {row['review_command']} then {row['inspect_command']}",
                ]
            )
        return "\n".join(lines)
    if "new_violations" not in payload:
        return json.dumps(payload, indent=2, sort_keys=True)
    lines = [
        "Repository enforcement: " + ("PASS" if payload["ok"] else "FAIL"),
        f"new violations: {len(payload['new_violations'])}",
        f"baselined violations: {len(payload['baselined_violations'])}",
        f"expired exceptions: {len(payload['expired_exceptions'])}",
        f"stale baseline entries: {len(payload['stale_baseline_entries'])}",
    ]
    for row in payload["new_violations"]:
        lines.append(f"  FAIL [{row['piece']}/{row['rule']}] {row['subject']}: {row['message']}")
    return "\n".join(lines)


def main(argv):
    try:
        opts = parse_args(argv)
    except ValueError as exc:
        print(f"error: {exc}\n{USAGE}", file=sys.stderr)
        return 2
    if opts.get("help"):
        print(USAGE, end="")
        return 0
    try:
        if opts["command"] == "policy":
            policy = load_policy(opts["policy"])
            payload = {"ok": True, "policy": policy, "policy_digest": digest(policy)}
            exit_code = 0
        else:
            policy, report = evaluate(opts)
            if opts["command"] == "baseline" and opts["subcommand"] == "propose":
                payload = proposal(opts, policy, report)
                exit_code = 0
            elif opts["command"] == "baseline" and opts["subcommand"] == "worklist":
                payload = make_worklist(opts, report)
                exit_code = 0 if payload["ok"] else 1
            elif opts["command"] == "baseline" and opts["subcommand"] == "inspect":
                payload = inspect_work_item(opts, report)
                exit_code = 0 if payload["ok"] else 1
            else:
                payload = report
                exit_code = 0 if report["ok"] else 1
    except ValueError as exc:
        print(f"error: {redact(str(exc))}", file=sys.stderr)
        return 2
    if opts["json"]:
        print(json.dumps(payload, sort_keys=opts["quiet"]))
    elif not opts["quiet"]:
        print(render_text(payload))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
