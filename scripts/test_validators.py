#!/usr/bin/env python3
"""Unit tests for the piece validators and the vendored YAML loader.

Standard library only. No test framework, no install step — this runs anywhere python3 exists,
which is the same promise the validators themselves make.

Covers:
  * the bundled fallback YAML loader, on the constructs our manifests actually use. This is the
    riskiest code in the repository: it only runs when PyYAML is absent, which is exactly when
    nobody is watching.
  * every valid fixture against its contract JSON Schema, so the schema and the hand-written
    validator cannot disagree about what a valid manifest is.
  * exit codes 0 / 1 / 2 and the "did you mean ...?" hint.
  * --output=json shape for both the passing and the failing case.

Usage:
    python3 scripts/test_validators.py [--output=json] [--quiet]
Exit codes: 0 = all tests pass, 1 = a test failed, 2 = usage / setup error.
"""
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jsonschema_mini import validate  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"

FALLBACK_SAMPLE = """\
# a leading comment
version: 0.1.0
piece: sample
source:
  slug: example-org/example-app   # trailing comment
  commit_sha: "0123456789abcdef"
  nested:
    deep: true
    empty_list: []
items:
  - name: first
    tags: [a, b, c]
    note: >
      folded text that spans
      two lines
    rationale: >
      Reviewed with the platform team and tracked in ticket #4412 until the
      rollout completes; see https://example.com/adr#retention for the C# port.
    summary: >-
      no trailing newline here
  - name: second
    literal: |
      line one
      # not a comment: inside a block scalar a hash is prose
      line two
    count: 42
    ratio: 1.5
    missing: null
flags:
  - alpha
  - beta
"""

# Verbatim PyYAML 6.0.3 output for FALLBACK_SAMPLE. Block scalars carry their trailing newline
# (clip chomping) and everything after a `#` inside them, because that `#` is content, not a
# comment -- get either wrong and prose is silently stored short on any machine without PyYAML.
FALLBACK_EXPECTED = {
    "version": "0.1.0",
    "piece": "sample",
    "source": {
        "slug": "example-org/example-app",
        "commit_sha": "0123456789abcdef",
        "nested": {"deep": True, "empty_list": []},
    },
    "items": [
        {
            "name": "first",
            "tags": ["a", "b", "c"],
            "note": "folded text that spans two lines\n",
            "rationale": (
                "Reviewed with the platform team and tracked in ticket #4412 until the "
                "rollout completes; see https://example.com/adr#retention for the C# port.\n"
            ),
            "summary": "no trailing newline here",
        },
        {
            "name": "second",
            "literal": (
                "line one\n# not a comment: inside a block scalar a hash is prose\nline two\n"
            ),
            "count": 42,
            "ratio": 1.5,
            "missing": None,
        },
    ],
    "flags": ["alpha", "beta"],
}


class Results:
    def __init__(self):
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append({"test": name, "ok": bool(ok), "detail": detail})
        return ok

    def skip(self, name, reason):
        """Record a check that could not run here, said out loud rather than counted as a pass."""
        self.rows.append({"test": name, "ok": True, "skipped": True, "detail": reason})

    @property
    def failures(self):
        return [r for r in self.rows if not r["ok"]]


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)


def piece_dirs():
    return sorted(p for p in PLUGINS.iterdir() if (p / "piece.json").is_file())


def test_fallback_loader(results):
    """The fallback loader must produce exactly what PyYAML would, on our constructs."""
    piece = piece_dirs()[0]
    module = load_module(piece / "scripts" / "validate_manifest.py", "piece_validator_probe")
    # _fallback_load is called directly, bypassing the PyYAML branch, so this test is meaningful
    # whether or not PyYAML happens to be installed in the environment running it.
    parsed = module._fallback_load(FALLBACK_SAMPLE)
    results.check(
        "fallback loader parses block scalars, flow sequences, nesting and comments",
        parsed == FALLBACK_EXPECTED,
        "" if parsed == FALLBACK_EXPECTED else f"got {json.dumps(parsed, sort_keys=True)}",
    )

    # The block-scalar bug this loader was written to fix: keys after a folded scalar must survive.
    after_block = module._fallback_load("a: >\n  folded\nb: kept\n")
    results.check(
        "keys after a folded block scalar are not dropped",
        after_block == {"a": "folded\n", "b": "kept"},
        f"got {after_block}",
    )

    # Content loss is worse than a parse failure, because nothing downstream can detect it: the
    # manifest validates, the rationale reads as a complete sentence, and the half that cited the
    # ticket is gone. A sibling key indented under a `-` must not be swallowed by the block either.
    hashed = module._fallback_load(
        "items:\n"
        "  - rationale: >\n"
        "      tracked in ticket #4412 until the\n"
        "      rollout completes\n"
        "    owner: platform\n"
    )
    expected_hashed = {
        "items": [{
            "rationale": "tracked in ticket #4412 until the rollout completes\n",
            "owner": "platform",
        }]
    }
    results.check(
        "a '#' inside a block scalar is prose, not the start of a comment",
        hashed == expected_hashed,
        f"got {hashed}",
    )

    empty = module._fallback_load("# only a comment\n")
    results.check("an empty document loads as None rather than raising", empty is None, f"got {empty}")

    # FALLBACK_EXPECTED is PyYAML's output, pinned so the comparison runs on a machine that has no
    # PyYAML — which is the only machine where the fallback loader actually runs. Pinned values go
    # stale silently, so where PyYAML *is* importable it is asked directly and the pin re-checked
    # against it. That closes the loop: the CI matrix runs a leg with PyYAML and one without, so
    # between them the fallback loader is compared to the real thing on every build.
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        results.skip(
            "the pinned expectations still match what PyYAML produces",
            "PyYAML is not importable here. The CI matrix runs a leg with it installed, which is "
            "where this comparison happens.",
        )
    else:
        actual = yaml.safe_load(FALLBACK_SAMPLE)
        results.check(
            "the pinned expectations still match what PyYAML produces",
            actual == FALLBACK_EXPECTED,
            "" if actual == FALLBACK_EXPECTED else f"PyYAML gave {json.dumps(actual, sort_keys=True)}",
        )


