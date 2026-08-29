#!/usr/bin/env python3
"""Unit tests for what the collectors actually detect.

Standard library only, no network, no install step — the same promise the collectors make.

The validators have `test_validators.py` and the contract has `contract_test.py`. Neither of them
asks the question this file exists for: **given a repository, does the scan find the right thing?**
A collector can be deterministic, offline and contract-clean while detecting nothing useful, and
every claim in a README about what a scan catches is a guess until something asserts it.

The weight here is on the `ai-inventory` Article 50 disclosure check, because that is the piece's
sharpest claim and the easiest to get quietly wrong. It is not enough to find the model call: the
finding is whether the disclosure the paragraph requires is present, and the states it reports
(present / unclear / absent) mean specific things that are asserted below one at a time.

For `audit-pack` the sharpest claim is that its sample can be redrawn. The pack tells an auditor how
to reproduce the selection, so the test below follows those written instructions independently rather
than calling the collector's own function — a sample nobody can reproduce is a list somebody typed,
and a recipe that does not work is worse than no recipe.

For `iac-scan` the sharpest claim is a negative one: the rule that finds a credential written into
configuration must never write that credential anywhere. A scanner that quotes what it matched puts
the secret into a committed file and then into a pull request, so that property is asserted directly
rather than left to the reviewer of the collector. The other assertions are about identity — a
finding is keyed on the resource, so moving a block is not a new problem and renaming one is.

Usage:
    python3 scripts/test_collectors.py [--output=json] [--quiet]
Exit codes: 0 = all tests pass, 1 = a test failed, 2 = usage / setup error.
"""
import hashlib
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
AI_INVENTORY = ROOT / "plugins" / "ai-inventory"
COLLECTOR = AI_INVENTORY / "scripts" / "collect.mjs"
VALIDATOR = AI_INVENTORY / "scripts" / "validate_manifest.py"
PRIVACY_DATAMAP = ROOT / "plugins" / "privacy-datamap"
PLUGINS = ROOT / "plugins"


class Results:
    def __init__(self):
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append({"test": name, "ok": bool(ok), "detail": str(detail)[:400]})
        return ok

    @property
    def failures(self):
        return [r for r in self.rows if not r["ok"]]


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)


def scan(tmp, name, files):
    """Write a throwaway repository, run the real collector over it, return its derived facts."""
    repo = pathlib.Path(tmp) / name
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    result = run(["node", str(COLLECTOR), f"--repo={repo}", "--output=json", "--quiet"])
    if result.returncode != 0:
        raise RuntimeError(f"collector exited {result.returncode}: {result.stderr[:300]}")
    derived = json.loads(
        (repo / ".noru" / ".cache" / "ai-inventory.derived.json").read_text(encoding="utf-8")
    )
    manifest = repo / ".noru" / "ai-inventory.yml"
    return derived, (manifest.read_text(encoding="utf-8") if manifest.is_file() else "")


CHAT_CALL = """\
import OpenAI from "openai"
const client = new OpenAI()
export async function chatRoute(question: string) {
  return client.responses.create({ model: "gpt-5-mini", input: question })
}
"""


def states_for(derived, trigger):
    return sorted(
        c["state"] for c in derived["art50_disclosure_checks"] if c["trigger"] == trigger
    )


def check_for(derived, trigger):
    """The check for one trigger, or None. Never raises: a trigger the collector stopped finding is
    a test failure with a readable message, not a stack trace out of the harness."""
    for c in derived["art50_disclosure_checks"]:
        if c["trigger"] == trigger:
            return c
    return None


def test_art50_disclosure_states(results, tmp):
    """present / unclear / absent must each mean the specific thing the schema says they mean."""
    # absent: the trigger is there and nothing anywhere in the repository discloses anything.
    derived, _ = scan(tmp, "absent", {"src/chat.ts": CHAT_CALL})
    results.check(
        "[ai-inventory] a chat surface with no disclosure anywhere reports state 'absent'",
        states_for(derived, "direct_human_interaction") == ["absent"],
        json.dumps(derived["art50_disclosure_checks"]),
    )
    check = check_for(derived, "direct_human_interaction")
    results.check(
        "[ai-inventory] an absent disclosure records where the check looked",
        check is not None and len(check["searched"]) > 0,
        json.dumps(check),
    )
    results.check(
        "[ai-inventory] an absent disclosure carries the paragraph and the duty it imposes",
        check is not None
        and check["article"] == "Article 50(1)"
        and check["required_action"] == "inform_natural_person",
        json.dumps(check),
    )

    # present: the notice is emitted from the same file that calls the model.
    derived, _ = scan(
        tmp,
        "present",
        {
            "src/chat.ts": CHAT_CALL
            + '\nexport const NOTICE = "You are chatting with an AI assistant."\n'
        },
    )
    results.check(
        "[ai-inventory] a notice in the same file as the model call reports state 'present'",
        states_for(derived, "direct_human_interaction") == ["present"],
        json.dumps(derived["art50_disclosure_checks"]),
    )

    # unclear: a notice exists next door, but nothing in the scan ties it to this call site.
    derived, _ = scan(
        tmp,
        "same-dir",
        {
            "src/chat.ts": CHAT_CALL,
            "src/notice.ts": 'export const NOTICE = "You are chatting with an AI assistant."\n',
        },
    )
    results.check(
        "[ai-inventory] a notice in the same directory reports 'unclear', not 'present'",
        states_for(derived, "direct_human_interaction") == ["unclear"],
        json.dumps(derived["art50_disclosure_checks"]),
    )

    derived, _ = scan(
        tmp,
        "far-away",
        {
            "src/chat.ts": CHAT_CALL,
            "web/legal/terms.md": "Some replies are AI-generated.\n",
        },
    )
    check = check_for(derived, "direct_human_interaction")
    results.check(
        "[ai-inventory] a notice elsewhere in the repository reports 'unclear' and cites it",
        states_for(derived, "direct_human_interaction") == ["unclear"]
        and check is not None
        and len(check["evidence_refs"]) > 0,
        json.dumps(derived["art50_disclosure_checks"]),
    )


def test_art50_marking_is_not_a_label(results, tmp):
    """Article 50(2) asks for a mark on the output; a caption in the interface is a different duty."""
    generate = (
        'import OpenAI from "openai"\n'
        "const client = new OpenAI()\n"
        "export const art = () => client.images.generate({ prompt: \"a poster\" })\n"
    )
    derived, _ = scan(
        tmp,
        "label-only",
        {
            "src/art.ts": generate + 'export const CAPTION = "This image is AI-generated."\n',
        },
    )
    check = check_for(derived, "synthetic_content_generation")
    results.check(
        "[ai-inventory] a visible 'AI-generated' caption does NOT satisfy the marking duty",
        check is not None
        and check["required_action"] == "machine_readable_marking"
        and check["state"] == "absent",
        json.dumps(check),
    )

    derived, _ = scan(
        tmp,
        "c2pa",
        {"src/art.ts": generate + 'import { signC2PA } from "c2pa"\n'},
    )
    check = check_for(derived, "synthetic_content_generation")
    results.check(
        "[ai-inventory] a content-provenance signing call does satisfy it",
        check is not None and check["state"] == "present",
        json.dumps(check),
    )

    derived, _ = scan(
        tmp,
        "weak-mark",
        {"src/art.ts": generate + "// TODO: add a watermark here one day\n"},
    )
    check = check_for(derived, "synthetic_content_generation")
    results.check(
        "[ai-inventory] a bare watermark mention is downgraded to 'unclear', not accepted",
        check is not None and check["state"] == "unclear",
        json.dumps(check),
    )


