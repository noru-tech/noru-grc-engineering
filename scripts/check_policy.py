#!/usr/bin/env python3
"""Find personal data the organization never agreed to process.

Standard library only. No network, no installs, no credential — the same constraint
`scripts/check_expiry.py` works under, and for the same reason: a check that only runs for people
with write access is not a check, it is a report someone remembers to run.

The drift gate in `scripts/ci_check.py` asks *"has the schema changed since somebody signed?"* It is
a good question and it is not this one. Add a passport number column and drift fires; re-scan,
re-sign, thirty seconds, green. Nothing in the repository ever says you were not permitted to collect
it. This file is the other question:

    was this ever allowed?

It answers it against `.noru/privacy-baseline.yml` — the agreed taxonomy, pinned from Noru so the
check can run offline on a fork pull request. Noru is the truth; the file is the floor. The
reconciliation between them belongs in a credentialed job and reports a difference rather than
silently preferring what is on disk (contract/README.md, "A vocabulary is not a catalogue").

Seven things it reports:

  unpermitted_category  a data category in the map that the baseline does not allow, or denies
  unpermitted_use       a data_use on a privacy declaration that the baseline does not allow
  unpermitted_subject   a data_subject that the baseline does not allow
  unpermitted_pair      a category and a use that are each permitted apart and forbidden together.
                        Health data used for advertising is the canonical case and no single-axis
                        list can express it
  confined_category     a category the baseline confined to one dataset or system, found somewhere
                        else. "Card numbers live in payments and nowhere else" is this rule
  undeclared_system     a system processing personal data that the baseline does not name, when the
                        baseline declares its system list closed
  special_category      GDPR Article 9 or Article 10 data in the map. Reported, not failed, by
                        default: whether it is *permitted* is what the category rules answer. This
                        finding exists so a reviewer never has to go looking for the highest-risk
                        thing in the map

Matching on every axis is by **dotted prefix**, because Fideslang is a tree: allowing `user.contact`
allows `user.contact.email`, and denying `user.biometric` denies `user.biometric.fingerprint`. Where
an allow and a deny both match, the **more specific one wins** — that is what makes "no financial
data, except the card number in payments" expressible in two lines instead of an enumeration.

Usage:
    python3 scripts/check_policy.py <manifest.yml|parsed.json> --baseline=<baseline.yml>
        [--fail-on=<kinds>|none] [--output=json|text] [--quiet]
Exit codes: 0 = nothing that --fail-on covers, 1 = at least one such finding, 2 = usage / load error.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

KINDS = (
    "unpermitted_category",
    "unpermitted_use",
    "unpermitted_subject",
    "unpermitted_pair",
    "confined_category",
    "undeclared_system",
    "special_category",
)
# `special_category` is deliberately advisory. It says the map contains Article 9 data, which is a
# fact a reviewer must see; whether that data is *permitted* is answered by the category rules
# above it, and failing twice on one condition is how a gate gets reverted.
DEFAULT_FAIL_ON = (
    "unpermitted_category",
    "unpermitted_use",
    "unpermitted_subject",
    "unpermitted_pair",
    "confined_category",
    "undeclared_system",
)

SPECIAL_CATEGORIES_PATH = ROOT / "contract" / "lib" / "taxonomy" / "special_categories.json"

USAGE = (
    "usage: check_policy.py <manifest.yml|parsed.json> --baseline=<baseline.yml> "
    "[--fail-on=<kinds>|none] [--output=json|text] [--quiet]\n"
)


def load_document(path):
    """Return a manifest or baseline as a Python object, or raise ValueError with something readable.

    Identical to check_expiry.py's loader on purpose: the two checks must never disagree about what
    a file says because they parsed it differently.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    sys.path.insert(0, str(ROOT / "contract" / "lib"))
    try:
        from yaml_mini import load_yaml  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - only if the repo is half-checked-out
        raise ValueError(f"cannot load contract/lib/yaml_mini.py ({exc})") from exc
    document, _loader = load_yaml(text)
    return document


def load_special_categories():
    """The Article 9 / Article 10 roots. Empty if the snapshot is missing, which is reported."""
    if not SPECIAL_CATEGORIES_PATH.is_file():
        return None
    data = json.loads(SPECIAL_CATEGORIES_PATH.read_text(encoding="utf-8"))
    keys = data.get("fides_keys")
    return list(keys) if isinstance(keys, list) else None


