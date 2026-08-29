#!/usr/bin/env python3
"""Prove CI mode actually fails, and actually stops failing.

Standard library only, no network, no credential. Every case below runs the real
scripts/ci_check.py against a throwaway copy of tests/fixture-repo/, because a gate that has never
been observed failing is a guess with a green checkmark.

What it asserts, in one line each:

  * a manifest that matches the repository passes                                    -> exit 0
  * a repository that gained a model provider fails, and the message names it        -> exit 3
  * a manifest carrying an expiry in the past fails, and the message names the owner -> exit 4
  * both at once is distinguishable from either                                      -> exit 1
  * warn-only mode reports exactly the same findings and exits 0                     -> exit 0
  * calling it wrong is not a compliance finding                                     -> exit 2
  * a check that cannot run is not a pass                                            -> exit 6
  * no credential means the push half is skipped, not failed                         -> exit 0
  * NORU_API_KEY never reaches a step that does not need it
  * the orchestrator hardcodes no piece name; it reads piece.json

A fixed --as-of is passed everywhere so these cases mean the same thing next year.

Usage:
    python3 scripts/test_ci_mode.py [--output=json] [--quiet] [--emit-fixture=<dir>]
Exit codes: 0 = pass, 1 = a case failed, 2 = usage / setup error.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ci_check  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
FIXTURE_REPO = ROOT / "tests" / "fixture-repo"
CI_CHECK = ROOT / "scripts" / "ci_check.py"
CHECK_EXPIRY = ROOT / "scripts" / "check_expiry.py"

# Fixed so that every assertion below means the same thing in five years' time.
AS_OF = "2026-08-27"

# Where the fixture manifests' citations are re-pointed. Any file that exists in the fixture repo
# will do; the point is only that the citations resolve, so `dangling_ref` noise does not drown the
# finding under test.
REF_TARGET = "src/inference.ts"
REF_TARGET_LINES = 15
REF_TOKEN_RE = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.][\w./-]*\.[A-Za-z0-9]+):(\d+)\b")

# A new model provider arriving in a pull request: the exact case the drift gate exists for.
NEW_PROVIDER_FILE = "src/summarize.ts"
NEW_PROVIDER_SOURCE = """// Fixture source added by scripts/test_ci_mode.py. Not real code; nothing here runs.
import { generateText } from "ai"
import { anthropic } from "@ai-sdk/anthropic"

