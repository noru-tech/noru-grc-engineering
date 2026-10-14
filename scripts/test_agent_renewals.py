#!/usr/bin/env python3
"""Prove that an agent may renew a person's claim and do nothing else.

Standard library only, no network. The validator cases run plugins/privacy-datamap's validator on
its own fixtures with --base and --interpretation-policy; the CI cases drive the real
scripts/ci_check.py against a git repository built here, so the base revision is real history.

What it asserts, in one line each:

  * a renewal of a person's unchanged collection, under a grant, with a named operator, is valid
  * the same renewal carried forward unchanged is still valid
  * without --base the lineage is a warning, never a pass; with an empty --base it is an error
  * an agent cannot renew an agent's renewal, a changed classification, or a declaration
  * `renews` must name the decision at the base revision, and come after it
  * a person's claim cannot carry operator, policy_ref or renews
  * no policy means no agent; a grant of anything but renewals, a grant in change-control, a
    ceiling above the piece's, and a team alias as operator each make the policy invalid
  * CI judges the renewal against the base revision, policy included: a pull request that widens
    the policy is judged by the one it would replace, and a base it cannot read refuses
  * repo-enforcement's CODEOWNERS block covers .noru/interpretation-policy.yml

Usage:
    python3 scripts/test_agent_renewals.py [--output=json] [--quiet]
Exit codes: 0 = pass, 1 = a case failed, 2 = usage / setup error.
"""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ci_check  # noqa: E402
from jsonschema_mini import validate as schema_validate  # noqa: E402
from test_ci_mode import Results, build_green_repo, ci  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
PIECE = ROOT / "plugins" / "privacy-datamap"
VALIDATOR = PIECE / "scripts" / "validate_manifest.py"
FIXTURES = PIECE / "fixtures"
RENEWAL = FIXTURES / "valid-agent-renewal.privacy-datamap.yml"
BASE = "--base=fixtures/valid.privacy-datamap.yml"
POLICY = "--interpretation-policy=fixtures/agent-interpretation-policy.yml"

USAGE = "usage: test_agent_renewals.py [--output=json] [--quiet]\n"

POLICY_TEXT = """kind: interpretation-policy
version: 1
grants:
  - id: datamap-renewals
    piece: privacy-datamap
    claims:
      - renewal
    agents:
      - {agent}
    operators:
      - Fixture Owner
    max_days: 60
"""


def validator(manifest, *args):
    """Run the validator from the piece directory, where fixture arguments resolve."""
    completed = subprocess.run(
        [sys.executable, str(VALIDATOR), str(manifest), "--schema-only", "--output=json", *args],
        cwd=PIECE, capture_output=True, text=True, timeout=120, check=False,
    )
    try:
        return completed.returncode, json.loads(completed.stdout)
    except json.JSONDecodeError:
        return completed.returncode, {"errors": [{"message": completed.stderr}], "warnings": []}


def messages(result, key="errors"):
    return [row["message"] for row in result.get(key, [])]


def says(result, text, key="errors"):
    return any(text in message for message in messages(result, key))


def variant(tmp, name, edits, source=RENEWAL):
    """The renewal fixture with each (old, new) edit applied exactly once."""
    text = source.read_text(encoding="utf-8")
    for old, new in edits:
        if text.count(old) != 1:
            raise RuntimeError(f"{name}: expected one '{old.strip()}' in {source.name}")
        text = text.replace(old, new)
    path = pathlib.Path(tmp) / f"{name}.privacy-datamap.yml"
    path.write_text(text, encoding="utf-8")
    return path


def policy(tmp, name, text):
    path = pathlib.Path(tmp) / f"{name}.yml"
    path.write_text(text, encoding="utf-8")
    return f"--interpretation-policy={path}"


def case_valid_renewal(results, tmp):
    code, result = validator(RENEWAL, BASE, POLICY)
    results.check("a renewal of a person's unchanged collection is valid", code == 0, messages(result))
    schema = json.loads((ROOT / "contract" / "privacy-datamap.schema.json").read_text(encoding="utf-8"))
    document = ci_check.parse_manifest_text(RENEWAL.read_text(encoding="utf-8"))
    errors = schema_validate(document, schema, schema)
    results.check("the renewal satisfies the contract schema", not errors, errors[:3])
    policy_schema = json.loads(
        (ROOT / "contract" / "interpretation-policy.schema.json").read_text(encoding="utf-8")
    )
    policy_doc = ci_check.parse_manifest_text(
        (FIXTURES / "agent-interpretation-policy.yml").read_text(encoding="utf-8")
    )
    errors = schema_validate(policy_doc, policy_schema, policy_schema)
    results.check("the policy fixture satisfies its contract schema", not errors, errors[:3])

    code, result = validator(RENEWAL, f"--base={RENEWAL}", POLICY)
    results.check("a renewal carried forward unchanged stays valid", code == 0, messages(result))