# --- prefix matching ------------------------------------------------------------------------------
def match_length(key, patterns):
    """Length of the most specific pattern covering `key`, or -1 if none does.

    Fideslang is a tree and the baseline has to be written in terms of subtrees, or it becomes an
    enumeration nobody maintains. `user.contact` covers `user.contact.email`; it does not cover
    `user.contacts_import`, which is why the boundary is a dot and not a bare prefix.
    """
    best = -1
    if not isinstance(key, str):
        return best
    for pattern in patterns or ():
        if not isinstance(pattern, str):
            continue
        if key == pattern or key.startswith(pattern + "."):
            if len(pattern) > best:
                best = len(pattern)
    return best


class Axis:
    """One axis of the baseline — categories, uses or subjects — as a decision procedure."""

    def __init__(self, rule, name):
        rule = rule if isinstance(rule, dict) else {}
        self.name = name
        self.allow = [p for p in (rule.get("allow") or []) if isinstance(p, str)]
        self.deny = [p for p in (rule.get("deny") or []) if isinstance(p, str)]
        self.allow_unlisted = bool(rule.get("allow_unlisted"))
        # Naming an allow list is what closes the world. A baseline with only a deny list is a
        # blocklist, which is a legitimate thing to want and a weaker thing to have.
        self.closed = bool(self.allow) and not self.allow_unlisted

    def verdict(self, key):
        """(permitted, reason). `reason` is None when permitted."""
        allowed = match_length(key, self.allow)
        denied = match_length(key, self.deny)
        if denied >= 0 and denied >= allowed:
            pattern = self._most_specific(key, self.deny)
            return False, (
                f"the baseline denies '{pattern}'"
                + ("" if pattern == key else f", which covers '{key}'")
            )
        if allowed >= 0:
            return True, None
        if self.closed:
            return False, (
                f"'{key}' is not in the baseline's {self.name} allow list, and that list is closed"
            )
        return True, None

    @staticmethod
    def _most_specific(key, patterns):
        best, best_len = None, -1
        for pattern in patterns:
            if isinstance(pattern, str) and (key == pattern or key.startswith(pattern + ".")):
                if len(pattern) > best_len:
                    best, best_len = pattern, len(pattern)
        return best


# --- walking the map ------------------------------------------------------------------------------
def _refs(node):
    refs = node.get("refs")
    return [r for r in refs if isinstance(r, str)] if isinstance(refs, list) else []


def _first_ref(node):
    refs = _refs(node)
    return refs[0] if refs else None


def walk_carriers(document):
    """Yield (path, node, context) for every mapping that carries a category, use or subject.

    Piece-agnostic by shape rather than by name: a mapping carrying `data_categories`, `data_use` or
    `data_subjects` is a thing the baseline has an opinion about, wherever the piece put it. That
    covers privacy-datamap's fields and privacy declarations, and ai-inventory's data categories,
    without this file being taught about either.

    `context` carries the enclosing dataset and system keys, because the baseline's scopes are
    written in terms of them.
    """

    def walk(node, path, context):
        if isinstance(node, dict):
            local = dict(context)
            key = node.get("fides_key")
            if isinstance(key, str):
                if path.startswith("dataset["):
                    local["dataset"] = key
                elif path.startswith("system["):
                    local["system"] = key
            if any(f in node for f in ("data_categories", "data_use", "data_subjects")):
                yield path or "<root>", node, local
            for name, child in node.items():
                if name in ("interpretation", "refs"):
                    continue
                yield from walk(child, f"{path}.{name}" if path else name, local)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                yield from walk(child, f"{path}[{index}]", context)

    yield from walk(document, "", {})


def _subject_of(node, context):
    for field in ("name", "key", "title"):
        value = node.get(field)
        if isinstance(value, str) and value.strip():
            label = value.strip()[:80]
            break
    else:
        label = None
    where = context.get("dataset") or context.get("system")
    if label and where:
        return f"{where}.{label}"
    return label or where


def _categories(node):
    raw = node.get("data_categories")
    if isinstance(raw, str):
        return [raw]
    return [c for c in raw if isinstance(c, str)] if isinstance(raw, list) else []