def test_multilingual_disclosure(results, tmp):
    """A Swedish notice is a notice. Concept stems, not an English phrase table."""
    derived, _ = scan(
        tmp,
        "swedish",
        {"src/chat.ts": CHAT_CALL + 'const SV = "Detta svar är AI-genererat."\n'},
    )
    results.check(
        "[ai-inventory] a non-English AI-generated notice is recognised as a disclosure",
        states_for(derived, "direct_human_interaction") == ["present"],
        json.dumps(derived["art50_disclosure_checks"]),
    )


def test_emotion_recognition_is_biometric(results, tmp):
    """Article 3(39) grounds emotion recognition in biometric data; text sentiment is not it."""
    derived, _ = scan(
        tmp,
        "sentiment",
        {
            "src/tickets.ts": 'import OpenAI from "openai"\n'
            "// score the sentiment of the ticket text\n"
            "export const sentimentScore = (text: string) => text.length\n"
        },
    )
    results.check(
        "[ai-inventory] text sentiment analysis is NOT reported as emotion recognition",
        not [c for c in derived["art50_disclosure_checks"] if c["trigger"] == "emotion_recognition"],
        json.dumps(derived["art50_disclosure_checks"]),
    )

    derived, _ = scan(
        tmp,
        "face-emotion",
        {"src/video.ts": "export const emotionDetection = (frame: FaceFrame) => frame\n"},
    )
    results.check(
        "[ai-inventory] emotion inference from a face IS reported as emotion recognition",
        [c for c in derived["art50_disclosure_checks"] if c["trigger"] == "emotion_recognition"],
        json.dumps(derived["art50_disclosure_checks"]),
    )


def test_art5_screen(results, tmp):
    """The screen has to be visibly running, and it has to stay narrow."""
    derived, _ = scan(tmp, "art5-clean", {"src/chat.ts": CHAT_CALL})
    results.check(
        "[ai-inventory] a clean repository still reports which practices were screened",
        derived["art5_signals"] == [] and len(derived["art5_screened"]) > 0,
        json.dumps(derived["art5_screened"]),
    )

    derived, _ = scan(
        tmp,
        "art5-word-only",
        {"CHANGELOG.md": "- the emotion picker now supports more emoji\n"},
    )
    results.check(
        "[ai-inventory] the word 'emotion' alone does not raise an Article 5 signal",
        derived["art5_signals"] == [],
        json.dumps(derived["art5_signals"]),
    )

    derived, _ = scan(
        tmp,
        "art5-workplace",
        {"src/hiring.ts": "export const emotionScore = (candidateVideo: Blob) => candidateVideo\n"},
    )
    signals = [
        s for s in derived["art5_signals"]
        if s["practice"] == "emotion_inference_workplace_or_education"
    ]
    results.check(
        "[ai-inventory] emotion inference in a hiring context does raise Article 5(1)(f)",
        bool(signals) and signals[0]["article"] == "Article 5(1)(f)",
        json.dumps(derived["art5_signals"]),
    )


def test_skeleton_never_asserts(results, tmp):
    """The collector proposes. It must never write a determination or an unreviewed finding."""
    derived, manifest = scan(
        tmp,
        "skeleton",
        {"src/hiring.ts": CHAT_CALL + "export const emotionScore = (candidateVideo: Blob) => 1\n"},
    )
    results.check(
        "[ai-inventory] the skeleton never writes determination: indicated",
        "determination: indicated" not in manifest,
        manifest[:300],
    )
    results.check(
        "[ai-inventory] every finding the skeleton proposes is flagged needs_review",
        manifest.count("needs_review: true")
        >= len(derived["art5_signals"]) + len(derived["art50_disclosure_checks"]),
        manifest[:300],
    )
    results.check(
        "[ai-inventory] the skeleton writes the finding categories in the enforceable-first order",
        manifest.find("prohibited_practices:")
        < manifest.find("transparency_obligations:")
        < manifest.find("role_and_risk:")
        < manifest.find("standards_alignment:"),
        manifest[manifest.find("findings:"):][:200],
    )
    # An Article 50 trigger with no disclosure check is exactly the failure the category exists to
    # prevent, so the collector must not be able to emit one even by accident.
    triggers = manifest.count("trigger: ")
    results.check(
        "[ai-inventory] no Article 50 trigger is written without its disclosure check",
        triggers == manifest.count("disclosure:"),
        f"{triggers} trigger(s), {manifest.count('disclosure:')} disclosure block(s)",
    )


def test_missing_disclosure_fixture_alerts(results):
    """The gap fixture must validate cleanly AND surface as an alert. A gap is data, not an error."""
    fixture = AI_INVENTORY / "fixtures" / "valid-art50-missing-disclosure.ai-inventory.yml"
    result = run(["python3", str(VALIDATOR), str(fixture), "--output=json", "--quiet"])
    payload = json.loads(result.stdout)
    results.check(
        "[ai-inventory] a recorded disclosure gap is valid, not a validation error",
        result.returncode == 0 and payload["ok"] is True,
        result.stdout[:300],
    )
    gaps = [a for a in payload["alerts"] if a["severity"] == "gap"]
    results.check(
        "[ai-inventory] a recorded disclosure gap is raised as an alert a CI job can fail on",
        len(gaps) == 1 and "machine_readable_marking" in gaps[0]["message"],
        json.dumps(payload["alerts"]),
    )

    stop = AI_INVENTORY / "fixtures" / "valid-art5-stop.ai-inventory.yml"
    payload = json.loads(
        run(["python3", str(VALIDATOR), str(stop), "--output=json", "--quiet"]).stdout
    )
    severities = sorted(a["severity"] for a in payload["alerts"])
    results.check(
        "[ai-inventory] an indicated practice alerts as 'stop' and an unclear one only as 'review'",
        severities == ["gap", "review", "stop"],
        json.dumps(payload["alerts"]),
    )


IAC_SCAN = ROOT / "plugins" / "iac-scan"
IAC_COLLECTOR = IAC_SCAN / "scripts" / "collect.mjs"

# A queue with nothing in it: enough for the collector to run, so a test can be about the scan and
# not about the snapshot.
EMPTY_IAC_QUEUE = {
    "fetched_at": "2026-08-27T09:14:00Z",
    "via": ["getSecurityFindings", "getOrganizationAssets", "getOrganizationRisks"],
    "source": "iac-scan",
    "open_findings": [],
    "assets": [],
    "risks": [],
}