def test_suggestions(results):
    """Unknown keys get a difflib hint. A validator that only says 'invalid' wastes the reader."""
    piece = PLUGINS / "ai-inventory"
    validator = piece / "scripts" / "validate_manifest.py"
    fixture = piece / "fixtures" / "invalid-unknown-data-category.ai-inventory.yml"
    result = run(["python3", str(validator), str(fixture)])
    results.check(
        "an unknown vocabulary key produces a 'did you mean ...?' hint",
        "did you mean" in result.stdout,
        result.stdout.strip()[:300],
    )


def test_exit_codes(results):
    for piece in piece_dirs():
        validator = piece / "scripts" / "validate_manifest.py"
        name = piece.name

        results.check(
            f"[{name}] no argument exits 2",
            run(["python3", str(validator)]).returncode == 2,
        )
        results.check(
            f"[{name}] unknown option exits 2",
            run(["python3", str(validator), "--nope"]).returncode == 2,
        )
        with tempfile.TemporaryDirectory() as tmp:
            missing = pathlib.Path(tmp) / "absent.yml"
            results.check(
                f"[{name}] missing file exits 2",
                run(["python3", str(validator), str(missing)]).returncode == 2,
            )
            unparseable = pathlib.Path(tmp) / "broken.yml"
            unparseable.write_text("just a bare string\n", encoding="utf-8")
            code = run(["python3", str(validator), str(unparseable)]).returncode
            results.check(
                f"[{name}] a document that is not a mapping exits 1 or 2, never 0",
                code in (1, 2),
                f"exited {code}",
            )
        results.check(
            f"[{name}] --help exits 0",
            run(["python3", str(validator), "--help"]).returncode == 0,
        )


def test_json_output(results):
    for piece in piece_dirs():
        decl = json.loads((piece / "piece.json").read_text(encoding="utf-8"))
        validator = piece / "scripts" / "validate_manifest.py"
        name = piece.name

        valid = piece / decl["validator"]["fixtures"]["valid"][0]
        result = run(["python3", str(validator), str(valid), "--output=json", "--quiet"])
        try:
            payload = json.loads(result.stdout)
            ok = payload.get("ok") is True and result.returncode == 0
            detail = ""
        except json.JSONDecodeError as exc:
            ok, detail = False, f"{exc}: {result.stdout[:200]}"
        results.check(f"[{name}] --output=json on a valid manifest reports ok:true", ok, detail)

        invalid = piece / decl["validator"]["fixtures"]["invalid"][0]["path"]
        result = run(["python3", str(validator), str(invalid), "--output=json", "--quiet"])
        try:
            payload = json.loads(result.stdout)
            ok = (
                payload.get("ok") is False
                and len(payload.get("errors", [])) > 0
                and result.returncode == 1
            )
            detail = ""
        except json.JSONDecodeError as exc:
            ok, detail = False, f"{exc}: {result.stdout[:200]}"
        results.check(f"[{name}] --output=json on an invalid manifest reports ok:false", ok, detail)


