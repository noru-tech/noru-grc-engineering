#!/usr/bin/env python3
"""Find interpretation blocks nobody stands behind any more.

Standard library only. No network, no installs, no credential. Everything this check needs is in
the manifest and a calendar, which is the point: it has to work on a fork pull request with no
secrets, where the alternative is no check at all.

Contract requirement 8 gives every claim an `interpretation` block — `owner`, `decided_at`,
`expires_at`, `rationale`, `refs[]`. The validator enforces that the block is *present and
well-formed*, and deliberately says nothing about whether the dates have passed: a fixture that
starts failing on a Tuesday is a worse test than no test. Time is this file's job instead.

Five things it reports:

  expired     an expiry that is already in the past. Nobody has stood behind this claim since it
              went stale.
  cadence     a claim outside the review cadence the caller declared for this path — either last
              decided longer ago than the cadence allows, or declaring a review window longer than
              the cadence allows. A cadence only exists if --max-age-days says so; this check does
              not invent a compliance opinion.
  expiring    an expiry inside the warning window. Reported, not failed, by default.
  unbounded   no expiry at all. The contract permits this for a genuinely point-in-time procedural
              claim, so it is reported and not failed by default.
  unparsable  a date that cannot be compared. A claim whose expiry cannot be read is a claim whose
              expiry cannot be trusted.

`interpretation.expires_at` is the field the contract requires. Where a piece also records the
expiry of the record it is about to create — `expiry_date` on an evidence upload — that field is
compared the same way, because a record that expires in Noru next week is not evidence of anything
the week after.

Usage:
    python3 scripts/check_expiry.py <manifest.yml|parsed.json> [--as-of=YYYY-MM-DD]
        [--warn-within-days=N] [--max-age-days=N] [--fail-on=<kinds>|none]
        [--output=json|text] [--quiet]
Exit codes: 0 = nothing that --fail-on covers, 1 = at least one such finding, 2 = usage / load error.
"""
import datetime
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

KINDS = ("expired", "cadence", "expiring", "unbounded", "unparsable")
DEFAULT_FAIL_ON = ("expired", "cadence", "unparsable")
DEFAULT_WARN_WITHIN_DAYS = 30

# Fields on the claim itself — not inside the interpretation block — that express the same thing:
# a date after which this is no longer current. Kept as a tuple so adding a piece that records the
# expiry under another name is a one-line change with a test, not a rewrite.
RECORD_EXPIRY_FIELDS = ("expiry_date",)

USAGE = (
    "usage: check_expiry.py <manifest.yml|parsed.json> [--as-of=YYYY-MM-DD] "
    "[--warn-within-days=N] [--max-age-days=N] [--fail-on=<kinds>|none] "
    "[--output=json|text] [--quiet]\n"
)