# The line the credential rule fires on. The value is a placeholder, but the test is that NOTHING
# resembling it reaches the derived facts or the manifest — which is the property that stops this
# piece publishing the real thing when it runs on a real repository.
SECRET_LITERAL = "hunter2-placeholder-not-a-real-credential"
TF_WITH_LITERAL = f"""\
resource "aws_db_instance" "primary" {{
  engine            = "postgres"
  storage_encrypted = true
  password          = "{SECRET_LITERAL}"
}}
"""


def iac_scan_repo(tmp, name, files, queue=None):
    """Write a throwaway repository, run the real collector over it, return its derived facts."""
    repo = pathlib.Path(tmp) / f"iac-{name}"
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    cache = repo / ".noru" / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "iac-queue.json").write_text(
        json.dumps(queue or EMPTY_IAC_QUEUE, indent=2), encoding="utf-8"
    )
    result = run(["node", str(IAC_COLLECTOR), f"--repo={repo}", "--output=json", "--quiet"])
    if result.returncode != 0:
        raise RuntimeError(f"iac-scan collector exited {result.returncode}: {result.stderr[:300]}")
    derived = json.loads(
        (cache / "iac-scan.derived.json").read_text(encoding="utf-8")
    )
    manifest = repo / ".noru" / "iac-scan.yml"
    return derived, (manifest.read_text(encoding="utf-8") if manifest.is_file() else "")


def iac_checks(derived, check_id):
    return [f for f in derived["findings"] if f["check"] == check_id]


def test_iac_never_copies_the_line(results, tmp):
    """The rule that fires on a credential must not put that credential anywhere it can be read."""
    derived, manifest = iac_scan_repo(tmp, "literal", {"infra/db.tf": TF_WITH_LITERAL})
    hits = iac_checks(derived, "terraform-credential-literal-in-source")
    results.check(
        "[iac-scan] a credential written into Terraform is found",
        len(hits) == 1 and hits[0]["resource"] == "aws_db_instance.primary",
        json.dumps(derived["findings"]),
    )
    results.check(
        "[iac-scan] the matched value appears in NEITHER the derived facts NOR the manifest",
        SECRET_LITERAL not in json.dumps(derived) and SECRET_LITERAL not in manifest,
        "the collector copied what it matched — that is how a scanner commits a secret",
    )
    results.check(
        "[iac-scan] what it writes instead is a citation the reader can open",
        hits[0]["ref"] == "infra/db.tf:4",
        json.dumps(hits),
    )


def test_iac_identity_survives_a_move(results, tmp):
    """A finding is keyed on the resource, not the line: moving a block is not a new problem."""
    moved = "# a comment added at the top\n# and another\n" + TF_WITH_LITERAL
    first, _ = iac_scan_repo(tmp, "move-before", {"infra/db.tf": TF_WITH_LITERAL})
    second, _ = iac_scan_repo(tmp, "move-after", {"infra/db.tf": moved})
    before = iac_checks(first, "terraform-credential-literal-in-source")[0]
    after = iac_checks(second, "terraform-credential-literal-in-source")[0]
    results.check(
        "[iac-scan] moving a resource down the file keeps the finding's identity",
        before["key"] == after["key"] and before["ref"] != after["ref"],
        f"{before['key']} vs {after['key']}, {before['ref']} vs {after['ref']}",
    )

    renamed, _ = iac_scan_repo(
        tmp,
        "renamed",
        {"infra/db.tf": TF_WITH_LITERAL.replace('"primary"', '"replica"')},
    )
    results.check(
        "[iac-scan] the same rule on a different resource is a different finding",
        iac_checks(renamed, "terraform-credential-literal-in-source")[0]["key"] != before["key"],
        iac_checks(renamed, "terraform-credential-literal-in-source")[0]["key"],
    )


def test_iac_absence_is_detectable(results, tmp):
    """The interesting half of an infrastructure review is what a block does NOT say."""
    unencrypted = 'resource "aws_db_instance" "primary" {\n  engine = "postgres"\n}\n'
    derived, _ = iac_scan_repo(tmp, "unencrypted", {"infra/db.tf": unencrypted})
    results.check(
        "[iac-scan] a database block declaring no encryption is reported",
        len(iac_checks(derived, "terraform-managed-database-storage-unencrypted")) == 1,
        json.dumps(derived["findings"]),
    )

    encrypted = (
        'resource "aws_db_instance" "primary" {\n'
        '  engine            = "postgres"\n'
        "  storage_encrypted = true\n"
        "}\n"
    )
    derived, _ = iac_scan_repo(tmp, "encrypted", {"infra/db.tf": encrypted})
    results.check(
        "[iac-scan] and the same block with encryption declared is NOT reported",
        iac_checks(derived, "terraform-managed-database-storage-unencrypted") == [],
        json.dumps(derived["findings"]),
    )


def test_iac_classification(results, tmp):
    """A rule only ever runs against the kind of document it is written for."""
    files = {
        "deploy/app.yaml": (
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n"
            "  template:\n    spec:\n      hostNetwork: true\n"
        ),
        ".github/workflows/ci.yml": (
            "name: ci\non: [push]\npermissions: write-all\njobs:\n  test:\n"
            "    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
        ),
        "docs/notes.yaml": "privileged: true\npermissions: write-all\n",
    }
    derived, _ = iac_scan_repo(tmp, "classify", files)
    kinds = {row["file"]: row["technology"] for row in derived["configuration_files"]}
    results.check(
        "[iac-scan] a Kubernetes manifest and a workflow are classified, a plain YAML file is not",
        kinds == {"deploy/app.yaml": "kubernetes", ".github/workflows/ci.yml": "github_actions"},
        json.dumps(kinds),
    )
    results.check(
        "[iac-scan] no rule fires inside the unclassified file",
        all(f["file"] != "docs/notes.yaml" for f in derived["findings"]),
        json.dumps([f["file"] for f in derived["findings"]]),
    )
    results.check(
        "[iac-scan] a workflow step pinned to a mutable reference is reported",
        len(iac_checks(derived, "github-actions-third-party-action-unpinned")) == 1,
        json.dumps([f["check"] for f in derived["findings"]]),
    )

    pinned = files[".github/workflows/ci.yml"].replace(
        "actions/checkout@v4", "actions/checkout@" + "0" * 40
    )
    derived, _ = iac_scan_repo(tmp, "pinned", {".github/workflows/ci.yml": pinned})
    results.check(
        "[iac-scan] and a step pinned to a full commit hash is not",
        iac_checks(derived, "github-actions-third-party-action-unpinned") == [],
        json.dumps(derived["findings"]),
    )