def test_fixtures_match_schema(results):
    """A valid fixture must satisfy the contract schema, not just the hand-written validator."""
    for piece in piece_dirs():
        decl = json.loads((piece / "piece.json").read_text(encoding="utf-8"))
        schema_rel = decl.get("manifest_schema")
        if not schema_rel:
            continue
        schema = json.loads((ROOT / schema_rel).read_text(encoding="utf-8"))
        validator = piece / "scripts" / "validate_manifest.py"

        for rel in decl["validator"]["fixtures"]["valid"]:
            fixture = piece / rel
            with tempfile.TemporaryDirectory() as tmp:
                parsed_path = pathlib.Path(tmp) / "parsed.json"
                result = run(
                    ["python3", str(validator), str(fixture), f"--emit-parsed={parsed_path}",
                     "--quiet"]
                )
                if result.returncode != 0 or not parsed_path.is_file():
                    results.check(
                        f"[{piece.name}] {rel} parses and validates",
                        False,
                        result.stdout.strip()[:300],
                    )
                    continue
                doc = json.loads(parsed_path.read_text(encoding="utf-8"))
            errors = validate(doc, schema, schema)
            results.check(
                f"[{piece.name}] {rel} satisfies {schema_rel}",
                not errors,
                "; ".join(f"{p}: {m}" for p, m in errors[:5]),
            )

        # An invalid manifest must be rejected by the validator; the schema is a second opinion,
        # not the gate, because the validator enforces cross-references a schema cannot express.
        for entry in decl["validator"]["fixtures"]["invalid"]:
            result = run(["python3", str(validator), str(piece / entry["path"])])
            results.check(
                f"[{piece.name}] {entry['path']} is rejected with a useful message",
                result.returncode == 1 and entry["expect_message"] in (result.stdout + result.stderr),
                result.stdout.strip()[:300],
            )


def test_as_of_expiry(results):
    """review-signoff claims that --as-of turns a stale sign-off into an error. Assert it.

    The validator never reads the clock — that is what keeps it deterministic and what stops these
    fixtures rotting — so "has anyone stood behind this recently?" has to be asked explicitly. The
    README and the skill both promise this behaviour, so it gets a test.
    """
    piece = PLUGINS / "review-signoff"
    validator = piece / "scripts" / "validate_manifest.py"
    fixture = piece / "fixtures" / "valid.review-signoff.yml"

    # Judged on its own terms, with no date supplied, the fixture is valid.
    plain = run(["python3", str(validator), str(fixture), "--quiet"])
    results.check(
        "[review-signoff] a valid manifest passes when no --as-of is given",
        plain.returncode == 0,
        plain.stdout.strip()[:300],
    )

    # The day after signing, nothing has expired.
    fresh = run(["python3", str(validator), str(fixture), "--as-of=2026-07-04", "--quiet"])
    results.check(
        "[review-signoff] --as-of inside every validity window still passes",
        fresh.returncode == 0,
        fresh.stdout.strip()[:300],
    )

    # Long after the last sign-off lapsed, it must fail — what is due then is another review.
    stale = run(["python3", str(validator), str(fixture), "--as-of=2027-06-15"])
    results.check(
        "[review-signoff] an expired sign-off is an ERROR under --as-of",
        stale.returncode == 1 and "expired on" in stale.stdout,
        stale.stdout.strip()[:300],
    )

    # A malformed date is a usage error, not a silently ignored flag.
    bad = run(["python3", str(validator), str(fixture), "--as-of=last-tuesday"])
    results.check(
        "[review-signoff] a malformed --as-of exits 2 rather than being ignored",
        bad.returncode == 2,
        (bad.stdout + bad.stderr).strip()[:200],
    )


def main(argv):
    output_json = False
    quiet = False
    for arg in argv:
        if arg == "--output=json":
            output_json = True
        elif arg == "--output=text":
            output_json = False
        elif arg == "--quiet":
            quiet = True
        elif arg in ("-h", "--help"):
            sys.stdout.write("usage: test_validators.py [--output=json] [--quiet]\n")
            return 0
        else:
            sys.stderr.write(f"error: unknown option '{arg}'\n")
            return 2

    if not PLUGINS.is_dir() or not piece_dirs():
        sys.stderr.write("error: no pieces found under plugins/\n")
        return 2

    results = Results()
    test_fallback_loader(results)
    test_suggestions(results)
    test_exit_codes(results)
    test_as_of_expiry(results)
    test_json_output(results)
    test_fixtures_match_schema(results)

    ok = not results.failures
    if output_json:
        sys.stdout.write(
            json.dumps(
                {"ok": ok, "total": len(results.rows), "results": results.rows},
                indent=None if quiet else 2,
            )
            + "\n"
        )
        return 0 if ok else 1

    for row in results.rows:
        if not row["ok"]:
            print(f"  FAIL  {row['test']}")
            if row["detail"]:
                print(f"        {row['detail']}")
        elif row.get("skipped"):
            # Printed even under --quiet: a check that did not run is the one thing a reader
            # skimming a passing run still needs to see.
            print(f"  SKIP  {row['test']}")
            print(f"        {row['detail']}")
        elif not quiet:
            print(f"  ok    {row['test']}")
    if ok:
        print(f"\nOK: {len(results.rows)} test(s) passed.")
        return 0
    print(f"\nFAILED: {len(results.failures)} of {len(results.rows)} test(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