def case_base_required(results, tmp):
    code, result = validator(RENEWAL, POLICY)
    results.check(
        "without --base the lineage is a warning, not a silent pass",
        code == 0 and says(result, "not checked against the base revision", "warnings"),
        result,
    )
    code, result = validator(RENEWAL, "--base=", POLICY)
    results.check(
        "a base that could not be read refuses the renewal",
        code == 1 and says(result, "none could be read"),
        messages(result),
    )


def case_lineage(results, tmp):
    again = variant(tmp, "again", [
        ('          decided_at: "2027-02-15"\n', '          decided_at: "2027-04-10"\n'),
        ('          expires_at: "2027-04-15"\n', '          expires_at: "2027-06-01"\n'),
    ])
    code, result = validator(again, f"--base={RENEWAL}", POLICY)
    results.check(
        "an agent cannot renew an agent's renewal",
        code == 1 and says(result, "the next renewal after an agent's is a person's"),
        messages(result),
    )

    reclassified = variant(tmp, "reclassified", [
        ("              - user.device.ip_address\n", "              - user.device\n"),
    ])
    code, result = validator(reclassified, BASE, POLICY)
    results.check(
        "an agent cannot renew a changed classification",
        code == 1 and says(result, "the classification changed since a person last owned it"),
        messages(result),
    )

    wrong = variant(tmp, "wrong-renews", [
        ('            decided_at: "2026-08-20"\n', '            decided_at: "2026-07-01"\n'),
    ])
    code, result = validator(wrong, BASE, POLICY)
    results.check(
        "renews must name the decision at the base revision",
        code == 1 and says(result, "does not name the decision it renews"),
        messages(result),
    )

    early = variant(tmp, "early", [
        ('          decided_at: "2027-02-15"\n', '          decided_at: "2026-08-01"\n'),
    ])
    code, result = validator(early, BASE, POLICY)
    results.check(
        "a renewal cannot be decided before what it renews",
        code == 1 and says(result, "is before the decision it renews"),
        messages(result),
    )


def case_declaration(results, tmp):
    declaration = variant(tmp, "declaration", [(
        "          owner: Dana Okafor\n          decided_at: \"2026-08-20\"\n"
        "          expires_at: \"2027-02-20\"\n          rationale: >-\n"
        "            Sign-in",
        "          owner: datamap-renewal-agent\n          owner_kind: agent\n"
        "          operator: Dana Okafor\n          policy_ref: datamap-renewals\n"
        "          renews:\n            owner: Dana Okafor\n            decided_at: \"2026-08-20\"\n"
        "          decided_at: \"2027-02-15\"\n          expires_at: \"2027-04-15\"\n"
        "          rationale: >-\n            Sign-in",
    )])
    code, result = validator(declaration, BASE, POLICY)
    results.check(
        "an agent cannot own a privacy declaration",
        code == 1 and says(result, "A declaration is a person's to sign"),
        messages(result),
    )


def case_person_fields(results, tmp):
    person = variant(tmp, "person-operator", [
        ("          owner: Dana Okafor\n          decided_at: \"2026-08-20\"\n"
         "          expires_at: \"2027-02-20\"\n          rationale: >-\n            Sign-in",
         "          owner: Dana Okafor\n          operator: Dana Okafor\n"
         "          decided_at: \"2026-08-20\"\n"
         "          expires_at: \"2027-02-20\"\n          rationale: >-\n            Sign-in"),
    ])
    code, result = validator(person, BASE, POLICY)
    results.check(
        "a person's claim cannot carry an operator",
        code == 1 and says(result, "belongs to an agent's renewal"),
        messages(result),
    )