def test_iac_reports_what_stopped_reproducing(results, tmp):
    """The half of the queue only Noru knows: a finding that is open and no longer fires."""
    queue = dict(
        EMPTY_IAC_QUEUE,
        open_findings=[
            {
                "external_id": "example/app:terraform-object-storage-public-acl.0123456789ab",
                "check_name": "terraform-object-storage-public-acl",
                "title": "Object storage bucket is granted a public access control list",
                "severity": "high",
                "status": "open",
                "category": "configuration",
            }
        ],
    )
    derived, _ = iac_scan_repo(tmp, "stale", {"infra/db.tf": TF_WITH_LITERAL}, queue=queue)
    results.check(
        "[iac-scan] an open finding no rule reproduced is named in the scan output",
        derived["queue_no_longer_reproducing"]
        == ["example/app:terraform-object-storage-public-acl.0123456789ab"],
        json.dumps(derived["queue_no_longer_reproducing"]),
    )


def test_iac_skeleton_never_decides(results, tmp):
    """The collector proposes. Severity, reality and ownership are the reviewer's."""
    _, manifest = iac_scan_repo(tmp, "skeleton", {"infra/db.tf": TF_WITH_LITERAL})
    # Counted at the finding's own indentation: the header comment mentions the flag too, and a
    # test that matches the documentation instead of the data is a test that proves nothing.
    results.check(
        "[iac-scan] every finding the skeleton proposes is flagged needs_review",
        manifest.count("\n    needs_review: true") == manifest.count("\n  - key: ") > 0,
        f'{manifest.count(chr(10) + "    needs_review: true")} flagged, '
        f'{manifest.count(chr(10) + "  - key: ")} finding(s)',
    )
    results.check(
        "[iac-scan] the skeleton never invents an owner",
        "owner: TODO@example.com" in manifest,
        manifest[:200],
    )


def test_iac_every_status_has_an_expiry_horizon(results):
    """A status with no horizon would make the expiry check pass without checking anything."""
    vocab = json.loads(
        (IAC_SCAN / "references" / "vocabulary.json").read_text(encoding="utf-8")
    )
    missing = sorted(set(vocab["finding_status"]) - set(vocab["status_horizon_days"]))
    results.check(
        "[iac-scan] every finding status has an expiry horizon in the bundled vocabulary",
        missing == [],
        f"no horizon for {missing}",
    )


AUDIT_PACK = ROOT / "plugins" / "audit-pack"
AP_COLLECTOR = AUDIT_PACK / "scripts" / "collect.mjs"
AP_VALIDATOR = AUDIT_PACK / "scripts" / "validate_manifest.py"

AP_QUEUE = {
    "fetched_at": "2026-08-27T09:14:00Z",
    "via": [
        "getOrganizationFrameworks",
        "getOrganizationControls",
        "getControlContext",
        "getEvidenceForControl",
        "getEvidenceItems",
    ],
    "framework_id": "zz_framework",
    "framework_name": "Example framework",
    "window": {"from": "2026-01-01", "to": "2026-06-30"},
    "controls": [
        {
            "control_id": "zz-01",
            "name": "Example control",
            "status": "implemented",
            "coverage": 50,
            "testing_guidance_available": True,
            "expected_evidence_items": [
                {"id": "E-ZZ-01", "title": "Example Records", "type": "record"},
                {"id": "E-ZZ-02", "title": "Example Procedure", "type": "procedure"},
            ],
            "linked_evidence": [
                {
                    "evidence_id": "EXAMPLE-EVIDENCE-ID",
                    "title": "Example procedure",
                    "status": "valid",
                    "type": "procedure",
                    "evidence_item_id": "E-ZZ-02",
                }
            ],
        }
    ],
}


def audit_pack_repo(tmp, name, files, queue=None):
    """Write a throwaway repository, run the real collector over it, return its derived facts."""
    repo = pathlib.Path(tmp) / f"ap-{name}"
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    cache = repo / ".noru" / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "audit-queue.json").write_text(
        json.dumps(queue or AP_QUEUE, indent=2), encoding="utf-8"
    )
    result = run(["node", str(AP_COLLECTOR), f"--repo={repo}", "--output=json", "--quiet"])
    if result.returncode != 0:
        raise RuntimeError(f"audit-pack collector exited {result.returncode}: {result.stderr[:300]}")
    derived = json.loads((cache / "audit-pack.derived.json").read_text(encoding="utf-8"))
    return repo, derived, json.loads(result.stdout)


def population_csv(rows):
    lines = ["reference,opened"]
    for i in range(1, rows + 1):
        lines.append(f"REF-{i:04d},2026-01-{(i % 28) + 1:02d}")
    return "\n".join(lines) + "\n"


def test_audit_pack_sample_is_redrawable(results, tmp):
    """The pack tells an auditor how to redraw the sample. Follow those instructions and check.

    This is the assertion the whole piece rests on: a sample nobody can reproduce is a list somebody
    typed. The recipe is written into the workpaper and into the README, so it gets a test that runs
    the recipe independently rather than calling the collector's own function.
    """
    body = population_csv(60)
    repo, derived, _ = audit_pack_repo(
        tmp, "sample", {".noru/artifacts/changes.csv": body}
    )
    artifact = next(a for a in derived["artifacts"] if a["file"].endswith("changes.csv"))
    population = artifact["population"]

    results.check(
        "[audit-pack] a delimited export is recognised as a population and counted",
        population is not None and population["size"] == 60,
        json.dumps(artifact),
    )

    # Independently: the seed is the file's own digest, and the order is sha256(seed|reference).
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    keys = [f"REF-{i:04d}" for i in range(1, 61)]
    expected_seed = digest[:32]
    redrawn = sorted(
        keys, key=lambda k: hashlib.sha256(f"{expected_seed}|{k}".encode("utf-8")).hexdigest()
    )[: population["suggested_sample_size"]]

    results.check(
        "[audit-pack] the seed is the population file's own digest, so it needs no random source",
        population["seed"] == expected_seed,
        f"{population['seed']} vs {expected_seed}",
    )
    results.check(
        "[audit-pack] REDRAWING the sample from the documented recipe reproduces it exactly",
        population["suggested_sample"] == redrawn,
        f"collector {population['suggested_sample'][:4]} vs redrawn {redrawn[:4]}",
    )
    results.check(
        "[audit-pack] the sample is not simply the first rows of the file",
        population["suggested_sample"] != keys[: population["suggested_sample_size"]],
        json.dumps(population["suggested_sample"][:5]),
    )

    # And the floor the validator enforces has to be the floor the collector proposes against.
    _, derived_small, _ = audit_pack_repo(
        tmp, "sample-small", {".noru/artifacts/changes.csv": population_csv(3)}
    )
    small = next(
        a for a in derived_small["artifacts"] if a["file"].endswith("changes.csv")
    )["population"]
    results.check(
        "[audit-pack] a population smaller than the floor is tested in full",
        small["minimum_sample"] == 3 and small["suggested_sample_size"] == 3,
        json.dumps(small),
    )

    _, derived_big, _ = audit_pack_repo(
        tmp, "sample-big", {".noru/artifacts/changes.csv": population_csv(600)}
    )
    big = next(
        a for a in derived_big["artifacts"] if a["file"].endswith("changes.csv")
    )["population"]
    results.check(
        "[audit-pack] a large population raises the floor above the default sample size",
        big["minimum_sample"] == 45 and big["suggested_sample_size"] == 45,
        json.dumps({k: v for k, v in big.items() if k != "suggested_sample"}),
    )