def _subjects(node):
    raw = node.get("data_subjects")
    if isinstance(raw, str):
        return [raw]
    return [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []


# --- the checks -----------------------------------------------------------------------------------
def evaluate(document, baseline, special_roots):
    findings = []
    counts = {kind: 0 for kind in KINDS}
    counts["carriers"] = 0

    categories = Axis(baseline.get("data_categories"), "data category")
    uses = Axis(baseline.get("data_uses"), "data use")
    subjects = Axis(baseline.get("data_subjects"), "data subject")
    pairs = [p for p in (baseline.get("forbidden_pairs") or []) if isinstance(p, dict)]
    scopes = [s for s in (baseline.get("scopes") or []) if isinstance(s, dict)]
    systems_rule = baseline.get("systems") if isinstance(baseline.get("systems"), dict) else {}

    def add(kind, path, node, context, value, message, **extra):
        counts[kind] += 1
        findings.append(
            {
                "kind": kind,
                "path": path,
                "subject": _subject_of(node, context),
                "value": value,
                "dataset": context.get("dataset"),
                "system": context.get("system"),
                "ref": _first_ref(node),
                "message": message,
                **extra,
            }
        )

    # Scope narrowings that apply at this location, most specific last so they override.
    def scoped_axis(context):
        allow = list(categories.allow)
        deny = list(categories.deny)
        for scope in scopes:
            hits_dataset = scope.get("dataset") and scope["dataset"] == context.get("dataset")
            hits_system = scope.get("system") and scope["system"] == context.get("system")
            if hits_dataset or hits_system:
                allow.extend(p for p in (scope.get("allow") or []) if isinstance(p, str))
                deny.extend(p for p in (scope.get("deny") or []) if isinstance(p, str))
        axis = Axis({"allow": allow, "deny": deny}, "data category")
        axis.closed = categories.closed
        return axis

    # `confine` is the direction teams actually ask for: not "what may payments hold" but "where is
    # the only place a card number is allowed to be". Built once, applied everywhere.
    confined = []
    for scope in scopes:
        for pattern in scope.get("confine") or []:
            if isinstance(pattern, str):
                confined.append((pattern, scope))

    carriers = list(walk_carriers(document))
    counts["carriers"] = len(carriers)

    for path, node, context in carriers:
        node_categories = _categories(node)
        axis = scoped_axis(context)

        for category in node_categories:
            permitted, reason = axis.verdict(category)
            if not permitted:
                add(
                    "unpermitted_category",
                    path,
                    node,
                    context,
                    category,
                    f"{reason} — this is personal data the organization has not agreed to process",
                )

            for pattern, scope in confined:
                if match_length(category, [pattern]) < 0:
                    continue
                here = scope.get("dataset") or scope.get("system")
                if scope.get("dataset") and context.get("dataset") == scope["dataset"]:
                    continue
                if scope.get("system") and context.get("system") == scope["system"]:
                    continue
                add(
                    "confined_category",
                    path,
                    node,
                    context,
                    category,
                    f"'{category}' is confined to '{here}' by the baseline, and this is not it"
                    + (f" — {scope['reason']}" if isinstance(scope.get("reason"), str) else ""),
                    confined_to=here,
                )

            if special_roots is not None and match_length(category, special_roots) >= 0:
                add(
                    "special_category",
                    path,
                    node,
                    context,
                    category,
                    f"'{category}' is GDPR Article 9 or Article 10 data — the highest-risk thing in "
                    "this map, surfaced so nobody has to go looking for it",
                )

        use = node.get("data_use")
        if isinstance(use, str) and use:
            permitted, reason = uses.verdict(use)
            if not permitted:
                add(
                    "unpermitted_use",
                    path,
                    node,
                    context,
                    use,
                    f"{reason} — this is a purpose nobody agreed to",
                )
            for pair in pairs:
                if match_length(use, pair.get("uses") or []) < 0:
                    continue
                for category in node_categories:
                    if match_length(category, pair.get("categories") or []) < 0:
                        continue
                    add(
                        "unpermitted_pair",
                        path,
                        node,
                        context,
                        f"{category} + {use}",
                        f"'{category}' may not be processed for '{use}': {pair.get('reason')}",
                        category=category,
                        use=use,
                    )

        for subject in _subjects(node):
            permitted, reason = subjects.verdict(subject)
            if not permitted:
                add(
                    "unpermitted_subject",
                    path,
                    node,
                    context,
                    subject,
                    f"{reason} — this names people the baseline does not cover",
                )

    if systems_rule.get("closed"):
        allowed = [s for s in (systems_rule.get("allow") or []) if isinstance(s, str)]
        for index, system in enumerate(document.get("system") or []):
            if not isinstance(system, dict):
                continue
            key = system.get("fides_key")
            if isinstance(key, str) and key not in allowed:
                add(
                    "undeclared_system",
                    f"system[{index}]",
                    system,
                    {"system": key},
                    key,
                    f"'{key}' processes personal data and the baseline's system list is closed — a "
                    "service that started processing without anyone agreeing to it is exactly the "
                    "drift this gate exists to catch",
                )

    return findings, counts


def parse_fail_on(value):
    if value.strip().lower() in ("none", ""):
        return set()
    kinds = {part.strip() for part in value.split(",") if part.strip()}
    unknown = kinds - set(KINDS)
    if unknown:
        raise ValueError(
            f"unknown --fail-on kind(s) {sorted(unknown)}; known kinds are {list(KINDS)} or 'none'"
        )
    return kinds


def render_text(payload, quiet):
    lines = []
    if not quiet:
        lines.append(
            f"policy check of {payload['manifest']} against {payload['baseline']} "
            f"({payload['counts']['carriers']} claim(s) carrying a category, use or subject)"
        )
    for finding in payload["findings"]:
        failing = finding["kind"] in payload["policy"]["fail_on"]
        if quiet and not failing:
            continue
        label = "ERROR" if failing else "warn "
        subject = f' "{finding["subject"]}"' if finding.get("subject") else ""
        where = f" ({finding['ref']})" if finding.get("ref") else ""
        lines.append(
            f"  {label} [{finding['kind']}] {finding['path']}{subject}: {finding['message']}{where}"
        )
    if not payload["ok"]:
        failing = sum(1 for f in payload["findings"] if f["kind"] in payload["policy"]["fail_on"])
        lines.append("")
        lines.append(f"FAILED: {failing} finding(s) that --fail-on covers.")
        lines.append(
            "Either the code should not process this, or the baseline should say it may — and "
            "changing the baseline is a decision with an owner and a date on it."
        )
    elif not quiet:
        counts = payload["counts"]
        lines.append("")
        lines.append(
            f"OK: {counts['carriers']} claim(s) checked, "
            f"{counts['unpermitted_category']} unpermitted category/categories, "
            f"{counts['unpermitted_use']} unpermitted use(s), "
            f"{counts['special_category']} special-category item(s) present."
        )
    return "\n".join(lines)


def main(argv):
    output_json = False
    quiet = False
    baseline_raw = None
    fail_on = set(DEFAULT_FAIL_ON)
    positional = []

    for arg in argv:
        if arg == "--output=json":
            output_json = True
        elif arg == "--output=text":
            output_json = False
        elif arg == "--quiet":
            quiet = True
        elif arg.startswith("--baseline="):
            baseline_raw = arg.split("=", 1)[1]
        elif arg.startswith("--fail-on="):
            try:
                fail_on = parse_fail_on(arg.split("=", 1)[1])
            except ValueError as exc:
                sys.stderr.write(f"error: {exc}\n")
                return 2
        elif arg in ("-h", "--help"):
            sys.stdout.write(USAGE)
            return 0
        elif arg.startswith("-"):
            sys.stderr.write(f"error: unknown option '{arg}'\n" + USAGE)
            return 2
        else:
            positional.append(arg)

    if len(positional) != 1 or not baseline_raw:
        sys.stderr.write(USAGE)
        return 2

    path = pathlib.Path(positional[0])
    baseline_path = pathlib.Path(baseline_raw)
    for candidate in (path, baseline_path):
        if not candidate.is_file():
            sys.stderr.write(f"error: no such file: {candidate}\n")
            return 2
    try:
        document = load_document(path)
        baseline = load_document(baseline_path)
    except Exception as exc:  # noqa: BLE001 - any load failure is a usage error, not a finding
        sys.stderr.write(f"error: could not read a file ({exc})\n")
        return 2

    if not isinstance(document, dict) or not isinstance(baseline, dict):
        sys.stderr.write("error: both the manifest and the baseline must be mappings\n")
        return 2
    if baseline.get("kind") != "privacy-baseline":
        sys.stderr.write(
            f"error: {baseline_path} is not a privacy baseline "
            f"(kind is {baseline.get('kind')!r}, expected 'privacy-baseline')\n"
        )
        return 2

    special_roots = load_special_categories()

    findings, counts = evaluate(document, baseline, special_roots)
    failing = [f for f in findings if f["kind"] in fail_on]
    payload = {
        "check": "policy",
        "manifest": str(path),
        "baseline": str(baseline_path),
        "policy": {
            "fail_on": sorted(fail_on),
            "pinned_from": (baseline.get("source") or {}).get("pinned_from"),
            "special_categories_available": special_roots is not None,
        },
        "counts": counts,
        "ok": not failing,
        "findings": findings,
    }

    if output_json:
        sys.stdout.write(json.dumps(payload, indent=None if quiet else 2, sort_keys=True) + "\n")
    else:
        rendered = render_text(payload, quiet)
        if rendered:
            sys.stdout.write(rendered + "\n")
    return 0 if not failing else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