export async function summarize(text: string) {
  return generateText({ model: anthropic("claude-sonnet-4-5"), prompt: text })
}
"""


class Results:
    def __init__(self):
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append({"test": name, "ok": bool(ok), "detail": str(detail)[:400]})
        return ok

    @property
    def failures(self):
        return [r for r in self.rows if not r["ok"]]


def run(cmd, env=None):
    merged = dict(os.environ)
    merged.pop("NORU_API_KEY", None)
    if env:
        merged.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False, env=merged)


def ci(repo, *args, piece="ai-inventory", env=None):
    """Run the real orchestrator and return (returncode, parsed report or None)."""
    completed = run(
        [
            sys.executable,
            str(CI_CHECK),
            f"--piece={piece}",
            f"--repo={repo}",
            f"--as-of={AS_OF}",
            "--output=json",
            "--quiet",
            *args,
        ],
        env=env,
    )
    try:
        return completed.returncode, json.loads(completed.stdout)
    except json.JSONDecodeError:
        return completed.returncode, None


def kinds(report):
    return [f["kind"] for f in (report or {}).get("findings", [])]


def build_green_repo(dest, piece_name="ai-inventory"):
    """A repository whose committed manifest genuinely matches it: the CI-mode green path.

    This is what a developer has after running :scan and filling in the interpretation blocks —
    reproduced here without a human, by taking the piece's own valid fixture and re-pointing the
    two things that are necessarily repository-specific: the derived digest, and the citations.
    """
    repo = pathlib.Path(dest)
    if repo.exists():
        shutil.rmtree(repo)
    shutil.copytree(FIXTURE_REPO, repo)

    piece = PLUGINS / piece_name
    decl = json.loads((piece / "piece.json").read_text(encoding="utf-8"))
    collector = piece / decl["collector"]["entrypoint"]
    completed = run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"])
    if completed.returncode != 0:
        raise RuntimeError(f"collector failed on the fixture repo: {completed.stderr[:400]}")
    digest = json.loads(completed.stdout)["derived_digest"]

    fixture = piece / decl["validator"]["fixtures"]["valid"][0]
    text = fixture.read_text(encoding="utf-8")
    if "derived_digest" in text:
        text = re.sub(r"derived_digest: [0-9a-f]{64}", f"derived_digest: {digest}", text)
    else:
        # Not every valid fixture carries one; a real collector always stamps it, so stamp it here
        # rather than pretending a fixture-shaped manifest is what CI mode meets in the wild.
        text = re.sub(
            r"(\n  generated_by: [^\n]+\n)", rf"\1  derived_digest: {digest}\n", text, count=1
        )
    text = REF_TOKEN_RE.sub(
        lambda m: f"{REF_TARGET}:{min(int(m.group(2)), REF_TARGET_LINES)}", text
    )
    manifest = repo / decl["artifact"]
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(text, encoding="utf-8")
    return repo, manifest


def expire_every_claim(manifest, when="2020-01-01"):
    """Turn every interpretation into one nobody has stood behind since it went stale."""
    text = manifest.read_text(encoding="utf-8")
    text = re.sub(r"expires_at: \d{4}-\d{2}-\d{2}", f"expires_at: {when}", text)
    manifest.write_text(text, encoding="utf-8")


def add_new_provider(repo):
    (repo / NEW_PROVIDER_FILE).write_text(NEW_PROVIDER_SOURCE, encoding="utf-8")


# --- the cases ----------------------------------------------------------------------------------
def case_green(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "green")
    code, report = ci(repo)
    results.check("green: exits 0", code == 0, f"exit {code}")
    results.check("green: no drift finding", "drift" not in kinds(report), kinds(report))
    results.check("green: no expired finding", "expired" not in kinds(report), kinds(report))
    results.check(
        "green: citations all resolve", "dangling_ref" not in kinds(report), kinds(report)
    )
    results.check(
        "green: every step ran",
        report is not None and step_statuses(report) == GREEN_STEPS,
        (report or {}).get("steps"),
    )


# (step, status) for the offline half of a green run. Named rather than counted so a step that
# silently stops running is a failure here and not a smaller list nobody looks at.
#
# `policy` is `skipped` because build_green_repo commits no privacy baseline: the orchestrator has
# no default policy of its own and must never report the absence of one as a pass. The repo that
# does commit one is case_policy below.
GREEN_STEPS = [
    ("scan", "pass"),
    ("validate", "pass"),
    ("expiry", "pass"),
    ("policy", "skipped"),
]


def step_statuses(report):
    return [(s["step"], s["status"]) for s in report["steps"]]


# A baseline that permits exactly what the privacy-datamap fixture contains, and one that does not.
# Written as prefixes because Fideslang is a tree and the baseline has to be readable: `user.contact`
# standing for `user.contact.email` is the whole reason the allow list is three lines and not thirty.
BASELINE_PERMISSIVE = """version: 0.1.0
kind: privacy-baseline
source:
  pinned_from:
    fetched_at: 2026-08-27T09:00:00Z
    via: [getPrivacyTaxonomy]
data_categories:
  allow: [user.contact, user.device, user.authorization]
data_uses:
  allow: [essential]
data_subjects:
  allow: [customer]
interpretation:
  owner: fixture.owner@example.com
  decided_at: 2026-08-01
  expires_at: 2027-08-01
  rationale: The taxonomy this fixture repository is allowed to process.
"""

BASELINE_STRICT = """version: 0.1.0
kind: privacy-baseline
source:
  pinned_from:
    fetched_at: 2026-08-27T09:00:00Z
    via: [getPrivacyTaxonomy]
data_categories:
  allow: [user.contact]
  deny: [user.device.ip_address]
data_uses:
  allow: [essential]
data_subjects:
  allow: [customer]
interpretation:
  owner: fixture.owner@example.com
  decided_at: 2026-08-01
  expires_at: 2027-08-01
  rationale: Deliberately narrower than the fixture, so the gate is observed failing.