def test_audit_pack_gap_analysis(results, tmp):
    """The gap is the difference between what the framework expects and what is actually linked."""
    _, derived, _ = audit_pack_repo(tmp, "gaps", {"README.md": "# fixture\n"})
    control = derived["controls"][0]
    results.check(
        "[audit-pack] an expectation with nothing linked to it is reported as unmet",
        control["unmet_evidence_items"] == ["E-ZZ-01"],
        json.dumps(control),
    )
    results.check(
        "[audit-pack] an expectation that IS linked is not reported as unmet",
        "E-ZZ-02" not in control["unmet_evidence_items"],
        json.dumps(control),
    )

    expired_queue = json.loads(json.dumps(AP_QUEUE))
    expired_queue["controls"][0]["linked_evidence"][0]["status"] = "expired"
    _, derived, _ = audit_pack_repo(
        tmp, "expired", {"README.md": "# fixture\n"}, queue=expired_queue
    )
    results.check(
        "[audit-pack] a linked record that expired is surfaced separately from an unmet expectation",
        derived["controls"][0]["expired_evidence"] == ["EXAMPLE-EVIDENCE-ID"],
        json.dumps(derived["controls"][0]),
    )


def test_audit_pack_assembles_upstream_manifests(results, tmp):
    """A pack says which reviewed inputs produced what is in Noru, not only what the register says."""
    repo, derived, _ = audit_pack_repo(
        tmp,
        "upstream",
        {
            ".noru/review-signoff.yml": "version: 0.1.0\npiece: review-signoff\nreviews: []\n",
            ".noru/notes.yml": "just: a file\n",
        },
    )
    pieces = [row["piece"] for row in derived["upstream_manifests"]]
    results.check(
        "[audit-pack] another piece's committed manifest is digested into the pack",
        pieces == ["review-signoff"],
        json.dumps(derived["upstream_manifests"]),
    )
    # The pack's own manifest is written into the same directory; digesting it would make the
    # derived facts depend on their own output.
    results.check(
        "[audit-pack] the pack's own manifest is not one of its inputs",
        all(row["file"] != ".noru/audit-pack.yml" for row in derived["upstream_manifests"]),
        json.dumps(derived["upstream_manifests"]),
    )


def test_audit_pack_renders_only_a_validated_pack(results, tmp):
    """A pack built from an unreviewed manifest would look exactly like a real one."""
    repo, _, summary = audit_pack_repo(
        tmp, "render", {".noru/artifacts/changes.csv": population_csv(40)}
    )
    index = repo / ".noru" / "audit-pack" / "index.md"
    results.check(
        "[audit-pack] a scan with no validated manifest renders the scope and says so",
        summary["bundle"] == [".noru/audit-pack/index.md"]
        and "has not been reviewed yet" in index.read_text(encoding="utf-8"),
        json.dumps(summary["bundle"]),
    )
    results.check(
        "[audit-pack] and it writes no workpaper for a conclusion nobody drew",
        not (repo / ".noru" / "audit-pack" / "workpapers").exists(),
        "workpapers were rendered from an unvalidated manifest",
    )

    # Now do it properly: the piece's own valid fixture, re-stamped with this repository's digest.
    decl = json.loads((AUDIT_PACK / "piece.json").read_text(encoding="utf-8"))
    fixture = (AUDIT_PACK / decl["validator"]["fixtures"]["valid"][0]).read_text(encoding="utf-8")
    digest = summary["derived_digest"]
    manifest = repo / ".noru" / "audit-pack.yml"
    manifest.write_text(
        re.sub(
            r"(\n  generated_by: [^\n]+\n)", rf"\1  derived_digest: {digest}\n", fixture, count=1
        ),
        encoding="utf-8",
    )
    parsed = repo / ".noru" / ".cache" / "audit-pack.parsed.json"
    validated = run(
        ["python3", str(AP_VALIDATOR), str(manifest), f"--emit-parsed={parsed}", "--quiet"]
    )
    if not results.check(
        "[audit-pack] the fixture manifest validates against this repository",
        validated.returncode == 0,
        validated.stdout[:300],
    ):
        return
    rendered = run(["node", str(AP_COLLECTOR), f"--repo={repo}", "--output=json", "--quiet"])
    bundle = json.loads(rendered.stdout)["bundle"]
    results.check(
        "[audit-pack] a validated manifest renders a workpaper per control and a sampling worksheet",
        ".noru/audit-pack/workpapers/change-management.md" in bundle
        and ".noru/audit-pack/sampling/change-management.csv" in bundle,
        json.dumps(bundle),
    )
    workpaper = (
        repo / ".noru" / "audit-pack" / "workpapers" / "change-management.md"
    ).read_text(encoding="utf-8")
    results.check(
        "[audit-pack] the workpaper tells the reader how to redraw the sample",
        "Redraw it" in workpaper and "Seed:" in workpaper,
        workpaper[:200],
    )
    # The framework's testing procedure is Noru's to serve. A pack that copied it would vendor
    # catalogue content and go stale the moment the framework moved.
    results.check(
        "[audit-pack] the pack records that a procedure exists, and never its text",
        "Testing procedure available from Noru: yes" in workpaper,
        workpaper[:200],
    )


def test_audit_pack_every_conclusion_has_an_assurance_horizon(results):
    """A conclusion with no horizon would make the expiry check pass without checking anything."""
    vocab = json.loads(
        (AUDIT_PACK / "references" / "vocabulary.json").read_text(encoding="utf-8")
    )
    missing = sorted(set(vocab["conclusion"]) - set(vocab["assurance_days"]))
    results.check(
        "[audit-pack] every conclusion has an assurance horizon in the bundled vocabulary",
        missing == [],
        f"no horizon for {missing}",
    )


# --------------------------------------------------------------------------------------------- #
# privacy-datamap. The weight here is on the split the piece rests on: structure is *derived* and
# meaning is *judged*. A parser that reports a column exists is standing behind a fact; a table that
# says `email` means user.contact.email is standing behind a lookup. Anything outside both is raised
# for a human rather than guessed, because a confidently wrong data category is worse than a gap —
# the gap gets reviewed and the wrong answer gets signed.


def datamap_repo(tmp, name, files):
    repo = pathlib.Path(tmp) / name
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    collector = PRIVACY_DATAMAP / "scripts" / "collect.mjs"
    result = run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"])
    if result.returncode != 0:
        raise RuntimeError(f"collector exited {result.returncode}: {result.stderr[:300]}")
    derived = json.loads(
        (repo / ".noru" / ".cache" / "privacy-datamap.derived.json").read_text(encoding="utf-8")
    )
    return derived, repo