def case_policy(results, tmp):
    code, result = validator(RENEWAL, BASE, policy(tmp, "empty", ""))
    results.check(
        "no interpretation policy means no agent owner",
        code == 1 and says(result, "outside the interpretation policy: there is no committed"),
        messages(result),
    )

    grant = (FIXTURES / "agent-interpretation-policy.yml").read_text(encoding="utf-8")
    cases = (
        ("new-claims", ("      - renewal\n", "      - renewal\n      - new_claim\n"),
         "no policy can grant them"),
        ("change-control", ("    max_days: 60\n",
                            "    max_days: 60\n  - id: segregation\n    piece: change-control\n"
                            "    claims:\n      - renewal\n    agents:\n      - datamap-renewal-agent\n"
                            "    operators:\n      - Dana Okafor\n    max_days: 30\n"),
         "change-control never accepts an agent owner"),
        ("long-ceiling", ("    max_days: 60\n", "    max_days: 120\n"),
         "a policy may shorten it, never lengthen it"),
        ("team-operator", ("      - Dana Okafor\n", "      - Dana Okafor\n      - Privacy Team\n"),
         "reads as a team alias"),
    )
    for name, (old, new), expected in cases:
        if grant.count(old) != 1:
            raise RuntimeError(f"{name}: expected one '{old.strip()}' in the policy fixture")
        code, result = validator(RENEWAL, BASE, policy(tmp, name, grant.replace(old, new)))
        results.check(
            f"policy {name}: invalid, and the renewal relying on it refused",
            code == 1 and says(result, expected),
            messages(result),
        )

    # 75 days: inside the piece's agent ceiling of 90, outside the grant's 60.
    longer = variant(tmp, "longer", [
        ('          expires_at: "2027-04-15"\n', '          expires_at: "2027-05-01"\n'),
    ])
    code, result = validator(longer, BASE, POLICY)
    results.check(
        "a grant's shorter ceiling applies on top of the piece's",
        code == 1 and says(result, "grant 'datamap-renewals' lets an agent's renewal stand for at most 60")
        and not says(result, "an agent's renewal may stand for at most 90"),
        messages(result),
    )


def git(repo, *args):
    completed = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
         *args],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {completed.stderr}")
    return completed.stdout


def renew(manifest, agent="datamap-renewal-agent"):
    """Make the first collection an agent's renewal of the person's decision the fixture signed."""
    spec = importlib.util.spec_from_file_location(
        "privacy_reconcile_renewals", PIECE / "scripts" / "reconcile.py"
    )
    reconcile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reconcile)
    document = ci_check.parse_manifest_text(manifest.read_text(encoding="utf-8"))
    collection = document["dataset"][0]["collections"][0]
    signed = collection["interpretation"]
    collection["interpretation"] = {
        "owner": agent,
        "owner_kind": "agent",
        "operator": "Fixture Owner",
        "policy_ref": "datamap-renewals",
        "renews": {"owner": signed["owner"], "decided_at": signed["decided_at"]},
        "decided_at": "2026-08-25",
        "expires_at": "2026-10-20",
        "rationale": "The collection is exactly what Fixture Owner signed, column for column.",
    }
    manifest.write_text(reconcile.to_yaml(document), encoding="utf-8")


def validate_step(report):
    return next((s for s in (report or {}).get("steps", []) if s["step"] == "validate"), {})


def invalid_messages(report):
    return [f["message"] for f in (report or {}).get("findings", []) if f["kind"] == "invalid"]


def signed_repo(tmp, name, agent="datamap-renewal-agent"):
    repo, manifest = build_green_repo(pathlib.Path(tmp) / name, "privacy-datamap")
    git(repo, "init", "-q", "-b", "main")
    (repo / ".noru" / "interpretation-policy.yml").write_text(
        POLICY_TEXT.format(agent=agent), encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a person signs the data map")
    return repo, manifest


def case_ci_renewal(results, tmp):
    repo, manifest = signed_repo(tmp, "ci-renewal")
    base = git(repo, "rev-parse", "HEAD").strip()
    renew(manifest)
    git(repo, "checkout", "-qb", "renew")
    git(repo, "commit", "-qam", "an agent renews it")

    code, report = ci(repo, "--steps=validate", "--base-ref=main", piece="privacy-datamap")
    step = validate_step(report)
    results.check(
        "CI: a renewal judged against the merge base passes",
        code == 0 and step.get("status") == "pass" and step.get("renewal_base") == base[:12],
        (code, step, invalid_messages(report)),
    )

    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "renew", "-m", "merge the renewal")
    code, report = ci(repo, "--steps=validate", piece="privacy-datamap")
    step = validate_step(report)
    results.check(
        "CI: after the merge, the push is judged against the branch before it",
        code == 0 and step.get("renewal_base") == base[:12],
        (code, step, invalid_messages(report)),
    )


def case_ci_widened_policy(results, tmp):
    repo, manifest = signed_repo(tmp, "ci-widened", agent="some-other-agent")
    git(repo, "checkout", "-qb", "widen")
    (repo / ".noru" / "interpretation-policy.yml").write_text(
        POLICY_TEXT.format(agent="datamap-renewal-agent"), encoding="utf-8"
    )
    renew(manifest)
    git(repo, "commit", "-qam", "widen the policy and renew in the same change")
    code, report = ci(repo, "--steps=validate", "--base-ref=main", piece="privacy-datamap")
    results.check(
        "CI: a change that widens the policy is judged by the policy it would replace",
        code != 0 and any("does not cover the agent 'datamap-renewal-agent'" in m
                          for m in invalid_messages(report)),
        (code, invalid_messages(report)),
    )