def load_document(path):
    """Return the manifest as a Python object, or raise ValueError with something readable."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    # The repo-level scripts may import the canonical loader directly; only an *installed plugin*
    # has to vendor it, and this file never ships inside one.
    sys.path.insert(0, str(ROOT / "contract" / "lib"))
    try:
        from yaml_mini import load_yaml  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - only if the repo is half-checked-out
        raise ValueError(f"cannot load contract/lib/yaml_mini.py ({exc})") from exc
    document, _loader = load_yaml(text)
    return document


def parse_date(value):
    """A date, or None if this is not one. Accepts a bare date and an ISO timestamp.

    Deliberately strict about the *type*. Every date in a manifest is an ISO string by contract, and
    the two YAML loaders have disagreed about that before: PyYAML used to resolve an unquoted
    `2026-08-01` to a datetime.date while the bundled fallback left it a string. Accepting both here
    would hide the next such divergence instead of reporting it as a claim that cannot be compared.
    """
    if not isinstance(value, str):
        return None
    match = DATE_PREFIX_RE.match(value.strip())
    if not match:
        return None
    try:
        return datetime.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _unparsable_message(raw):
    if not isinstance(raw, str):
        return (
            f"{raw!r} arrived as {type(raw).__name__}, not the ISO string the contract requires — "
            "the YAML loader in use resolved it to another type"
        )
    return f"'{raw}' is not a date this check can compare (expected YYYY-MM-DD)"


def walk_claims(node, path=""):
    """Yield (path, claim) for every mapping that carries an interpretation block.

    Piece-agnostic on purpose. The contract puts the interpretation block on the claim, wherever the
    claim happens to live in that piece's manifest, so finding the block is the only reliable way to
    find the claims without teaching this file about every piece that will ever exist.
    """
    if isinstance(node, dict):
        if isinstance(node.get("interpretation"), dict):
            yield path or "<root>", node
        for key, child in node.items():
            if key == "interpretation":
                continue
            yield from walk_claims(child, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from walk_claims(child, f"{path}[{index}]")


def _describe(claim):
    """A short human handle for a claim, so a finding names something and not just a path."""
    for field in ("key", "name", "title", "file", "value", "system"):
        value = claim.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return None


def evaluate_claim(path, claim, as_of, warn_within_days, max_age_days):
    """Every finding for one claim. Pure: same inputs, same output, no clock, no filesystem."""
    findings = []
    block = claim.get("interpretation")
    owner = block.get("owner") if isinstance(block.get("owner"), str) else None
    subject = _describe(claim)

    def add(kind, field, value, message, **extra):
        findings.append(
            {
                "kind": kind,
                "path": path,
                "field": field,
                "value": value,
                "owner": owner,
                "subject": subject,
                "message": message,
                **extra,
            }
        )

    decided_raw = block.get("decided_at")
    decided = parse_date(decided_raw)
    if decided_raw is not None and decided is None:
        add(
            "unparsable",
            "interpretation.decided_at",
            decided_raw if isinstance(decided_raw, str) else str(decided_raw),
            _unparsable_message(decided_raw),
        )

    # interpretation.expires_at first, then any record-level expiry the piece also records.
    expiries = [("interpretation.expires_at", block.get("expires_at"))]
    for field in RECORD_EXPIRY_FIELDS:
        if field in claim:
            expiries.append((field, claim.get(field)))

    bounded = False
    for field, raw in expiries:
        if raw is None:
            continue
        expires = parse_date(raw)
        if expires is None:
            add(
                "unparsable",
                field,
                raw if isinstance(raw, str) else str(raw),
                _unparsable_message(raw),
            )
            continue
        bounded = True
        days = (expires - as_of).days
        if days < 0:
            add(
                "expired",
                field,
                str(expires),
                f"expired {abs(days)} day(s) ago"
                + (f"; {owner} owned it" if owner else "")
                + " — nobody has stood behind this claim since it went stale",
                days=days,
            )
        elif days <= warn_within_days:
            add(
                "expiring",
                field,
                str(expires),
                f"expires in {days} day(s)"
                + (f"; ask {owner} to re-own it before then" if owner else ""),
                days=days,
            )

        if max_age_days > 0 and decided is not None:
            window = (expires - decided).days
            if window > max_age_days:
                add(
                    "cadence",
                    field,
                    str(expires),
                    f"declares a {window}-day review window, longer than the {max_age_days}-day "
                    "cadence declared for this path",
                    days=window,
                )

    if not bounded:
        add(
            "unbounded",
            "interpretation.expires_at",
            None,
            "no expiry — acceptable only for a genuinely point-in-time procedural claim, and then "
            "the rationale has to say so",
        )

    if max_age_days > 0 and decided is not None:
        age = (as_of - decided).days
        if age > max_age_days:
            add(
                "cadence",
                "interpretation.decided_at",
                str(decided),
                f"last decided {age} day(s) ago, past the {max_age_days}-day review cadence "
                "declared for this path",
                days=age,
            )

    return findings


def evaluate(document, as_of, warn_within_days, max_age_days):
    findings = []
    claims = 0
    for path, claim in walk_claims(document):
        claims += 1
        findings.extend(evaluate_claim(path, claim, as_of, warn_within_days, max_age_days))
    counts = {kind: sum(1 for f in findings if f["kind"] == kind) for kind in KINDS}
    counts["claims"] = claims
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
            f"expiry check of {payload['manifest']} as of {payload['as_of']} "
            f"({payload['counts']['claims']} claim(s))"
        )
    for finding in payload["findings"]:
        failing = finding["kind"] in payload["policy"]["fail_on"]
        if quiet and not failing:
            continue
        label = "ERROR" if failing else "warn "
        subject = f" \"{finding['subject']}\"" if finding.get("subject") else ""
        lines.append(f"  {label} [{finding['kind']}] {finding['path']}{subject}: {finding['message']}")
    if not payload["ok"]:
        failing = sum(1 for f in payload["findings"] if f["kind"] in payload["policy"]["fail_on"])
        lines.append("")
        lines.append(f"FAILED: {failing} claim finding(s) that --fail-on covers.")
    elif not quiet:
        counts = payload["counts"]
        lines.append("")
        lines.append(
            f"OK: {counts['claims']} claim(s), {counts['expired']} expired, "
            f"{counts['cadence']} outside cadence, {counts['expiring']} expiring soon, "
            f"{counts['unbounded']} unbounded."
        )
    return "\n".join(lines)


def main(argv):
    output_json = False
    quiet = False
    as_of_raw = None
    warn_within_days = DEFAULT_WARN_WITHIN_DAYS
    max_age_days = 0
    fail_on = set(DEFAULT_FAIL_ON)
    positional = []

    for arg in argv:
        if arg == "--output=json":
            output_json = True
        elif arg == "--output=text":
            output_json = False
        elif arg == "--quiet":
            quiet = True
        elif arg.startswith("--as-of="):
            as_of_raw = arg.split("=", 1)[1]
        elif arg.startswith("--warn-within-days=") or arg.startswith("--max-age-days="):
            flag, _, raw = arg.partition("=")
            try:
                number = int(raw)
            except ValueError:
                sys.stderr.write(f"error: {flag} needs a whole number of days, got '{raw}'\n")
                return 2
            if number < 0:
                sys.stderr.write(f"error: {flag} cannot be negative\n")
                return 2
            if flag == "--warn-within-days":
                warn_within_days = number
            else:
                max_age_days = number
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

    if len(positional) != 1:
        sys.stderr.write(USAGE)
        return 2

    if as_of_raw is None:
        as_of = datetime.datetime.now(datetime.timezone.utc).date()
    else:
        as_of = parse_date(as_of_raw)
        if as_of is None:
            sys.stderr.write(f"error: --as-of='{as_of_raw}' is not a date (expected YYYY-MM-DD)\n")
            return 2

    path = pathlib.Path(positional[0])
    if not path.is_file():
        sys.stderr.write(f"error: no such file: {path}\n")
        return 2
    try:
        document = load_document(path)
    except Exception as exc:  # noqa: BLE001 - any load failure is a usage error, not a finding
        sys.stderr.write(f"error: could not read {path} ({exc})\n")
        return 2

    findings, counts = evaluate(document, as_of, warn_within_days, max_age_days)
    failing = [f for f in findings if f["kind"] in fail_on]
    payload = {
        "check": "expiry",
        "manifest": str(path),
        "as_of": as_of.isoformat(),
        "policy": {
            "warn_within_days": warn_within_days,
            "max_age_days": max_age_days,
            "fail_on": sorted(fail_on),
        },
        "counts": counts,
        "ok": not failing,
        "findings": findings,
    }

    if output_json:
        sys.stdout.write(
            json.dumps(payload, indent=None if quiet else 2, sort_keys=True) + "\n"
        )
    else:
        rendered = render_text(payload, quiet)
        if rendered:
            sys.stdout.write(rendered + "\n")
    return 0 if not failing else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