def fields_of(derived, collection_name):
    for dataset in derived["datasets"]:
        for collection in dataset["collections"]:
            if collection["name"] == collection_name:
                return {f["name"]: f for f in collection["fields"]}
    return {}


SQL_FIXTURE = """CREATE TABLE accounts (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    weird_column  TEXT,
    created_at    TIMESTAMPTZ,
    CONSTRAINT accounts_email_lower CHECK (email = lower(email))
);
"""


def test_datamap_reads_every_declared_format(results, tmp):
    """Each parser the README claims must actually find its collection, or the claim is marketing."""
    derived, _ = datamap_repo(
        tmp,
        "formats",
        {
            "db/schema.sql": SQL_FIXTURE,
            "store/schema.prisma": "model Subscriber {\n  id Int @id\n  emailAddress String\n}\n",
            "store/models.py": (
                "from django.db import models\n\n\n"
                "class Patient(models.Model):\n"
                "    full_name = models.CharField(max_length=200)\n"
                "    date_of_birth = models.DateField()\n"
            ),
            "api/contact.proto": (
                'syntax = "proto3";\n\nmessage ContactCard {\n  string email = 1;\n}\n'
            ),
            "api/schema.graphql": "type Viewer {\n  username: String!\n  ipAddress: String\n}\n",
        },
    )
    found = {d["source_kind"] for d in derived["datasets"]}
    for kind in ("sql_ddl", "prisma", "python_orm", "protobuf", "graphql"):
        results.check(
            f"[privacy-datamap] the {kind} parser finds a collection",
            kind in found,
            f"found: {sorted(found)}",
        )
    results.check(
        "[privacy-datamap] a SQL constraint clause is not read as a column",
        "CONSTRAINT" not in fields_of(derived, "accounts")
        and "accounts_email_lower" not in fields_of(derived, "accounts"),
        sorted(fields_of(derived, "accounts")),
    )


def test_datamap_classifies_only_what_it_knows(results, tmp):
    derived, _ = datamap_repo(tmp, "classify", {"db/schema.sql": SQL_FIXTURE})
    fields = fields_of(derived, "accounts")

    results.check(
        "[privacy-datamap] an exact match is classified",
        fields.get("email", {}).get("data_categories") == ["user.contact.email"],
        fields.get("email"),
    )
    # The whole determinism argument rests on this: a name the table does not know is RAISED, never
    # inferred. A collector that guessed would be non-deterministic and confidently wrong at once.
    results.check(
        "[privacy-datamap] an unrecognised name is raised, not guessed",
        fields.get("weird_column", {}).get("needs_review") is True
        and fields.get("weird_column", {}).get("data_categories") == [],
        fields.get("weird_column"),
    )
    # Operational columns are not personal data and must not become review noise; a reviewer who has
    # to dismiss `id` and `created_at` on every table stops reading the list.
    results.check(
        "[privacy-datamap] an operational column is not review noise",
        fields.get("created_at", {}).get("needs_review") is not True
        and fields.get("id", {}).get("needs_review") is not True,
        [fields.get("id"), fields.get("created_at")],
    )


def test_datamap_normalises_naming_styles(results, tmp):
    """The same column in camelCase, snake_case and PascalCase is the same column."""
    derived, _ = datamap_repo(
        tmp,
        "naming",
        {
            "store/schema.prisma": (
                "model A {\n  emailAddress String\n  last_login_ip String\n  DateOfBirth String\n}\n"
            )
        },
    )
    fields = fields_of(derived, "A")
    for name, expected in (
        ("emailAddress", "user.contact.email"),
        ("last_login_ip", "user.device.ip_address"),
        ("DateOfBirth", "user.demographic.date_of_birth"),
    ):
        results.check(
            f"[privacy-datamap] {name} normalises to {expected}",
            fields.get(name, {}).get("data_categories") == [expected],
            fields.get(name),
        )


def test_datamap_citations_point_at_the_real_line(results, tmp):
    """A citation that points at the wrong line is worse than no citation: it looks checkable."""
    derived, repo = datamap_repo(tmp, "refs", {"db/schema.sql": SQL_FIXTURE})
    fields = fields_of(derived, "accounts")
    ok = True
    detail = []
    for name, field in fields.items():
        path, _, line = field["ref"].rpartition(":")
        source = (repo / path).read_text(encoding="utf-8").split("\n")[int(line) - 1]
        if name not in source:
            ok = False
            detail.append(f"{name} cited at {field['ref']} but that line reads {source.strip()!r}")
    results.check(
        "[privacy-datamap] every field citation resolves to the line the field is on", ok, detail
    )


def test_datamap_surfaces_special_category_data(results, tmp):
    """Article 9 data is the highest-risk thing in a data map and must never be a line to scroll past."""
    derived, _ = datamap_repo(
        tmp,
        "special",
        {
            "store/models.py": (
                "from django.db import models\n\n\n"
                "class Patient(models.Model):\n"
                "    medical_record_number = models.CharField(max_length=64)\n"
                "    email = models.EmailField()\n"
            )
        },
    )
    fields = fields_of(derived, "Patient")
    results.check(
        "[privacy-datamap] special-category data is flagged on the field",
        fields.get("medical_record_number", {}).get("special_category") is True,
        fields.get("medical_record_number"),
    )
    results.check(
        "[privacy-datamap] ordinary personal data is not flagged special",
        fields.get("email", {}).get("special_category") is not True,
        fields.get("email"),
    )
    results.check(
        "[privacy-datamap] special-category citations are collected for the report",
        derived["special_category_refs"] == ["store/models.py:5"],
        derived["special_category_refs"],
    )


def test_datamap_never_overwrites_a_reviewed_manifest(results, tmp):
    """Regenerating over someone's signed classification is the worst thing a collector can do,
    because it looks like it worked."""
    derived, repo = datamap_repo(tmp, "no-clobber", {"db/schema.sql": SQL_FIXTURE})
    manifest = repo / ".noru" / "privacy-datamap.yml"
    reviewed = manifest.read_text(encoding="utf-8") + "\n# a human edited this\n"
    manifest.write_text(reviewed, encoding="utf-8")

    (repo / "db" / "schema.sql").write_text(
        SQL_FIXTURE.replace("weird_column  TEXT,", "weird_column  TEXT,\n    phone_number  TEXT,"),
        encoding="utf-8",
    )
    collector = PRIVACY_DATAMAP / "scripts" / "collect.mjs"
    again = run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"])
    results.check(
        "[privacy-datamap] a changed schema reports drift instead of rewriting the manifest",
        again.returncode == 0 and json.loads(again.stdout)["drift"] is True,
        again.stdout[:200],
    )
    results.check(
        "[privacy-datamap] the reviewed manifest is left exactly as the human left it",
        manifest.read_text(encoding="utf-8") == reviewed,
        "the collector overwrote a manifest a person had edited",
    )
    checked = run(["node", str(collector), f"--repo={repo}", "--check", "--output=json", "--quiet"])
    results.check(
        "[privacy-datamap] --check exits 1 on that drift so CI fails",
        checked.returncode == 1,
        f"exit {checked.returncode}",
    )