"""

BASELINE_EXPIRED = BASELINE_PERMISSIVE.replace("expires_at: 2027-08-01", "expires_at: 2020-01-01")


def case_policy(results, tmp):
    """The privacy gate: was this personal data ever agreed to, not merely signed for?

    Drift already asks whether anybody looked. This asks whether the answer was allowed to be yes,
    which is a different question and the one an unpermitted column gets wrong.
    """
    repo, _ = build_green_repo(pathlib.Path(tmp) / "policy", "privacy-datamap")
    baseline = repo / ".noru" / "privacy-baseline.yml"

    # No baseline at all: the step has no policy of its own and must not invent one — but it must
    # never report that as a pass either.
    code, report = ci(repo, piece="privacy-datamap")
    policy_step = next(
        (s for s in (report or {}).get("steps", []) if s["step"] == "policy"), None
    )
    results.check("policy: no baseline exits 0", code == 0, f"exit {code}")
    results.check(
        "policy: no baseline is skipped, never passed",
        policy_step is not None and policy_step["status"] == "skipped",
        policy_step,
    )

    baseline.write_text(BASELINE_PERMISSIVE, encoding="utf-8")
    code, report = ci(repo, piece="privacy-datamap")
    results.check("policy: a baseline that permits the map exits 0", code == 0, f"exit {code}")
    results.check(
        "policy: prefixes cover their subtree",
        "unpermitted_category" not in kinds(report),
        kinds(report),
    )

    baseline.write_text(BASELINE_STRICT, encoding="utf-8")
    code, report = ci(repo, piece="privacy-datamap")
    results.check("policy: an unpermitted category exits 7", code == 7, f"exit {code}")
    results.check(
        "policy: raises an unpermitted_category finding",
        "unpermitted_category" in kinds(report),
        kinds(report),
    )
    denied = [
        f for f in (report or {}).get("findings", []) if f["kind"] == "unpermitted_category"
    ]
    # Two different code paths reach this finding and both must be exercised: an explicit `deny`
    # entry, and a value that is simply absent from a closed `allow` list. They produce different
    # messages because the fix is different — one was ruled out, the other was never considered.
    by_value = {str(f.get("value")): f for f in denied}
    results.check(
        "policy: an explicit deny names the pattern that denied it",
        "user.device.ip_address" in by_value
        and "denies" in by_value["user.device.ip_address"]["message"],
        sorted(by_value),
    )
    results.check(
        "policy: a closed allow list says the value was never listed",
        "user.authorization.password" in by_value
        and "not in the baseline" in by_value["user.authorization.password"]["message"],
        sorted(by_value),
    )
    results.check(
        "policy: the finding cites where in the manifest it is",
        all(f.get("path") and f.get("subject") for f in denied),
        denied[:2],
    )

    code, report = ci(repo, "--mode=warn", piece="privacy-datamap")
    results.check("policy: warn mode reports and exits 0", code == 0, f"exit {code}")
    results.check(
        "policy: warn mode finds the same thing",
        "unpermitted_category" in kinds(report),
        kinds(report),
    )

    # The baseline is a claim like any other, so the expiry step ages it. A policy nobody has
    # re-owned in six years is not a policy, and the gate standing on it is not a gate.
    baseline.write_text(BASELINE_EXPIRED, encoding="utf-8")
    code, report = ci(repo, f"--baseline={baseline}", piece="privacy-datamap")
    results.check(
        "policy: an expired baseline still gates the map it permits", code == 0, f"exit {code}"
    )
    expiry_code, _ = run_expiry(baseline)
    results.check(
        "policy: check_expiry ages the baseline itself", expiry_code == 1, f"exit {expiry_code}"
    )


def run_expiry(path):
    completed = run(
        [
            sys.executable,
            str(CHECK_EXPIRY),
            str(path),
            f"--as-of={AS_OF}",
            "--output=json",
            "--quiet",
        ]
    )
    try:
        return completed.returncode, json.loads(completed.stdout)
    except json.JSONDecodeError:
        return completed.returncode, None


# A repository whose only schema is in a format the collector cannot read. Five formats, so a single
# regex going stale cannot silently turn this case green.
BLINDSPOT_FILES = {
    "src/user.model.ts": (
        "import mongoose from \"mongoose\"\n"
        "const UserSchema = new mongoose.Schema({ email: String })\n"
    ),
    "src/patient.entity.ts": "@Entity()\nexport class Patient { diagnosis: string }\n",
    "db/schema.rb": (
        "ActiveRecord::Schema.define(version: 1) do\n"
        "  create_table \"members\" do |t|\n    t.string \"ni_number\"\n  end\nend\n"
    ),
    "src/models.go": "type Account struct {\n\tEmail string `gorm:\"column:email\"`\n}\n",
    "api/openapi.yaml": "openapi: 3.0.0\ninfo: { title: x, version: \"1\" }\n",
}


def case_coverage(results, tmp):
    """An empty data map must never be reportable as a clean one.

    This is the most dangerous failure this piece has, because it is silent: drift, expiry and
    policy all pass on an empty set, so a repository whose schema the collector cannot read gets a
    green build that means nothing at all.
    """
    repo = pathlib.Path(tmp) / "coverage"
    repo.mkdir(parents=True, exist_ok=True)
    for rel, body in BLINDSPOT_FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    code, report = ci(repo, piece="privacy-datamap")
    results.check("coverage: an unreadable schema is a broken gate, not a pass", code == 6, f"exit {code}")
    results.check(
        "coverage: raises a coverage finding", "coverage" in kinds(report), kinds(report)
    )
    finding = next((f for f in (report or {}).get("findings", []) if f["kind"] == "coverage"), None)
    results.check(
        "coverage: the finding names every format it saw",
        finding is not None
        and {"mongoose", "typeorm", "activerecord", "gorm", "openapi"} <= set(finding.get("formats") or []),
        (finding or {}).get("formats"),
    )
    results.check(
        "coverage: the finding cites where each one is",
        finding is not None and all(":" in ref for ref in (finding.get("refs") or [])),
        (finding or {}).get("refs"),
    )

    # A broken gate is loud in warn mode too. This is the promise docs/ci-mode.md makes about 6.
    code, _ = ci(repo, "--mode=warn", piece="privacy-datamap")
    results.check("coverage: warn mode does not suppress a broken gate", code == 6, f"exit {code}")

    # A repository with nothing to parse and nothing it missed is genuinely clean, and must not be
    # dragged down by this check — otherwise every repository without a database fails forever.
    empty = pathlib.Path(tmp) / "coverage-empty"
    (empty / "src").mkdir(parents=True, exist_ok=True)
    (empty / "src" / "util.ts").write_text("export const add = (a: number) => a + 1\n", encoding="utf-8")
    code, report = ci(empty, piece="privacy-datamap")
    results.check(
        "coverage: a repository with no schema at all is not a coverage failure",
        "coverage" not in kinds(report),
        kinds(report),
    )

    # Found by running this against this repository, which is what dogfooding is for: a marker
    # matching `"$schema": ".../json-schema.org/..."` fires on every JSON Schema document, and
    # contract/ produced ten candidates holding no personal data at all. A check that fires on
    # every repository with a schema directory is a check somebody turns off.
    schemas = pathlib.Path(tmp) / "coverage-schemas"
    (schemas / "contract").mkdir(parents=True, exist_ok=True)
    (schemas / "contract" / "thing.schema.json").write_text(
        json.dumps({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "A manifest format, not a stored record",
            "properties": {"version": {"type": "string"}},
        }),
        encoding="utf-8",
    )
    (schemas / "validate.ts").write_text(
        "import { z } from 'zod'\nexport const Body = z.object({ page: z.number() })\n",
        encoding="utf-8",
    )
    code, report = ci(schemas, piece="privacy-datamap")
    results.check(
        "coverage: a schema describing a format is not a schema describing a record",
        "coverage" not in kinds(report) and code != 6,
        f"exit {code}, kinds {kinds(report)}",
    )

    # Supported and unsupported side by side: the map is partial, not absent. Reported, not gated,
    # because failing here would block every repository that has one Zod file next to its SQL.
    partial = pathlib.Path(tmp) / "coverage-partial"
    (partial / "db").mkdir(parents=True, exist_ok=True)
    for rel, body in BLINDSPOT_FILES.items():
        path = partial / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    (partial / "db" / "schema.sql").write_text(
        "CREATE TABLE accounts (\n  id INTEGER PRIMARY KEY,\n  email TEXT\n);\n", encoding="utf-8"
    )
    code, report = ci(partial, piece="privacy-datamap")
    results.check("coverage: a partial map is reported, not a tooling failure", code != 6, f"exit {code}")
    results.check(
        "coverage: a partial map still raises the finding", "coverage" in kinds(report), kinds(report)
    )
    code, _ = ci(partial, "--fail-on=coverage", piece="privacy-datamap")
    results.check("coverage: --fail-on=coverage makes a partial map gate", code == 6, f"exit {code}")


def git_init(repo, message):
    """A real commit, so the base comparison is exercised against real git rather than a stub."""
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", message],
    ):
        run(["git", "-C", str(repo), *args])


def case_base_ref(results, tmp):
    """Which of these findings is this branch responsible for?

    A team adopting the gate on an existing repository has a backlog, and a gate that blocks on the
    backlog on day one is a gate that gets reverted on day two. The delta is what lets them gate on
    what they are adding while they burn the rest down.
    """
    repo, _ = build_green_repo(pathlib.Path(tmp) / "base-ref", "privacy-datamap")
    git_init(repo, "the map as it stood before this branch")
    baseline = repo / ".noru" / "privacy-baseline.yml"
    baseline.write_text(BASELINE_STRICT, encoding="utf-8")

    # Nothing has changed since the base, so every finding is somebody else's problem.
    code, report = ci(repo, "--base-ref=HEAD", piece="privacy-datamap")
    seen = {f.get("first_seen") for f in (report or {}).get("findings", [])}
    results.check("base-ref: the backlog is marked pre_existing", seen == {"pre_existing"}, seen)
    results.check("base-ref: and still gates by default", code == 7, f"exit {code}")

    code, _ = ci(repo, "--base-ref=HEAD", "--gate-on-new", piece="privacy-datamap")
    results.check("base-ref: --gate-on-new lets the backlog through", code == 0, f"exit {code}")

    # Now this branch introduces one. The structure digest has to be re-stamped because adding a
    # field is exactly what that digest exists to notice — which is itself worth asserting.
    manifest = repo / ".noru" / "privacy-datamap.yml"
    before = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        before.replace(
            "          - name: password_hash",
            "          - name: passport_number\n"
            "            data_categories: [user.government_id.passport_number]\n"
            f"            refs: [\"{REF_TARGET}:9\"]\n"
            "          - name: password_hash",
            1,
        ),
        encoding="utf-8",
    )
    code, _ = ci(repo, "--base-ref=HEAD", "--gate-on-new", piece="privacy-datamap")
    results.check(
        "base-ref: adding a field without re-signing is caught by the structure digest",
        code == 5,
        f"exit {code}",
    )

    restamp_structure_digest(manifest)
    code, report = ci(repo, "--base-ref=HEAD", "--gate-on-new", piece="privacy-datamap")
    new = [f for f in (report or {}).get("findings", []) if f.get("first_seen") == "this_pr"]
    results.check("base-ref: a new violation gates even with --gate-on-new", code == 7, f"exit {code}")
    results.check(
        "base-ref: and only the new one is attributed to this branch",
        [f["value"] for f in new] == ["user.government_id.passport_number"],
        [(f.get("first_seen"), f.get("value")) for f in (report or {}).get("findings", [])],
    )

    # A delta nobody can compute must gate everything rather than nothing. The safe direction is
    # the one that does not quietly disable the gate on every shallow clone in the world.
    code, report = ci(repo, "--base-ref=refs/heads/nope", "--gate-on-new", piece="privacy-datamap")
    step = next((s for s in (report or {}).get("steps", []) if s["step"] == "policy"), None)
    results.check(
        "base-ref: an unresolvable base gates everything, not nothing", code == 7, f"exit {code}"
    )
    results.check(
        "base-ref: and says why it could not compare",
        step is not None and "does not resolve" in (step.get("detail") or ""),
        (step or {}).get("detail"),
    )


def restamp_structure_digest(manifest):
    """Re-sign the first collection for its current fields, the way :scan would."""
    import hashlib

    document = ci_check.parse_manifest_text(manifest.read_text(encoding="utf-8"))
    fields = sorted(f["name"] for f in document["dataset"][0]["collections"][0]["fields"])
    digest = hashlib.sha256("\n".join(fields).encode("utf-8")).hexdigest()
    manifest.write_text(
        re.sub(
            r"structure_digest: [0-9a-f]{64}",
            f"structure_digest: {digest}",
            manifest.read_text(encoding="utf-8"),
            count=1,
        ),
        encoding="utf-8",
    )


def case_drift(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "drift")
    add_new_provider(repo)
    code, report = ci(repo)
    results.check("drift: exits 3", code == 3, f"exit {code}")
    results.check("drift: raises a drift finding", "drift" in kinds(report), kinds(report))
    drift = next((f for f in (report or {}).get("findings", []) if f["kind"] == "drift"), None)
    identities = [
        row["identity"] for row in (drift or {}).get("explanation", {}).get("new_in_repository", [])
    ]
    results.check(
        "drift: the message names the provider that arrived",
        "anthropic" in identities,
        identities,
    )
    results.check(
        "drift: the message cites where it arrived",
        any(
            (row.get("ref") or "").startswith(NEW_PROVIDER_FILE)
            for row in (drift or {}).get("explanation", {}).get("new_in_repository", [])
        ),
        (drift or {}).get("explanation"),
    )
    # And the gate stops firing the moment the record is brought back in line.
    (repo / NEW_PROVIDER_FILE).unlink()
    code, _ = ci(repo)
    results.check("drift: clears once the repository matches again", code == 0, f"exit {code}")


def case_expired(results, tmp):
    repo, manifest = build_green_repo(pathlib.Path(tmp) / "expired")
    expire_every_claim(manifest)
    code, report = ci(repo)
    results.check("expired: exits 4", code == 4, f"exit {code}")
    results.check("expired: raises an expired finding", "expired" in kinds(report), kinds(report))
    expired = next((f for f in (report or {}).get("findings", []) if f["kind"] == "expired"), None)
    results.check(
        "expired: the message names the owner who has to re-own it",
        bool((expired or {}).get("owner")),
        expired,
    )
    results.check(
        "expired: no drift is reported — the code did not change",
        "drift" not in kinds(report),
        kinds(report),
    )


def case_mixed(results, tmp):
    repo, manifest = build_green_repo(pathlib.Path(tmp) / "mixed")
    add_new_provider(repo)
    expire_every_claim(manifest)
    code, report = ci(repo)
    results.check("mixed: exits 1, not 3 or 4", code == 1, f"exit {code}")
    results.check(
        "mixed: both conditions are in the report",
        {"drift", "expired"} <= set(kinds(report)),
        kinds(report),
    )


def case_cadence(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "cadence")
    code, report = ci(repo, "--max-age-days=90")
    results.check("cadence: a 90-day cadence fails a 184-day window", code == 4, f"exit {code}")
    results.check("cadence: raises a cadence finding", "cadence" in kinds(report), kinds(report))
    code, _ = ci(repo)
    results.check("cadence: no cadence is declared by default", code == 0, f"exit {code}")


def case_warn_only(results, tmp):
    repo, manifest = build_green_repo(pathlib.Path(tmp) / "warn")
    add_new_provider(repo)
    expire_every_claim(manifest)
    code, report = ci(repo, "--mode=warn")
    results.check("warn: exits 0 with findings present", code == 0, f"exit {code}")
    results.check("warn: status says warn", (report or {}).get("status") == "warn", report)
    results.check(
        "warn: reports exactly what gate mode would fail on",
        {"drift", "expired"} <= set(kinds(report)),
        kinds(report),
    )
    code, report = ci(repo, "--fail-on=none")
    results.check("fail-on=none: exits 0", code == 0, f"exit {code}")
    results.check(
        "fail-on=none: findings are still reported",
        "drift" in kinds(report),
        kinds(report),
    )
    code, _ = ci(repo, "--fail-on=expired")
    results.check("fail-on=expired: drift alone no longer gates", code == 4, f"exit {code}")


def case_usage(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "usage")
    for label, args in (
        ("unknown option", ["--nonsense"]),
        ("bad --mode", ["--mode=maybe"]),
        ("bad --as-of", ["--as-of=last-tuesday"]),
        ("unknown --fail-on kind", ["--fail-on=vibes"]),
        ("unknown step", ["--steps=teleport"]),
        ("negative --max-age-days", ["--max-age-days=-1"]),
    ):
        code, _ = ci(repo, *args)
        results.check(f"usage: {label} exits 2", code == 2, f"exit {code}")

    completed = run([sys.executable, str(CI_CHECK), "--piece=no-such-piece", f"--repo={repo}"])
    results.check("usage: unknown piece exits 2", completed.returncode == 2, completed.stderr[:200])
    results.check(
        "usage: unknown piece lists the pieces that do exist",
        "ai-inventory" in completed.stderr,
        completed.stderr[:200],
    )


def case_tooling(results, tmp):
    """A check that could not run must never be reported as a check that passed."""
    repo = pathlib.Path(tmp) / "tooling"
    shutil.copytree(FIXTURE_REPO, repo)
    (repo / ".noru" / ".cache" / "evidence-queue.json").unlink()

    def evidence_ci(*args):
        completed = run(
            [
                sys.executable,
                str(CI_CHECK),
                "--piece=evidence-push",
                f"--repo={repo}",
                f"--as-of={AS_OF}",
                "--output=json",
                "--quiet",
                *args,
            ]
        )
        try:
            return completed.returncode, json.loads(completed.stdout)
        except json.JSONDecodeError:
            return completed.returncode, None

    code, report = evidence_ci()
    results.check("prerequisite: missing queue exits 0 by default", code == 0, f"exit {code}")
    results.check(
        "prerequisite: and says nothing was checked rather than 'passed'",
        (report or {}).get("status") == "skipped",
        report,
    )
    code, _ = evidence_ci("--on-missing-prerequisite=fail")
    results.check("prerequisite: --on-missing-prerequisite=fail exits 6", code == 6, f"exit {code}")


def case_credentials(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "creds")
    code, report = ci(repo, "--steps=all")
    push = next((s for s in (report or {}).get("steps", []) if s["step"] == "push"), None)
    results.check("no credential: exits 0", code == 0, f"exit {code}")
    results.check("no credential: the push step is skipped", (push or {}).get("status") == "skipped", push)
    results.check(
        "no credential: the reason says why, not 'error'",
        "NORU_API_KEY" in (push or {}).get("detail", "") or "diff step" in (push or {}).get("detail", ""),
        push,
    )

    # The value of a credential must not reach a step that has no business with it. Asserted on
    # the helper every step goes through, rather than by inspecting a child we cannot see into.
    probe = [sys.executable, "-c", "import os;print('NORU_API_KEY' in os.environ)"]
    os.environ["NORU_API_KEY"] = "test-value-not-a-real-key"
    try:
        without, _ = ci_check.run(probe)
        with_key, _ = ci_check.run(probe, with_api_key=True)
    finally:
        os.environ.pop("NORU_API_KEY", None)
    results.check(
        "credential: stripped from every step that does not push",
        without is not None and without.stdout.strip() == "False",
        without.stdout if without else "",
    )
    results.check(
        "credential: passed through only to the push step",
        with_key is not None and with_key.stdout.strip() == "True",
        with_key.stdout if with_key else "",
    )
    # Assembled from fragments rather than written out: a credential-shaped literal in a source
    # file is exactly what scripts/check_repo.py exists to reject, test or not.
    fake_bearer = "Authorization: Bearer " + ("a1b2c3d4" * 4)
    fake_key = "in the body: " + "noru_" + ("z9y8x7w6" * 2)
    results.check(
        "credential: redact() scrubs a bearer token",
        "<redacted>" in ci_check.redact(fake_bearer),
        ci_check.redact(fake_bearer),
    )
    results.check(
        "credential: redact() scrubs a Noru-shaped key",
        "<redacted>" in ci_check.redact(fake_key),
        ci_check.redact(fake_key),
    )


def case_expiry_tool(results, tmp):
    """The standalone expiry check keeps the house exit codes: 0 valid, 1 wrong, 2 called wrong."""
    valid = PLUGINS / "ai-inventory" / "fixtures" / "valid.ai-inventory.yml"
    completed = run([sys.executable, str(CHECK_EXPIRY), str(valid), f"--as-of={AS_OF}", "--quiet"])
    results.check("expiry tool: a current manifest exits 0", completed.returncode == 0, completed.stdout[:300])

    completed = run([sys.executable, str(CHECK_EXPIRY), str(valid), "--as-of=2030-01-01", "--quiet"])
    results.check("expiry tool: a stale manifest exits 1", completed.returncode == 1, completed.stdout[:300])

    completed = run([sys.executable, str(CHECK_EXPIRY), "--as-of=2030-01-01"])
    results.check("expiry tool: no manifest exits 2", completed.returncode == 2, completed.stderr[:200])

    # The record-level expiry a piece may also carry is compared the same way as the contract's
    # interpretation.expires_at. evidence-push's fixture is the case that exercises it.
    upload_fixture = PLUGINS / "evidence-push" / "fixtures" / "valid.evidence-push.yml"
    completed = run(
        [sys.executable, str(CHECK_EXPIRY), str(upload_fixture), "--as-of=2027-01-05", "--output=json", "--quiet"]
    )
    payload = json.loads(completed.stdout)
    fields = {f["field"] for f in payload["findings"] if f["kind"] == "expired"}
    results.check(
        "expiry tool: a record-level expiry_date is compared too",
        "expiry_date" in fields,
        sorted(fields),
    )
    results.check(
        "expiry tool: and so is interpretation.expires_at",
        "interpretation.expires_at" in fields,
        sorted(fields),
    )


def case_every_piece(results, tmp):
    """Every piece in the marketplace, not just the one this file was written against."""
    for name in sorted(p.parent.name for p in PLUGINS.glob("*/piece.json")):
        repo, _ = build_green_repo(pathlib.Path(tmp) / f"all-{name}", name)
        code, report = ci(repo, piece=name)
        results.check(f"every piece: {name} passes CI mode green", code == 0, f"exit {code}")
        results.check(
            f"every piece: {name} ran every offline step",
            report is not None and step_statuses(report) == GREEN_STEPS,
            (report or {}).get("steps"),
        )


def case_invalid_manifest(results, tmp):
    """An invalid manifest is its own exit code, and the piece's validator owns the rule.

    The manifest-declared review cadence is checked by the piece's validator against the piece's own
    bundled vocabulary — CI mode runs that validator rather than shipping a second opinion about
    what 'quarterly' means.
    """
    piece_name = "review-signoff"
    if not (PLUGINS / piece_name / "piece.json").is_file():
        return
    repo, manifest = build_green_repo(pathlib.Path(tmp) / "invalid", piece_name)
    decl = json.loads((PLUGINS / piece_name / "piece.json").read_text(encoding="utf-8"))
    invalid = next(
        (
            row
            for row in decl["validator"]["fixtures"]["invalid"]
            if "cadence" in row["path"]
        ),
        None,
    )
    if invalid is None:
        return
    # Same repository, same derived digest — only the manifest is wrong, so drift must stay silent.
    good = manifest.read_text(encoding="utf-8")
    digest = re.search(r"derived_digest: ([0-9a-f]{64})", good).group(1)
    text = (PLUGINS / piece_name / invalid["path"]).read_text(encoding="utf-8")
    text = re.sub(
        r"(\n  generated_by: [^\n]+\n)", rf"\1  derived_digest: {digest}\n", text, count=1
    )
    manifest.write_text(text, encoding="utf-8")

    code, report = ci(repo, piece=piece_name)
    results.check("invalid: exits 5", code == 5, f"exit {code}")
    results.check("invalid: raises an invalid finding", "invalid" in kinds(report), kinds(report))
    results.check(
        "invalid: no drift is reported — the repository did not change",
        "drift" not in kinds(report),
        kinds(report),
    )
    results.check(
        "invalid: the validator's own message is carried through",
        any(
            invalid["expect_message"] in f["message"]
            for f in (report or {}).get("findings", [])
            if f["kind"] == "invalid"
        ),
        [f["message"] for f in (report or {}).get("findings", []) if f["kind"] == "invalid"],
    )
    results.check(
        "invalid: the expiry step is blocked, not silently passed",
        any(s["step"] == "expiry" and s["status"] == "blocked" for s in (report or {}).get("steps", [])),
        (report or {}).get("steps"),
    )


def case_piece_agnostic(results, tmp):
    """The orchestrator must read piece.json, not know about the pieces that exist today."""
    source = CI_CHECK.read_text(encoding="utf-8")
    for name in sorted(p.parent.name for p in PLUGINS.glob("*/piece.json")):
        results.check(
            f"piece-agnostic: ci_check.py does not name '{name}'",
            name not in source,
            "a piece name in the orchestrator means the next piece needs a code change",
        )
    results.check(
        "piece-agnostic: entrypoints are read from the declaration",
        'decl["collector"]["entrypoint"]' in source and 'decl["validator"]["entrypoint"]' in source,
        "",
    )


CASES = (
    case_green,
    case_policy,
    case_coverage,
    case_base_ref,
    case_drift,
    case_expired,
    case_mixed,
    case_cadence,
    case_warn_only,
    case_usage,
    case_tooling,
    case_credentials,
    case_expiry_tool,
    case_every_piece,
    case_invalid_manifest,
    case_piece_agnostic,
)

USAGE = "usage: test_ci_mode.py [--output=json] [--quiet] [--emit-fixture=<dir>]\n"


def main(argv):
    output_json = False
    quiet = False
    emit_fixture = None
    for arg in argv:
        if arg == "--output=json":
            output_json = True
        elif arg == "--output=text":
            output_json = False
        elif arg == "--quiet":
            quiet = True
        elif arg.startswith("--emit-fixture="):
            emit_fixture = arg.split("=", 1)[1]
        elif arg in ("-h", "--help"):
            sys.stdout.write(USAGE)
            return 0
        else:
            sys.stderr.write(f"error: unknown option '{arg}'\n" + USAGE)
            return 2

    if emit_fixture:
        # Materialise the same green repository the cases use, so a workflow can point the action
        # at something the action is supposed to pass on.
        try:
            repo, manifest = build_green_repo(emit_fixture)
        except (RuntimeError, OSError) as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 2
        sys.stdout.write(f"{repo}\n" if quiet else f"wrote {manifest}\n")
        return 0

    results = Results()
    with tempfile.TemporaryDirectory(prefix="noru-ci-mode-") as tmp:
        for case in CASES:
            try:
                case(results, tmp)
            except Exception as exc:  # noqa: BLE001 - a crashing case is a failing case
                results.check(case.__name__, False, f"raised {type(exc).__name__}: {exc}")

    if output_json:
        sys.stdout.write(
            json.dumps(
                {"ok": not results.failures, "tests": results.rows},
                indent=None if quiet else 2,
                sort_keys=True,
            )
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