def case_ci_new_claim(results, tmp):
    repo, manifest = signed_repo(tmp, "ci-new")
    document = ci_check.parse_manifest_text(manifest.read_text(encoding="utf-8"))
    # The person's signature is removed at the base, so what the agent signs next is new to it.
    document["dataset"][0]["collections"][0]["needs_review"] = True
    del document["dataset"][0]["collections"][0]["interpretation"]
    spec = importlib.util.spec_from_file_location(
        "privacy_reconcile_new", PIECE / "scripts" / "reconcile.py"
    )
    reconcile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reconcile)
    unsigned = reconcile.to_yaml(document)
    signed_text = manifest.read_text(encoding="utf-8")
    manifest.write_text(unsigned, encoding="utf-8")
    git(repo, "commit", "-qam", "the collection is reopened for review")
    manifest.write_text(signed_text, encoding="utf-8")
    renew(manifest)
    code, report = ci(repo, "--steps=validate", piece="privacy-datamap")
    results.check(
        "CI: an agent cannot sign a collection nobody signed at the base",
        code != 0 and any("an agent cannot own a new claim" in m for m in invalid_messages(report)),
        (code, invalid_messages(report)),
    )


def case_ci_no_base(results, tmp):
    repo, manifest = build_green_repo(pathlib.Path(tmp) / "ci-no-base", "privacy-datamap")
    (repo / ".noru" / "interpretation-policy.yml").write_text(
        POLICY_TEXT.format(agent="datamap-renewal-agent"), encoding="utf-8"
    )
    renew(manifest)
    code, report = ci(repo, "--steps=validate", piece="privacy-datamap")
    step = validate_step(report)
    results.check(
        "CI: with no history to read, an agent's renewal is refused, not trusted",
        code != 0 and step.get("renewal_base") == "unavailable"
        and any("none could be read" in m for m in invalid_messages(report)),
        (code, step, invalid_messages(report)),
    )


def case_ci_other_pieces(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "ci-other", "ai-inventory")
    code, report = ci(repo, "--steps=validate", piece="ai-inventory")
    results.check(
        "CI: a piece that admits no agent owner gets no base arguments",
        code == 0 and "renewal_base" not in validate_step(report),
        (code, validate_step(report)),
    )


def case_codeowners(results, tmp):
    template = (ROOT / "plugins/repo-enforcement/assets/CODEOWNERS.template").read_text(
        encoding="utf-8"
    )
    target = "/.noru/interpretation-policy.yml"
    patterns = [line.split()[0] for line in template.splitlines()
                if line.strip() and not line.startswith("#")]
    covered = [p for p in patterns if target == p or (p.endswith("/") and target.startswith(p))]
    results.check(
        "repo-enforcement's CODEOWNERS block protects the interpretation policy",
        bool(covered),
        patterns,
    )


CASES = (
    case_valid_renewal,
    case_base_required,
    case_lineage,
    case_declaration,
    case_person_fields,
    case_policy,
    case_ci_renewal,
    case_ci_widened_policy,
    case_ci_new_claim,
    case_ci_no_base,
    case_ci_other_pieces,
    case_codeowners,
)


def main(argv):
    output_json = "--output=json" in argv
    quiet = "--quiet" in argv
    unknown = [a for a in argv if a not in ("--output=json", "--quiet")]
    if unknown:
        sys.stderr.write(USAGE)
        return 2

    results = Results()
    with tempfile.TemporaryDirectory(prefix="noru-agent-renewals-") as tmp:
        for case in CASES:
            try:
                case(results, tmp)
            except Exception as exc:  # noqa: BLE001 - a crashing case is a failing case
                results.check(case.__name__, False, f"raised {type(exc).__name__}: {exc}")

    if output_json:
        sys.stdout.write(
            json.dumps({"ok": not results.failures, "tests": results.rows},
                       indent=None if quiet else 2, sort_keys=True)
            + "\n"
        )
        return 0 if not results.failures else 1
    for row in results.rows:
        if not row["ok"]:
            print(f"  FAIL  {row['test']}: {row['detail']}")
        elif not quiet:
            print(f"  ok    {row['test']}")
    if results.failures:
        print(f"\nFAILED: {len(results.failures)} of {len(results.rows)} case(s).")
        return 1
    if not quiet:
        print(f"\nOK: {len(results.rows)} case(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