def test_datamap_digest_agrees_across_languages(results, tmp):
    """collect.mjs stamps structure_digest; validate_manifest.py recomputes it. Two implementations
    of one hash in two languages is exactly the thing that silently diverges, and the failure would
    be invisible: every collection would read as "changed shape since it was signed" forever, and
    the obvious fix — re-running :scan — would not help."""
    _, repo = datamap_repo(
        tmp,
        "digest-agreement",
        {
            "db/schema.sql": SQL_FIXTURE,
            "api/contact.proto": (
                'syntax = "proto3";\n\nmessage ContactCard {\n'
                "  string email = 1;\n  string phone_number = 2;\n}\n"
            ),
        },
    )
    validator = PRIVACY_DATAMAP / "scripts" / "validate_manifest.py"
    result = run(["python3", str(validator), str(repo / ".noru" / "privacy-datamap.yml")])
    # The skeleton is deliberately invalid — it is full of needs_review — so the assertion is not
    # "it validates", it is "the digest is never the thing it complains about".
    complaints = [
        line for line in result.stdout.splitlines() if "structure_digest" in line
    ]
    results.check(
        "[privacy-datamap] the JS and Python structure digests agree",
        not complaints,
        "; ".join(complaints)[:300],
    )



def test_datamap_render_is_gated_and_matches_the_push(results, tmp):
    """`.fides/datamap.yml` is a deliverable somebody hands over. Two things have to hold.

    It must never be rendered from a manifest that no longer describes the repository — a Fides file
    that looks authoritative and documents a schema that has moved on is worse than no file, because
    nobody re-reads one that already exists.

    And it must be the same content Noru receives. If the export and the payload were built
    separately they would drift, and the failure would be silent and horrible: a data map handed to
    an auditor saying something different from the one in the compliance record.
    """
    piece = PRIVACY_DATAMAP
    _, repo = datamap_repo(tmp, "render", {"db/schema.sql": SQL_FIXTURE})

    # No validated manifest yet — the ordinary state of a first scan, and not an error.
    results.check(
        "[privacy-datamap] nothing is rendered without a validated manifest",
        not (repo / ".fides" / "datamap.yml").exists(),
        "an unvalidated manifest produced a Fides export",
    )

    fixture = (piece / "fixtures" / "valid.privacy-datamap.yml").read_text(encoding="utf-8")
    manifest = repo / ".noru" / "privacy-datamap.yml"
    collector = piece / "scripts" / "collect.mjs"
    digest = json.loads(
        run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"]).stdout
    )["derived_digest"]
    manifest.write_text(
        re.sub(r"derived_digest: [0-9a-f]{64}", f"derived_digest: {digest}", fixture),
        encoding="utf-8",
    )
    validator = piece / "scripts" / "validate_manifest.py"
    parsed = repo / ".noru" / ".cache" / "privacy-datamap.parsed.json"
    validated = run(["python3", str(validator), str(manifest), f"--emit-parsed={parsed}", "--quiet"])
    if not results.check(
        "[privacy-datamap] the fixture validates against this repository", validated.returncode == 0,
        validated.stdout[:300],
    ):
        return

    scan = run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"])
    rendered = json.loads(scan.stdout).get("rendered")
    results.check(
        "[privacy-datamap] a validated manifest renders the Fides export",
        rendered == ".fides/datamap.yml" and (repo / ".fides" / "datamap.yml").is_file(),
        scan.stdout[:200],
    )

    # A stale validated manifest must stop rendering, not render something out of date.
    (repo / "db" / "schema.sql").write_text(
        SQL_FIXTURE.replace("weird_column  TEXT,", "weird_column  TEXT,\n    phone_number  TEXT,"),
        encoding="utf-8",
    )
    (repo / ".fides" / "datamap.yml").unlink()
    run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"])
    results.check(
        "[privacy-datamap] a manifest that no longer matches the repository renders nothing",
        not (repo / ".fides" / "datamap.yml").exists(),
        "a stale manifest was rendered as if it were current",
    )

    # Put it back, render again, and compare the export against what :push would send.
    (repo / "db" / "schema.sql").write_text(SQL_FIXTURE, encoding="utf-8")
    run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"])
    (repo / ".noru" / ".cache" / "noru-state.json").write_text(
        json.dumps({"fetched_at": "2026-08-27T09:00:00Z"}), encoding="utf-8"
    )
    plan = run(
        ["node", str(piece / "scripts" / "diff.mjs"), f"--repo={repo}", "--output=json", "--quiet"]
    )
    if not results.check("[privacy-datamap] diff succeeds", plan.returncode == 0, plan.stderr[:300]):
        return
    sent = json.loads(plan.stdout)["operations"][0]["arguments"]["manifest"]

    spec = importlib.util.spec_from_file_location("pdm_validator", validator)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    exported, _ = module.load_yaml((repo / ".fides" / "datamap.yml").read_text(encoding="utf-8"))
    results.check(
        "[privacy-datamap] the Fides export is exactly what :push sends",
        exported == sent,
        f"export {json.dumps(exported)[:180]} vs sent {json.dumps(sent)[:180]}",
    )



def test_digest_ignores_the_collectors_own_version(results, tmp):
    """The derived digest must answer "has the repository changed?" and nothing else.

    `generated_by` used to be inside the hash, which made a plugin upgrade indistinguishable from a
    schema change: every committed manifest reported drift on the next run and CI mode failed with
    exit 3 for repositories where nothing had moved. It is asserted here for EVERY piece rather than
    the one it was found in, because seven collectors carry a byte-identical digestOf() and fixing
    one is not fixing the property.
    """
    probe = pathlib.Path(tmp) / "digest-probe.mjs"
    for piece in sorted(PLUGINS.glob("*/piece.json")):
        name = piece.parent.name
        collector = piece.parent / "scripts" / "collect.mjs"
        probe.write_text(
            "import { digestOf } from %r;\n"
            "const facts = { piece: 'x', generated_by: 'x@0.1.0', findings: [1, 2] };\n"
            "const bumped = { ...facts, generated_by: 'x@9.9.9' };\n"
            "const absent = { piece: 'x', findings: [1, 2] };\n"
            "console.log(JSON.stringify({\n"
            "  same: digestOf(facts) === digestOf(bumped),\n"
            "  absent_same: digestOf(facts) === digestOf(absent),\n"
            "}));\n" % str(collector),
            encoding="utf-8",
        )
        result = run(["node", str(probe)])
        if not results.check(
            f"[{name}] digestOf is callable", result.returncode == 0, result.stderr[:200]
        ):
            continue
        out = json.loads(result.stdout)
        results.check(
            f"[{name}] the digest ignores the collector's own version",
            out["same"],
            "bumping generated_by changed the digest, so upgrading the plugin will report drift "
            "in every repository that has already run :scan",
        )
        # And it must be ignored, not merely stable — a digest that changed when the field was
        # absent would still break the moment anything stopped emitting it.
        results.check(
            f"[{name}] an absent generated_by hashes the same as a present one",
            out["absent_same"],
            "the field is excluded inconsistently",
        )



def test_change_control_rules_agree_across_languages(results, tmp):
    """The segregation rules are implemented twice, and two implementations of one rule drift.

    collect.mjs computes the violations so the skeleton can propose them; validate_manifest.py
    recomputes them so a manifest with an unowned one is refused. Neither can trust the other's
    output — the validator must work on a manifest whose derived facts are long gone — so both
    exist, and this is the check that stops them disagreeing.

    The cases below are the ones where the two could plausibly diverge: a name that differs only in
    case or whitespace, an approval that is not an approval, an agent whose only reviewer is its
    operator, and the clean change that must produce nothing at all.
    """
    cases = [
        (
            "clean: independent approver and deployer",
            {
                "authored_by": "a@example.com", "author_kind": "human",
                "approvals": [{"by": "b@example.com", "state": "approved"}],
                "merged_by": "b@example.com", "deployed_by": "b@example.com",
                "bypass": {"used": False},
            },
        ),
        (
            "self-approved, self-merged, self-deployed",
            {
                "authored_by": "a@example.com", "author_kind": "human",
                "approvals": [{"by": "a@example.com", "state": "approved"}],
                "merged_by": "a@example.com", "deployed_by": "a@example.com",
                "bypass": {"used": False},
            },
        ),
        (
            "the same person spelled differently",
            {
                "authored_by": "A@Example.com", "author_kind": "human",
                "approvals": [{"by": " a@example.com ", "state": "approved"}],
                "bypass": {"used": False},
            },
        ),
        (
            "a comment is not an approval",
            {
                "authored_by": "a@example.com", "author_kind": "human",
                "approvals": [{"by": "b@example.com", "state": "commented"}],
                "bypass": {"used": False},
            },
        ),
        (
            "agent reviewed only by its operator",
            {
                "authored_by": "bot@example.com", "author_kind": "agent",
                "agent_operator": "a@example.com",
                "approvals": [{"by": "a@example.com", "state": "approved"}],
                "bypass": {"used": False},
            },
        ),
        (
            "agent reviewed by an independent human",
            {
                "authored_by": "bot@example.com", "author_kind": "agent",
                "agent_operator": "a@example.com",
                "approvals": [{"by": "b@example.com", "state": "approved"}],
                "bypass": {"used": False},
            },
        ),
        (
            "a bypass with an otherwise clean change",
            {
                "authored_by": "a@example.com", "author_kind": "human",
                "approvals": [{"by": "b@example.com", "state": "approved"}],
                "bypass": {"used": True, "kind": "force_push"},
            },
        ),
    ]

    piece = PLUGINS / "change-control"
    script = (
        "import { violationsOf } from "
        f"{json.dumps(str(piece / 'scripts' / 'collect.mjs'))};\n"
        # With `node -e`, argv[0] is the executable and the first extra argument is argv[1].
        "const cases = JSON.parse(process.argv[1]);\n"
        "console.log(JSON.stringify(cases.map((c) => violationsOf(c).map((v) => v.rule))));\n"
    )
    payload = json.dumps([change for _, change in cases])
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", payload],
        capture_output=True, text=True, check=False,
    )
    if not results.check(
        "[change-control] the JS rules run", completed.returncode == 0, completed.stderr[:300]
    ):
        return
    js_rules = json.loads(completed.stdout)

    sys.path.insert(0, str(piece / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "cc_validator", piece / "scripts" / "validate_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for (label, change), js in zip(cases, js_rules):
        py = [rule for rule, _ in module.violations_of(change)]
        results.check(
            f"[change-control] JS and Python agree — {label}",
            py == js,
            f"node says {js}, python says {py}",
        )

    # And the one that matters most: a clean change produces nothing at all. A rule set that fires
    # on everything is as useless as one that fires on nothing, and easier to ship by accident.
    results.check(
        "[change-control] a clean change raises no violation at all",
        js_rules[0] == [] and not module.violations_of(cases[0][1]),
        json.dumps(js_rules[0]),
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
            sys.stdout.write("usage: test_collectors.py [--output=json] [--quiet]\n")
            return 0
        else:
            sys.stderr.write(f"error: unknown option '{arg}'\n")
            return 2

    if not COLLECTOR.is_file():
        sys.stderr.write(f"error: collector missing at {COLLECTOR}\n")
        return 2

    results = Results()
    try:
        with tempfile.TemporaryDirectory(prefix="noru-collectors-") as tmp:
            test_art50_disclosure_states(results, tmp)
            test_art50_marking_is_not_a_label(results, tmp)
            test_multilingual_disclosure(results, tmp)
            test_emotion_recognition_is_biometric(results, tmp)
            test_art5_screen(results, tmp)
            test_skeleton_never_asserts(results, tmp)
            test_datamap_reads_every_declared_format(results, tmp)
            test_datamap_classifies_only_what_it_knows(results, tmp)
            test_datamap_normalises_naming_styles(results, tmp)
            test_datamap_citations_point_at_the_real_line(results, tmp)
            test_datamap_surfaces_special_category_data(results, tmp)
            test_datamap_never_overwrites_a_reviewed_manifest(results, tmp)
            test_datamap_digest_agrees_across_languages(results, tmp)
            test_datamap_render_is_gated_and_matches_the_push(results, tmp)
            test_digest_ignores_the_collectors_own_version(results, tmp)
            test_change_control_rules_agree_across_languages(results, tmp)
            test_iac_never_copies_the_line(results, tmp)
            test_iac_identity_survives_a_move(results, tmp)
            test_iac_absence_is_detectable(results, tmp)
            test_iac_classification(results, tmp)
            test_iac_reports_what_stopped_reproducing(results, tmp)
            test_iac_skeleton_never_decides(results, tmp)
            test_audit_pack_sample_is_redrawable(results, tmp)
            test_audit_pack_gap_analysis(results, tmp)
            test_audit_pack_assembles_upstream_manifests(results, tmp)
            test_audit_pack_renders_only_a_validated_pack(results, tmp)
        test_missing_disclosure_fixture_alerts(results)
        test_iac_every_status_has_an_expiry_horizon(results)
        test_audit_pack_every_conclusion_has_an_assurance_horizon(results)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"error: {exc}\n")
        return 2

    ok = not results.failures
    if output_json:
        sys.stdout.write(
            json.dumps({"ok": ok, "total": len(results.rows), "results": results.rows},
                       indent=None if quiet else 2)
            + "\n"
        )
        return 0 if ok else 1

    for row in results.rows:
        if not row["ok"]:
            print(f"  FAIL  {row['test']}")
            if row["detail"]:
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
