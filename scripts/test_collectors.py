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
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jsonschema_mini import validate as validate_json_schema  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
AI_INVENTORY = ROOT / "plugins" / "ai-inventory"
COLLECTOR = AI_INVENTORY / "scripts" / "collect.mjs"
VALIDATOR = AI_INVENTORY / "scripts" / "validate_manifest.py"
PRIVACY_DATAMAP = ROOT / "plugins" / "privacy-datamap"
PLUGINS = ROOT / "plugins"
TEMPLATE_COLLECTOR = ROOT / "scripts" / "templates" / "collect.mjs.tmpl"


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


def write_files(repo, files):
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return repo


def git_repo(repo, files, then=None):
    """A repository as CI would check it out, plus whatever else the developer keeps in the tree.

    `files` is staged; `then` is written afterwards, so it is untracked without being ignored. No
    commit is made — `git ls-files` reads the index, so staging is enough and this needs no
    committer identity, which a CI runner may not have configured.
    """
    write_files(repo, files)
    run(["git", "-C", str(repo), "init", "-q"])
    run(["git", "-C", str(repo), "add", "-A"])
    if then:
        write_files(repo, then)
    return repo


def ai_scan(repo):
    """Run the ai-inventory collector over a directory that already exists.

    Returns (summary, derived facts, manifest text).
    """
    result = run(["node", str(COLLECTOR), f"--repo={repo}", "--output=json", "--quiet"])
    if result.returncode != 0:
        raise RuntimeError(f"collector exited {result.returncode}: {result.stderr[:300]}")
    derived = json.loads(
        (repo / ".noru" / ".cache" / "ai-inventory.derived.json").read_text(encoding="utf-8")
    )
    manifest = repo / ".noru" / "ai-inventory.yml"
    return (
        json.loads(result.stdout),
        derived,
        manifest.read_text(encoding="utf-8") if manifest.is_file() else "",
    )


def scan(tmp, name, files):
    """Write a throwaway repository, run the real collector over it, return its derived facts."""
    _, derived, manifest = ai_scan(write_files(pathlib.Path(tmp) / name, files))
    return derived, manifest


CHAT_CALL = """\
import OpenAI from "openai"
const client = new OpenAI()
export async function chatRoute(question: string) {
  return client.responses.create({ model: "gpt-5-mini", input: question })
}
"""

# A second provider, so that a file being left out of the scan shows up as a provider that is not
# in the inventory rather than as a count nobody can read.
DRAFT_CALL = """\
import Anthropic from "@anthropic-ai/sdk"
const client = new Anthropic()
export const reply = (question: string) =>
  client.messages.create({ model: "claude-opus-5", max_tokens: 256, input: question })
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


def iac_scan(repo, queue=None):
    """Run the iac-scan collector over a directory that already exists.

    Returns (summary, derived facts, manifest text). The queue lands under `.noru/`, which is in
    SKIP_DIRS, so writing it never changes what the scan enumerates.
    """
    cache = repo / ".noru" / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "iac-queue.json").write_text(
        json.dumps(queue or EMPTY_IAC_QUEUE, indent=2), encoding="utf-8"
    )
    result = run(["node", str(IAC_COLLECTOR), f"--repo={repo}", "--output=json", "--quiet"])
    if result.returncode != 0:
        raise RuntimeError(f"iac-scan collector exited {result.returncode}: {result.stderr[:300]}")
    derived = json.loads((cache / "iac-scan.derived.json").read_text(encoding="utf-8"))
    manifest = repo / ".noru" / "iac-scan.yml"
    return (
        json.loads(result.stdout),
        derived,
        manifest.read_text(encoding="utf-8") if manifest.is_file() else "",
    )


def iac_scan_repo(tmp, name, files, queue=None):
    """Write a throwaway repository, run the real collector over it, return its derived facts."""
    repo = write_files(pathlib.Path(tmp) / f"iac-{name}", files)
    _, derived, manifest = iac_scan(repo, queue)
    return derived, manifest


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


def test_iac_scans_what_ci_checks_out(results, tmp):
    """A scan on a working tree and a scan in CI have to describe the same configuration.

    A finding is keyed on the check and the file it fired against, so a gitignored copy of the
    repository — a worktree, a scratch checkout, an unpacked archive — does not merely double a
    count. It opens a *distinct* finding against a resource at a path that is not in the
    repository, which this piece then pushes to Noru, and which nobody can fix by editing anything:
    the file it cites is on one machine only. CI scans an `actions/checkout`, so the two scans
    disagree permanently and the committed manifest can match one of them or the other.
    """
    tracked = {".gitignore": "worktrees/\n", "infra/db.tf": TF_WITH_LITERAL}
    repo = git_repo(
        pathlib.Path(tmp) / "iac-worktree",
        {**tracked, "worktrees/agent-1/infra/db.tf": TF_WITH_LITERAL},
        # Untracked and not ignored: CI cannot see it either, so scanning it would put the same
        # disagreement back in a smaller form. Stage it and it is scanned.
        then={"infra/draft.tf": TF_WITH_LITERAL},
    )
    summary, derived, _ = iac_scan(repo)

    refs = sorted(f["ref"] for f in derived["findings"])
    results.check(
        "[iac-scan] a gitignored copy of the configuration opens no second finding",
        refs == ["infra/db.tf:4"],
        refs,
    )
    results.check(
        "[iac-scan] and configuration that has not been staged yet is not scanned",
        sorted(f["file"] for f in derived["configuration_files"]) == ["infra/db.tf"],
        [f["file"] for f in derived["configuration_files"]],
    )
    results.check(
        "[iac-scan] the derived facts record that the file list came from git",
        derived["coverage"].get("enumerated_by") == "git",
        derived["coverage"].get("enumerated_by"),
    )
    results.check(
        "[iac-scan] and the scan summary reports it too, where a reader will meet it",
        summary.get("enumerated_by") == "git",
        summary.get("enumerated_by"),
    )

    # The same commit as CI sees it: tracked files, no .git directory. Identical digest, or the
    # drift gate is comparing two different repositories and reporting the difference as a
    # configuration change that nobody can make go away.
    checkout = write_files(pathlib.Path(tmp) / "iac-ci-checkout", tracked)
    ci_summary, ci_derived, _ = iac_scan(checkout)
    results.check(
        "[iac-scan] a working tree and a checkout of the same files agree on the digest",
        ci_summary["derived_digest"] == summary["derived_digest"],
        f"{summary['derived_digest'][:12]} vs {ci_summary['derived_digest'][:12]}",
    )
    # With no git to ask, reading the disk is the honest fallback — an exported tarball is a
    # legitimate thing to scan. It is a different question though, so it is reported, not assumed.
    results.check(
        "[iac-scan] a directory that is not a work tree falls back to reading the disk",
        ci_derived["coverage"].get("enumerated_by") == "walk"
        and [f["ref"] for f in ci_derived["findings"]] == ["infra/db.tf:4"],
        ci_derived["coverage"].get("enumerated_by"),
    )
    results.check(
        "[iac-scan] enumerated_by is outside the digest, so it can never read as drift",
        derived["coverage"]["enumerated_by"] != ci_derived["coverage"]["enumerated_by"],
        "the two scans enumerated the same way, so this test asserted nothing",
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


def datamap_scan(repo):
    """Run the collector over a directory that already exists. Returns (summary, derived facts)."""
    collector = PRIVACY_DATAMAP / "scripts" / "collect.mjs"
    result = run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"])
    if result.returncode != 0:
        raise RuntimeError(f"collector exited {result.returncode}: {result.stderr[:300]}")
    derived = json.loads(
        (repo / ".noru" / ".cache" / "privacy-datamap.derived.json").read_text(encoding="utf-8")
    )
    return json.loads(result.stdout), derived


def datamap_repo(tmp, name, files):
    repo = write_files(pathlib.Path(tmp) / name, files)
    return datamap_scan(repo)[1], repo


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


def test_ai_inventory_scans_what_ci_checks_out(results, tmp):
    """A scan on a working tree and a scan in CI have to describe the same repository.

    The same defect privacy-datamap had, and it lands harder here. CI scans an `actions/checkout` —
    tracked files, and nothing else. A developer scans a working tree, which may also hold
    worktrees, scratch checkouts and unpacked archives, each a full copy of the repository as far as
    a directory walk can tell. Every such copy contributes a provider ref, a model id and an
    Article 50 trigger site cited to a path that is not in the repository, so the inventory names
    call sites nobody can open — and the drift between the two scans is then unresolvable, because
    the committed manifest can match one environment or the other and never both.
    """
    # `.gitignore` is tracked, so it belongs in both repositories — a file present on one side only
    # would make the digest comparison below pass or fail for the wrong reason.
    tracked = {".gitignore": "worktrees/\n", "src/chat.ts": CHAT_CALL}
    repo = git_repo(
        pathlib.Path(tmp) / "ai-worktree",
        {**tracked, "worktrees/agent-1/src/chat.ts": CHAT_CALL},
        # Untracked and not ignored: the one case where leaving out a file the developer can see is
        # the right answer, because CI cannot see it either. Stage it and it is in the inventory.
        then={"src/draft.ts": DRAFT_CALL},
    )
    summary, derived, _ = ai_scan(repo)

    refs = sorted(
        ref
        for group in ("providers", "frameworks", "models", "vector_stores")
        for row in derived[group]
        for ref in row["refs"]
    )
    results.check(
        "[ai-inventory] a gitignored copy of the repository contributes no call site",
        not any(ref.startswith("worktrees/") for ref in refs),
        refs,
    )
    results.check(
        "[ai-inventory] and a model call that has not been staged yet is not inventoried",
        [p["key"] for p in derived["providers"]] == ["openai"],
        [p["key"] for p in derived["providers"]],
    )
    results.check(
        "[ai-inventory] the derived facts record that the file list came from git",
        derived["coverage"].get("enumerated_by") == "git",
        derived["coverage"].get("enumerated_by"),
    )
    results.check(
        "[ai-inventory] and the scan summary reports it too, where a reader will meet it",
        summary.get("enumerated_by") == "git",
        summary.get("enumerated_by"),
    )

    # The same commit as CI sees it: tracked files, no .git directory. Identical digest, or the
    # drift gate is comparing two different repositories and reporting the difference as a change
    # to the AI estate that nobody can make go away.
    checkout = write_files(pathlib.Path(tmp) / "ai-ci-checkout", tracked)
    ci_summary, ci_derived, _ = ai_scan(checkout)
    results.check(
        "[ai-inventory] a working tree and a checkout of the same files agree on the digest",
        ci_summary["derived_digest"] == summary["derived_digest"],
        f"{summary['derived_digest'][:12]} vs {ci_summary['derived_digest'][:12]}",
    )
    # With no git to ask, reading the disk is the honest fallback — an exported tarball is a
    # legitimate thing to scan. It is a different question though, so it is reported, not assumed.
    results.check(
        "[ai-inventory] a directory that is not a work tree falls back to reading the disk",
        ci_derived["coverage"].get("enumerated_by") == "walk"
        and [p["key"] for p in ci_derived["providers"]] == ["openai"],
        ci_derived["coverage"].get("enumerated_by"),
    )
    # The point of putting it under `coverage`: how the files were found is not a fact about the
    # repository, so the two answers above must not be a difference the drift gate can see.
    results.check(
        "[ai-inventory] enumerated_by is outside the digest, so it can never read as drift",
        derived["coverage"]["enumerated_by"] != ci_derived["coverage"]["enumerated_by"],
        "the two scans enumerated the same way, so this test asserted nothing",
    )


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
    found = {kind for dataset in derived["datasets"] for kind in dataset.get("source_kinds", [])}
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


DRIZZLE_FIXTURE = """import { pgTable, text, uuid } from "drizzle-orm/pg-core"

export const members = pgTable("members", {
  id: uuid("id").primaryKey(),
  email: text("email").notNull(), // comments after commas must not hide the next field
  apiSecret: text("api_secret").notNull(),
  /* comments between properties are trivia, not part of the next declaration */
  displayName: text("display_name"),
  metadata: jsonb("metadata").$type<Record<string, unknown>>(),
})
"""


def test_datamap_scans_what_ci_checks_out(results, tmp):
    """A scan on a working tree and a scan in CI have to describe the same repository.

    CI scans an `actions/checkout`: tracked files, and nothing else. A developer scans a working
    tree, which may also hold worktrees, scratch checkouts and unpacked archives — each one a full
    copy of the repository as far as a directory walk can tell. Walking those turns every copy into
    its own dataset, keyed off a path that is not in the repository at all, and the drift between
    the two scans is then unresolvable: the committed manifest can match one environment or the
    other and never both.
    """
    # `.gitignore` is tracked, so it belongs in both repositories — the digest counts files, and a
    # file present on one side only would make this test pass or fail for the wrong reason.
    tracked = {".gitignore": "worktrees/\n", "db/schema.sql": SQL_FIXTURE}
    repo = write_files(
        pathlib.Path(tmp) / "worktree",
        {**tracked, "worktrees/agent-1/db/schema.sql": SQL_FIXTURE},
    )
    # No commit: `git ls-files` reads the index, so staging is enough and this needs no identity.
    run(["git", "-C", str(repo), "init", "-q"])
    run(["git", "-C", str(repo), "add", "-A"])
    # Written after the add, so it is untracked and not ignored — the one case where excluding a
    # file the developer can see is the right answer, because CI cannot see it either.
    write_files(repo, {"db/unstaged.sql": SQL_FIXTURE})
    summary, derived = datamap_scan(repo)

    names = [d["name"] for d in derived["datasets"]]
    results.check(
        "[privacy-datamap] a gitignored copy of a schema is not a second dataset",
        names == ["db"],
        names,
    )
    results.check(
        "[privacy-datamap] and a schema that has not been staged yet is not one either",
        "db/unstaged.sql" not in names,
        names,
    )
    results.check(
        "[privacy-datamap] the derived facts record that the file list came from git",
        derived["coverage"].get("enumerated_by") == "git",
        derived["coverage"].get("enumerated_by"),
    )

    # The same commit as CI sees it: tracked files, no .git directory. Identical digest, or the
    # drift gate is comparing two different repositories and reporting the difference as a schema
    # change that nobody can make go away.
    checkout = write_files(pathlib.Path(tmp) / "ci-checkout", tracked)
    ci_summary, ci_derived = datamap_scan(checkout)
    results.check(
        "[privacy-datamap] a working tree and a checkout of the same files agree on the digest",
        ci_summary["derived_digest"] == summary["derived_digest"],
        f"{summary['derived_digest'][:12]} vs {ci_summary['derived_digest'][:12]}",
    )
    # With no git to ask, reading the disk is the honest fallback — an exported tarball is a
    # legitimate thing to scan. It is a different question though, so it is reported, not assumed.
    results.check(
        "[privacy-datamap] a directory that is not a work tree falls back to reading the disk",
        ci_derived["coverage"].get("enumerated_by") == "walk" and len(ci_derived["datasets"]) == 1,
        ci_derived["coverage"].get("enumerated_by"),
    )


def test_datamap_parses_drizzle_without_execution(results, tmp):
    """Drizzle's nested builder calls are parsed as text; repository code is never imported."""
    derived, repo = datamap_repo(tmp, "drizzle", {"src/schema.ts": DRIZZLE_FIXTURE})
    candidates = derived["coverage"]["unparsed_candidates"]
    results.check(
        "[privacy-datamap] every direct Drizzle field is parsed despite neighbouring comments",
        set(fields_of(derived, "members"))
        == {"id", "email", "api_secret", "display_name", "metadata"},
        fields_of(derived, "members"),
    )
    results.check(
        "[privacy-datamap] Drizzle is no longer reported as unsupported coverage",
        candidates == [] and derived["coverage"]["parsed_by_kind"].get("drizzle") == 1,
        derived["coverage"],
    )
    imported, _ = datamap_repo(
        tmp,
        "drizzle-import",
        {"src/helpers.ts": 'import { pgTable } from "drizzle-orm/pg-core"\nexport { pgTable }\n'},
    )
    results.check(
        "[privacy-datamap] importing the symbol without declaring a table is not a dataset",
        imported["datasets"] == [],
        imported["datasets"],
    )
    quoted, _ = datamap_repo(
        tmp,
        "drizzle-quoted-example",
        {"src/example.ts": 'const example = \'pgTable("not_a_table", { id: text("id") })\'\n'},
    )
    results.check(
        "[privacy-datamap] Drizzle syntax inside a string is not schema coverage",
        quoted["datasets"] == [] and quoted["coverage"]["unparsed_candidates"] == [],
        quoted["coverage"],
    )
    dynamic, _ = datamap_repo(
        tmp,
        "drizzle-dynamic",
        {
            "src/schema.ts": (
                'const tableName = process.env.TABLE_NAME\n'
                'export const members = pgTable(tableName, { email: text("email") })\n'
            )
        },
    )
    results.check(
        "[privacy-datamap] a dynamic Drizzle table name is coverage instead of executed or guessed",
        dynamic["datasets"] == []
        and [item["format"] for item in dynamic["coverage"]["unparsed_candidates"]]
        == ["drizzle"],
        dynamic["coverage"],
    )
    spread, _ = datamap_repo(
        tmp,
        "drizzle-spread",
        {
            "src/schema.ts": (
                'const shared = { id: uuid("id") }\n'
                'export const members = pgTable("members", {\n'
                '  ...shared,\n'
                '  email: text("email"),\n'
                '})\n'
            )
        },
    )
    results.check(
        "[privacy-datamap] a partially static Drizzle map is reported instead of silently complete",
        set(fields_of(spread, "members")) == {"email"}
        and [item["format"] for item in spread["coverage"]["unparsed_candidates"]]
        == ["drizzle"],
        spread["coverage"],
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
    # A creation timestamp is context-free bookkeeping. A generic identifier is not: it can name a
    # person, an account or a non-person row, so the proposal stage must inspect its context.
    results.check(
        "[privacy-datamap] only context-free operational columns bypass proposal review",
        fields.get("created_at", {}).get("needs_review") is not True
        and fields.get("id", {}).get("needs_review") is True,
        [fields.get("id"), fields.get("created_at")],
    )
    contextual, _ = datamap_repo(
        tmp,
        "context-dependent-operational-names",
        {
            "db/schema.sql": (
                "CREATE TABLE records (\n"
                "  uuid UUID,\n"
                "  status TEXT,\n"
                "  enabled BOOLEAN,\n"
                "  is_active BOOLEAN\n"
                ");\n"
            )
        },
    )
    contextual_fields = fields_of(contextual, "records")
    results.check(
        "[privacy-datamap] context-sensitive identifiers and state enter proposal_required",
        all(
            contextual_fields.get(name, {}).get("needs_review") is True
            for name in ("uuid", "status", "enabled", "is_active")
        ),
        contextual_fields,
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


def accepted_datamap_text(digest):
    names = ["id", "email", "password_hash", "weird_column", "created_at"]
    structure = hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()
    return f"""version: 0.8.1
piece: privacy-datamap
source:
  slug: fixture/privacy-map
  commit_sha: 4f3c1a9e77b2d5c8a10e6b4f2d9c3a71e5b80d64
  branch: main
  generated_by: privacy-datamap@0.8.1
  derived_digest: {digest}
dataset:
  - fides_key: db
    name: db
    collections:
      - name: accounts
        refs:
          - "db/schema.sql:1"
        structure_digest: {structure}
        interpretation:
          owner: Dana Okafor
          decided_at: "2026-08-20"
          expires_at: "2027-08-20"
          rationale: Reviewed the account schema and its application semantics.
        fields:
          - name: id
            data_categories: []
            refs: ["db/schema.sql:2"]
          - name: email
            data_categories: [user.contact.email]
            refs: ["db/schema.sql:3"]
          - name: password_hash
            data_categories: [user.authorization.password]
            refs: ["db/schema.sql:4"]
          - name: weird_column
            data_categories: []
            refs: ["db/schema.sql:5"]
          - name: created_at
            data_categories: []
            refs: ["db/schema.sql:6"]
system:
  - fides_key: repository
    name: repository
    system_type: Application
    dataset_references: [db]
    privacy_declarations:
      - name: Operate customer accounts
        data_use: essential.service
        data_subjects: [customer]
        data_categories: [user.contact.email, user.authorization.password]
        refs: ["db/schema.sql:1"]
        interpretation:
          owner: Dana Okafor
          decided_at: "2026-08-20"
          expires_at: "2027-08-20"
          rationale: Account data is used to provide authentication and service access.
"""


def accepted_compact_datamap_text(digest):
    names = ["id", "email", "password_hash", "weird_column", "created_at"]
    structure = hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()
    return f"""version: 0.8.1
piece: privacy-datamap
source:
  slug: fixture/privacy-map
  commit_sha: 4f3c1a9e77b2d5c8a10e6b4f2d9c3a71e5b80d64
  branch: main
  generated_by: privacy-datamap@0.8.1
  derived_digest: {digest}
dataset:
  - fides_key: db
    name: db
    collections:
      - name: accounts
        refs: ["db/schema.sql:1"]
        structure_digest: {structure}
        interpretation:
          owner: Dana Okafor
          decided_at: "2026-08-20"
          expires_at: "2027-08-20"
          rationale: Reviewed the account schema and its application semantics.
        non_personal_fields: [created_at, id, weird_column]
        fields:
          - name: email
            data_categories: [user.contact.email]
            refs: ["db/schema.sql:3"]
          - name: password_hash
            data_categories: [user.authorization.password]
            refs: ["db/schema.sql:4"]
system:
  - fides_key: repository
    name: repository
    system_type: Application
    dataset_references: [db]
    privacy_declarations:
      - name: Operate customer accounts
        data_use: essential.service
        data_subjects: [customer]
        data_categories: [user.contact.email, user.authorization.password]
        refs: ["db/schema.sql:1"]
        interpretation:
          owner: Dana Okafor
          decided_at: "2026-08-20"
          expires_at: "2027-08-20"
          rationale: Account data is used to provide authentication and service access.
"""


def load_datamap_yaml(path):
    validator = PRIVACY_DATAMAP / "scripts" / "validate_manifest.py"
    spec = importlib.util.spec_from_file_location("privacy_datamap_validator", validator)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_yaml(path.read_text(encoding="utf-8"))[0]


def test_datamap_compacts_non_personal_review_state(results, tmp):
    """Review files stay small while derived facts and the lock retain every observed field."""
    workflow = (PRIVACY_DATAMAP / "commands" / "scan.md").read_text(encoding="utf-8")
    results.check(
        "[privacy-datamap] agent workflow requires contextual grouped proposal review",
        all(
            phrase in workflow
            for phrase in (
                "Before asking the user",
                "proposed personal",
                "proposed non-personal",
                "genuine ambiguities",
                "special_category",
                "accountable owner",
                "collection-level acceptance",
            )
        ),
        workflow[:400],
    )
    summary, derived = datamap_scan(
        write_files(pathlib.Path(tmp) / "compact-non-personal", {"db/schema.sql": SQL_FIXTURE})
    )
    repo = pathlib.Path(summary["repo"])
    manifest = repo / ".noru" / "privacy-datamap.yml"
    skeleton = load_datamap_yaml(manifest)
    collection = skeleton["dataset"][0]["collections"][0]
    review_names = [field["name"] for field in collection["fields"]]
    results.check(
        "[privacy-datamap] new fields remain visible until the collection review is accepted",
        collection["non_personal_fields"] == []
        and review_names == ["created_at", "email", "id", "password_hash", "weird_column"],
        collection,
    )
    observed_names = set(fields_of(derived, "accounts"))
    represented_names = set(review_names) | set(collection["non_personal_fields"])
    results.check(
        "[privacy-datamap] compact manifests still represent the complete observed structure",
        observed_names == represented_names,
        {"observed": sorted(observed_names), "represented": sorted(represented_names)},
    )
    full_digest = hashlib.sha256("\n".join(sorted(observed_names)).encode("utf-8")).hexdigest()
    results.check(
        "[privacy-datamap] structure digest covers verbose and compact field names",
        collection["structure_digest"] == full_digest,
        collection["structure_digest"],
    )

    manifest.write_text(accepted_compact_datamap_text(summary["derived_digest"]), encoding="utf-8")
    validator = PRIVACY_DATAMAP / "scripts" / "validate_manifest.py"
    parsed = repo / ".noru" / ".cache" / "privacy-datamap.parsed.json"
    validated = run(["python3", str(validator), str(manifest), f"--emit-parsed={parsed}", "--quiet"])
    results.check(
        "[privacy-datamap] validator accepts the union of review and compact fields",
        validated.returncode == 0,
        validated.stdout,
    )

    missing = accepted_compact_datamap_text(summary["derived_digest"]).replace(
        "non_personal_fields: [created_at, id, weird_column]",
        "non_personal_fields: [created_at, id]",
    )
    manifest.write_text(missing, encoding="utf-8")
    incomplete = run(["python3", str(validator), str(manifest), "--quiet"])
    results.check(
        "[privacy-datamap] validator rejects an observed field omitted from both representations",
        incomplete.returncode == 1 and "observed field(s) missing" in incomplete.stdout,
        incomplete.stdout,
    )
    manifest.write_text(accepted_compact_datamap_text(summary["derived_digest"]), encoding="utf-8")
    run(["python3", str(validator), str(manifest), f"--emit-parsed={parsed}", "--quiet"])

    reconcile = PRIVACY_DATAMAP / "scripts" / "reconcile.py"
    sealed = run(["python3", str(reconcile), f"--repo={repo}", "--seal", "--output=json", "--quiet"])
    lock = json.loads((repo / ".noru" / "privacy-datamap.lock.json").read_text(encoding="utf-8"))
    results.check(
        "[privacy-datamap] accepted lock retains every field hidden by compaction",
        sealed.returncode == 0
        and {entity.rsplit("/", 1)[-1] for entity in lock["entities"]} == observed_names
        and lock["entities"]["db/accounts/created_at"]["shape"] == "timestamptz"
        and lock["entities"]["db/accounts/created_at"]["refs"] == ["db/schema.sql:6"],
        lock.get("entities"),
    )
    unchanged = run(["python3", str(reconcile), f"--repo={repo}", "--output=json", "--quiet"])
    unchanged_payload = json.loads(unchanged.stdout)
    candidate = load_datamap_yaml(repo / ".noru" / ".cache" / "privacy-datamap.candidate.yml")
    candidate_collection = candidate["dataset"][0]["collections"][0]
    results.check(
        "[privacy-datamap] unchanged compact non-personal decisions carry forward",
        unchanged_payload["counts"]["proposal_required"] == 0
        and candidate_collection["non_personal_fields"] == ["created_at", "id", "weird_column"],
        unchanged_payload,
    )
    results.check(
        "[privacy-datamap] scan and validation emit no external-write call file",
        not (repo / ".noru" / ".cache" / "privacy-datamap.calls.json").exists(),
        "scan or validation prepared an MCP write",
    )

    (repo / "db" / "schema.sql").write_text(
        SQL_FIXTURE.replace("weird_column  TEXT", "weird_column  JSON"), encoding="utf-8"
    )
    datamap_scan(repo)
    changed = run(["python3", str(reconcile), f"--repo={repo}", "--output=json", "--quiet"])
    changed_payload = json.loads(changed.stdout)
    changed_candidate = load_datamap_yaml(
        repo / ".noru" / ".cache" / "privacy-datamap.candidate.yml"
    )["dataset"][0]["collections"][0]
    results.check(
        "[privacy-datamap] a changed accepted non-personal field returns to review",
        [row["field"] for row in changed_payload["proposal_required"]] == ["weird_column"]
        and "weird_column" not in changed_candidate["non_personal_fields"]
        and any(
            field["name"] == "weird_column" and field.get("needs_review") is True
            for field in changed_candidate["fields"]
        ),
        changed_payload,
    )


def test_datamap_fides_projection_is_privacy_only(results, tmp):
    probe = pathlib.Path(tmp) / "privacy-fides-projection.mjs"
    fides = PRIVACY_DATAMAP / "scripts" / "lib" / "fides.mjs"
    probe.write_text(
        f"""import {{ toFideslang }} from {json.dumps(str(fides))};
const base = {{
  dataset: [
    {{ fides_key: "primary", name: "primary", collections: [{{
      name: "profiles", non_personal_fields: ["row_version"], fields: [{{
        name: "profile", data_categories: [], refs: ["schema.ts:1"], fields: [
          {{ name: "email", data_categories: ["user.contact.email"], refs: ["schema.ts:2"] }},
          {{ name: "timezone", data_categories: [], refs: ["schema.ts:3"] }},
        ],
      }}],
    }}] }},
    {{ fides_key: "logs", name: "logs", collections: [{{
      name: "events", non_personal_fields: ["created_at"], fields: [],
    }}] }},
  ],
  system: [{{
    fides_key: "app", name: "app", dataset_references: ["primary", "logs", "absent"],
    privacy_declarations: [{{
      name: "accounts", data_use: "essential.service", data_subjects: ["customer"],
      data_categories: ["user.contact.email"], refs: ["service.ts:1"],
    }}],
  }}],
}};
let blocked = false;
try {{ toFideslang({{ dataset: [{{ fides_key: "x", collections: [{{ fields: [{{ name: "x", needs_review: true }}] }}] }}] }}); }}
catch {{ blocked = true; }}
console.log(JSON.stringify({{ projected: toFideslang(base), blocked }}));
""",
        encoding="utf-8",
    )
    executed = run(["node", str(probe)])
    if not results.check(
        "[privacy-datamap] privacy-only Fides projection runs", executed.returncode == 0,
        executed.stderr,
    ):
        return
    payload = json.loads(executed.stdout)
    projected = payload["projected"]
    fields = projected["dataset"][0]["collections"][0]["fields"]
    results.check(
        "[privacy-datamap] Fides keeps categorized nested fields and their parent only",
        len(projected["dataset"]) == 1
        and [field["name"] for field in fields] == ["profile"]
        and [field["name"] for field in fields[0]["fields"]] == ["email"]
        and "non_personal_fields" not in json.dumps(projected),
        projected,
    )
    results.check(
        "[privacy-datamap] empty collections and datasets are removed and references repaired",
        projected["system"][0]["dataset_references"] == ["primary"],
        projected["system"],
    )
    results.check(
        "[privacy-datamap] unresolved review state cannot reach Fides projection",
        payload["blocked"] is True,
        payload,
    )


def test_datamap_enrichment_gate(results, tmp):
    repo = write_files(pathlib.Path(tmp) / "privacy-enrichment", {"db/schema.sql": SQL_FIXTURE})
    datamap_scan(repo)
    scripts = PRIVACY_DATAMAP / "scripts"
    setup = run(["python3", str(scripts / "reconcile.py"), f"--repo={repo}", "--output=json"])
    results.check("[privacy-datamap] reconciliation reports structure collected",
                  setup.returncode == 0 and json.loads(setup.stdout)["status"] == "structure collected")
    cache = repo / ".noru" / ".cache"
    proposals = cache / "privacy-datamap.proposals.json"
    command = ["python3", str(scripts / "review.py"), f"--repo={repo}", "--output=json"]
    incomplete = run(command)
    results.check("[privacy-datamap] skeleton is enrichment incomplete",
                  incomplete.returncode == 1 and json.loads(incomplete.stdout)["status"] == "enrichment incomplete")
    document = json.loads(proposals.read_text())
    document["decisions"] = {"classify": {"decision_summary": "Establish the use of the unresolved account fields."}}
    for row in document["proposals"]:
        row.update(decision_id="classify", proposal_kind="ambiguous", rationale="Schema establishes storage but no service code establishes use.",
                   confidence="low", unresolved_question="Which business process uses this field?",
                   decision_impact="Determines whether the value identifies a person.",
                   resolution_needed="Name the process and explain how it uses this value.",
                   analysis={"schema": "Read the SQL definition.", "relationships": "No foreign key in this fixture.",
                             "service_or_serialization": "No runtime consumer exists in this fixture."})
    for row in document["system_proposals"]:
        row.update(rationale="Only a repository fallback boundary is available.", refs=["db/schema.sql:1"],
                   confidence="low", processing_activities=[{
                       "activity_id": "unknown", "purpose": "Processing purpose not established by this fixture",
                       "decision_summary": "Establish the account processing purpose.",
                       "decision_impact": "Determines the purpose and subject classification.",
                       "rationale": "No runtime consumer is present.", "refs": ["db/schema.sql:1"], "confidence": "low",
                       "proposed_data_uses": [], "proposed_data_subjects": [], "dataset_references": [],
                       "unresolved_question": "What process and subjects does the runtime serve?",
                       "relationship_rationale": "Schema exists but runtime access is not established.",
                       "business_context_question": "Confirm actual processing intent.",
                       "resolution_needed": "Name each business process, its subjects and accessed stores."}])
    document["store_investigation"].update(search_scope="Inspected all tracked SQL and runtime files.",
        rationale="The fixture has only a SQL schema and no other persistent integration.",
        refs=["db/schema.sql:1"], confidence="high")
    protected = [repo / ".noru" / "privacy-datamap.yml", cache / "privacy-datamap.candidate.yml",
                 repo / ".noru" / "privacy-datamap.lock.json", repo / ".fides" / "datamap.yml"]
    before = {str(p): p.read_bytes() if p.exists() else None for p in protected}
    proposals.write_text(json.dumps(document))
    ready = run(command)
    results.check("[privacy-datamap] supported unresolved questions are ready for human review",
                  ready.returncode == 0 and json.loads(ready.stdout)["status"] == "ready for human review", ready.stderr or ready.stdout)
    review = (cache / "privacy-datamap.review.md").read_text()
    results.check("[privacy-datamap] review leads with meaning and includes questions and coverage",
                  all(value in review for value in ["ambiguous", "Which business process", "Systems and processing",
                      "Possible Article 9 or Article 10", "Changes"]))
    results.check("[privacy-datamap] enrichment never accepts or exports decisions",
                  before == {str(p): p.read_bytes() if p.exists() else None for p in protected})
    mutations = {
        "missing queue item": lambda d: d["proposals"].pop(),
        "duplicate queue item": lambda d: d["proposals"].append(d["proposals"][0]),
        "stale observation": lambda d: d.update(observation_digest="stale"),
        "invalid category": lambda d: d["proposals"][0].update(proposed_categories=["invented.category"]),
        "hidden special category": lambda d: d["proposals"][0].update(proposal_kind="personal", proposed_categories=["user.health_and_medical"]),
        "missing analysis": lambda d: d["proposals"][0].update(analysis={}),
        "missing resolution": lambda d: d["proposals"][0].update(resolution_needed=""),
        "missing activities": lambda d: d["system_proposals"][0].update(processing_activities=[]),
        "duplicate activities": lambda d: d["system_proposals"][0]["processing_activities"].append(d["system_proposals"][0]["processing_activities"][0]),
        "missing ambiguity question": lambda d: d["proposals"][0].update(unresolved_question=""),
        "invalid citation": lambda d: d["proposals"][0].update(refs=["db/schema.sql:999999"]),
        "escaping citation": lambda d: d["proposals"][0].update(refs=["../outside.sql:1"]),
        "missing store investigation": lambda d: d.pop("store_investigation"),
        "missing systems": lambda d: d.update(system_proposals=[]),
        "invalid use": lambda d: d["system_proposals"][0]["processing_activities"][0].update(proposed_data_uses=["invented.use"]),
        "invalid subject": lambda d: d["system_proposals"][0]["processing_activities"][0].update(proposed_data_subjects=["invented.subject"]),
        "invalid relationship": lambda d: d["system_proposals"][0]["processing_activities"][0].update(dataset_references=["missing"]),
    }
    for name, mutate in mutations.items():
        changed = json.loads(json.dumps(document))
        mutate(changed)
        proposals.write_text(json.dumps(changed))
        rejected = run(command)
        results.check(f"[privacy-datamap] enrichment rejects {name}", rejected.returncode == 1,
                      rejected.stderr or rejected.stdout)
    document["store_investigation"]["findings"] = [{"store": "archive", "status": "gap",
        "rationale": "Payload contract is absent.", "refs": ["db/schema.sql:1"], "confidence": "low",
        "unresolved_question": "Provide the archive payload contract.", "resolution_needed": "Supply the typed upload payload.",
        "decision_summary": "Establish archive coverage.", "decision_impact": "Determines which stored fields need classification."}]
    proposals.write_text(json.dumps(document))
    gap = run(command)
    results.check("[privacy-datamap] explicit store gaps remain visible at review readiness",
                  gap.returncode == 0 and "Provide the archive payload contract" in (cache / "privacy-datamap.review.md").read_text())
    document["proposals"][0]["proposed_categories"] = ["user.health_and_medical"]
    proposals.write_text(json.dumps(document))
    special = run(command)
    special_section = (cache / "privacy-datamap.review.md").read_text().split("## Possible Article 9 or Article 10 data")[1]
    results.check("[privacy-datamap] ambiguous special categories appear in the dedicated review section",
                  special.returncode == 0 and document["proposals"][0]["entity_id"] in special_section)


    evidence = json.loads((cache / "privacy-datamap.evidence.json").read_text())
    schema = json.loads((ROOT / "contract" / "privacy-datamap-review.schema.json").read_text())
    results.check("[privacy-datamap] three review outputs satisfy the public contract",
                  not validate_json_schema(evidence, schema) and (cache / "privacy-datamap.map.md").exists())
    derived = json.loads((cache / "privacy-datamap.derived.json").read_text())
    observed_count = sum(len(c["fields"]) for d in derived["datasets"] for c in d["collections"])
    results.check("[privacy-datamap] evidence retains every observed field including deterministic fields",
                  len(evidence["evidence_record"]["fields"]) == observed_count)
    for row in document["proposals"]:
        row.update(proposal_kind="personal", proposed_categories=["user.unique_id"], unresolved_question="",
                   resolution_needed="", decision_impact="", reasoning_group="identifiers", rationale="", analysis={})
    document["decisions"]["classify"]["decision_summary"] = "Treat account identifiers as personal data."
    document["reasoning_groups"] = {"identifiers": {"summary": "Account identifiers", "rationale": "Shared account linkage rationale.",
        "confidence": "medium", "refs": ["db/schema.sql:1"], "analysis": {"schema": "Identifier columns.",
        "relationships": "Linkage remains a proposal.", "service_or_serialization": "No service code is present."}}}
    proposals.write_text(json.dumps(document))
    grouped = run(command)
    review = (cache / "privacy-datamap.review.md").read_text()
    results.check("[privacy-datamap] shared decision summary appears once and full reasoning stays in evidence",
                  grouped.returncode == 0 and review.count("Treat account identifiers as personal data.") == 1
                  and "Shared account linkage rationale." not in review
                  and "Shared account linkage rationale." in (cache / "privacy-datamap.evidence.json").read_text(), grouped.stdout or grouped.stderr)
    document["proposals"][0].update(proposal_kind="non_personal", proposed_categories=[], reasoning_group="",
                                    rationale="Technical row counter.")
    document["proposals"][0]["analysis"] = document["reasoning_groups"]["identifiers"]["analysis"]
    activity = document["system_proposals"][0]["processing_activities"][0]
    document["system_proposals"][0]["processing_activities"] = [dict(activity, activity_id=name, purpose=name)
        for name in ("Authentication", "Training", "Billing", "Provider sharing")]
    proposals.write_text(json.dumps(document))
    technical = run(command)
    review = (cache / "privacy-datamap.review.md").read_text()
    map_text = (cache / "privacy-datamap.map.md").read_text()
    results.check("[privacy-datamap] technical fields collapse and distinct processing descriptions survive",
                  technical.returncode == 0 and "<details><summary>Technical field coverage" in review
                  and "Technical row counter." not in review and all(name in map_text for name in
                  ("Authentication", "Training", "Billing", "Provider sharing")))
    document["structural_errors"] = [{"kind": "datastore boundary", "detail": "Two distinct catalog databases were combined.",
                                     "resolution_needed": "Correct the supplemental dataset identities and rescan."}]
    proposals.write_text(json.dumps(document))
    blocked = run(command)
    review = (cache / "privacy-datamap.review.md").read_text()
    results.check("[privacy-datamap] structural errors defer privacy decisions without dropping evidence",
                  blocked.returncode == 1 and "Fix structure before privacy review" in review
                  and "Changes requiring decisions" not in review
                  and len(json.loads((cache / "privacy-datamap.evidence.json").read_text())["evidence_record"]["fields"]) == observed_count)

    document["structural_errors"] = []
    proposals.write_text(json.dumps(document))
    for gap_kind in ("schema_conflicts", "migration_gaps"):
        changed_derived = json.loads(json.dumps(derived))
        changed_derived.setdefault("coverage", {})[gap_kind] = [{"ref": "db/schema.sql:1", "reason": "Conflicting structure."}]
        (cache / "privacy-datamap.derived.json").write_text(json.dumps(changed_derived))
        failed_structure = run(command)
        results.check(f"[privacy-datamap] observed {gap_kind} block privacy readiness",
                      failed_structure.returncode == 1 and json.loads(failed_structure.stdout)["structural_blockers"])

    # Compare a concise reference decision set against the same complete fixture inventory.
    (cache / "privacy-datamap.derived.json").write_text(json.dumps(derived))
    document["store_investigation"]["findings"] = []
    for index, row in enumerate(document["proposals"]):
        row.update(proposal_kind="personal", proposed_categories=["user.unique_id"], reasoning_group="",
                   rationale=f"Field-specific explanation {index}: " + "Detailed evidence retained for audit. " * 30,
                   analysis={"schema": "Read the column definition.", "relationships": "Inspected account relationships.",
                             "service_or_serialization": "Inspected runtime use."})
    for activity in document["system_proposals"][0]["processing_activities"]:
        activity.update(decision_summary=f"Use account data for {activity['purpose'].lower()}.",
                        proposed_data_uses=["essential.service"], proposed_data_subjects=["customer"],
                        dataset_references=[derived["datasets"][0]["fides_key"]],
                        unresolved_question="", business_context_question="", resolution_needed="", decision_impact="",
                        rationale="Detailed processing explanation. " * 30)
    proposals.write_text(json.dumps(document))
    precise = run(command)
    review = (cache / "privacy-datamap.review.md").read_text()
    evidence = json.loads((cache / "privacy-datamap.evidence.json").read_text())
    groups = evidence["review_queue"]["field_groups"]
    results.check("[privacy-datamap] concise reference decisions preserve categories, scope and all evidence",
                  precise.returncode == 0 and len(groups) == 1
                  and groups[0]["data_categories"] == ["user.unique_id"]
                  and set(groups[0]["field_ids"]) == {row["entity_id"] for row in document["proposals"]}
                  and len(evidence["evidence_record"]["fields"]) == observed_count
                  and len(review.split()) <= 230, f"{len(review.split())} words; {precise.stderr or precise.stdout}")
    results.check("[privacy-datamap] different field reasoning groups by decision and stays out of the review",
                  review.count("Treat account identifiers as personal data.") == 1
                  and "Field-specific explanation" not in review and "Detailed processing explanation" not in review
                  and all(row["rationale"] in (cache / "privacy-datamap.evidence.json").read_text() for row in document["proposals"]))
    results.check("[privacy-datamap] supported activities need no artificial questions or repeated instructions",
                  "Unknown:" not in review and "To resolve:" not in review
                  and review.count("Accept or amend") == 1 and review.count("[Full evidence]") == 1
                  and "## Coverage and identity questions" not in review)
    proposal_contract = json.loads((ROOT / "contract" / "privacy-datamap-proposals.schema.json").read_text())
    results.check("[privacy-datamap] question-free proposals satisfy both output contracts",
                  not validate_json_schema(document, proposal_contract) and not validate_json_schema(evidence, schema))
    for name, mutate in {
        "unknown decision": lambda d: d["proposals"][0].update(decision_id="absent"),
        "verbose summary": lambda d: d["decisions"]["classify"].update(decision_summary="x" * 241),
        "multiline summary": lambda d: d["decisions"]["classify"].update(decision_summary="One line\nAnother line"),
        "question without impact": lambda d: d["system_proposals"][0]["processing_activities"][0].update(
            unresolved_question="Are events used for analytics?", resolution_needed="Identify the analytics consumer."),
        "conflicting grouped questions": lambda d: d["proposals"][0].update(
            unresolved_question="Does this identifier belong to an employee?", resolution_needed="Identify the principal type.",
            decision_impact="Determines the data subject."),
    }.items():
        changed = json.loads(json.dumps(document))
        mutate(changed)
        proposals.write_text(json.dumps(changed))
        failed = run(command)
        results.check(f"[privacy-datamap] precise review rejects {name}", failed.returncode == 1,
                      failed.stderr or failed.stdout)
    activity = document["system_proposals"][0]["processing_activities"][-1]
    activity.update(unresolved_question="Does the provider retain account payloads?",
                    resolution_needed="Provide the provider retention configuration.",
                    decision_impact="Determines whether the flow includes persistent provider storage.")
    proposals.write_text(json.dumps(document))
    unresolved = run(command)
    review = (cache / "privacy-datamap.review.md").read_text()
    results.check("[privacy-datamap] one material unknown produces exactly one actionable question",
                  unresolved.returncode == 0 and review.count("Unknown:") == 1
                  and "provider retention configuration" in review and "persistent provider storage" in review)

    module_spec = importlib.util.spec_from_file_location("proposal_relationship_workflow", scripts / "reconcile.py")
    workflow = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(workflow)

    graph = {"nodes": [
        {"id": "runtime", "kind": "runtime", "key": "repository"},
        {"id": "client", "kind": "client"},
        {"id": "connection", "kind": "connection", "unresolved_question": "Which deployed database is selected?",
         "resolution_needed": "Provide the deployed connection configuration."},
        {"id": "schema", "kind": "schema", "paths": ["db/schema.sql"]},
    ], "edges": []}
    mapping_specs = []
    for source, target in (("runtime", "client"), ("client", "connection"), ("client", "schema")):
        identity = source + "_" + target
        spec = {"id": identity, "path": "db/schema.sql", "method": "text", "targets": ["relationship:" + identity]}
        # Synthetic evidence for the proposal gate, without accepting a mapping.
        spec["fingerprint"] = workflow.dependencies.observe(repo, spec)["fingerprint"]
        mapping_specs.append(spec)
        graph["edges"].append({"id": identity, "source": source, "target": target, "dependencies": [identity],
                               "rationale": "Synthetic relationship evidence for the validator fixture.", "confidence": "low"})
    document["relationship_proposal"] = {"graph": graph, "evidence_dependencies": mapping_specs,
                                          "decision_summary": "Keep the connection separate until its destination is known."}
    proposals.write_text(json.dumps(document))
    mapped = run(command)
    rendered = (cache / "privacy-datamap.review.md").read_text()
    results.check("[privacy-datamap] mapping proposals require a collected preview before acceptance",
                  mapped.returncode == 1 and "--candidate" in mapped.stdout and "Keep the connection separate" in rendered
                  and "Provide the deployed connection configuration" in rendered, mapped.stderr or mapped.stdout)
    mapping_specs[0]["fingerprint"] = "0" * 64
    proposals.write_text(json.dumps(document))
    stale = run(command)
    results.check("[privacy-datamap] review rejects stale relationship proposal evidence", stale.returncode == 1, stale.stderr or stale.stdout)


def test_discovery_acceptance_gate(results, tmp):
    scripts = PRIVACY_DATAMAP / "scripts"
    spec = importlib.util.spec_from_file_location("discovery_workflow", scripts / "reconcile.py")
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)
    repo = write_files(pathlib.Path(tmp) / "discovery-gate", {"db/schema.sql": SQL_FIXTURE})
    summary, _ = datamap_scan(repo)
    manifest = repo / ".noru/privacy-datamap.yml"
    manifest.write_text(accepted_datamap_text(summary["derived_digest"]))
    command = ["python3", str(scripts / "reconcile.py"), f"--repo={repo}", "--output=json"]
    assert run(command + ["--seal"]).returncode == 0
    assert run(command).returncode == 0
    lock = repo / ".noru/privacy-datamap.lock.json"
    before = lock.read_bytes()
    parsed = repo / ".noru/.cache/privacy-datamap.parsed.json"
    assert run(["python3", str(scripts / "validate_manifest.py"), str(manifest), f"--emit-parsed={parsed}", "--quiet"]).returncode == 0
    (repo / "src").mkdir()
    (repo / "src/labels.ts").write_text('export const label = (value: string) => value;\n')
    summary, _ = datamap_scan(repo)
    pending = json.loads(run(command).stdout)
    results.check("[privacy-datamap] new unregistered scope blocks acceptance and stale export without schema drift",
                  summary["rendered"] is None and not pending["drift"] and pending["agent_required"]
                  and not pending["accepted_current"] and run(command + ["--seal"]).returncode == 1 and lock.read_bytes() == before)
    path = repo / ".noru/.cache/privacy-datamap.proposals.json"
    document = json.loads(path.read_text())
    change = document["discovery_required"][0]
    answer = {"path": change["path"], "observation_digest": workflow.dependencies.digest(change), "outcome": "no_new_scope",
              "decision_summary": "The unused identity formatter adds no datastore or processing flow.",
              "rationale": "The only new declaration returns its argument and has no callers, imports, persistence or service calls.",
              "confidence": "high", "refs": ["missing.ts:1"]}
    document["discovery_proposals"] = [answer]
    path.write_text(json.dumps(document))
    results.check("[privacy-datamap] discovery acceptance rejects unsupported answers", run(command + ["--seal"]).returncode == 1 and lock.read_bytes() == before)
    answer["refs"] = ["src/labels.ts:1"]
    path.write_text(json.dumps(document))
    sealed = run(command + ["--seal"])
    current = json.loads(run(command).stdout)
    results.check("[privacy-datamap] explicit reviewed discovery establishes a clean future baseline",
                  sealed.returncode == 0 and not current["discovery_required"] and current["accepted_current"], sealed.stderr)


def test_scoped_reconciliation_cli(results, tmp):
    scripts = PRIVACY_DATAMAP / "scripts"
    spec = importlib.util.spec_from_file_location("scoped_cli", scripts / "reconcile.py")
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)
    text = 'export function serialize(user: User) { return {email:user.email}; }\nexport function independent(value: string) { return value; }\n'
    repo = write_files(pathlib.Path(tmp) / "scoped-cli", {"db/schema.sql": 'CREATE TABLE accounts (\n  alpha_token TEXT,\n  beta_token TEXT\n);\n',
                                                       "src/payload.ts": text})
    datamap_scan(repo)
    command = ["python3", str(scripts / "reconcile.py"), f"--repo={repo}", "--output=json"]
    assert run(command).returncode == 0
    path = repo / ".noru/.cache/privacy-datamap.proposals.json"
    document = json.loads(path.read_text())
    document["evidence_dependencies"] = []
    for row, selector in zip(document["proposals"], ["serialize", "independent"]):
        row["rationale"] = "Preserve the inspected meaning of " + selector
        row["refs"] = ["src/payload.ts:1"]
        dependency = {"id": selector, "path": "src/payload.ts", "method": "typescript_ast", "selector": selector, "targets": [row["entity_id"]]}
        dependency["fingerprint"] = workflow.dependencies.observe(repo, dependency)["fingerprint"]
        document["evidence_dependencies"].append(dependency)
    path.write_text(json.dumps(document))
    assert run(command).returncode == 0
    bound = json.loads(path.read_text())
    (repo / "README.md").write_text('Unrelated prose changed.\n')
    datamap_scan(repo)
    assert run(command).returncode == 0
    after_doc = json.loads(path.read_text())
    results.check("[privacy-datamap] CLI reconciliation preserves unaccepted analysis after README edits",
                  after_doc["proposals"] == bound["proposals"] and not after_doc["discovery_required"])
    (repo / "src/payload.ts").write_text('// comment\n\n' + text.replace('{email:user.email}', '{ email: user.email, }'))
    datamap_scan(repo)
    assert run(command).returncode == 0
    after_format = json.loads(path.read_text())
    results.check("[privacy-datamap] CLI reconciliation refreshes citations without invalidating TypeScript analysis",
                  all(state["status"] == "current" for state in after_format["evidence_state"].values())
                  and after_format["proposals"][0]["refs"] == ["src/payload.ts:3"]
                  and not after_format["discovery_required"])
    (repo / "src/payload.ts").write_text(text.replace('email:user.email', 'email:user.email, phone:user.phone'))
    datamap_scan(repo)
    assert run(command).returncode == 0
    changed = json.loads(path.read_text())
    alpha, beta = [row["entity_id"] for row in changed["proposals"]]
    results.check("[privacy-datamap] CLI reconciliation preserves independent proposals and flags only affected evidence",
                  changed["evidence_state"][alpha]["status"] == "investigate" and changed["evidence_state"][beta]["status"] == "current"
                  and changed["proposals"][1]["rationale"] == bound["proposals"][1]["rationale"]
                  and not changed["discovery_required"])
    (repo / "src/payload.ts").write_text(text + 'export const newArchive = new Storage();\n')
    datamap_scan(repo)
    assert run(command).returncode == 0
    new_scope = json.loads(path.read_text())
    results.check("[privacy-datamap] CLI discovery queues new integration scope without deleting existing analysis",
                  [row["path"] for row in new_scope["discovery_required"]] == ["src/payload.ts"]
                  and new_scope["proposals"][1]["rationale"] == bound["proposals"][1]["rationale"])


def test_typescript_and_scoped_analysis(results, tmp):
    scripts = PRIVACY_DATAMAP / "scripts"
    spec = importlib.util.spec_from_file_location("scoped_workflow", scripts / "reconcile.py")
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)
    dependencies, analysis = workflow.dependencies, workflow.analysis_cache
    source = 'import { format } from "./helper";\nexport function serialize(user: User): Payload { return { email: format(user.email) }; }\nexport function unrelated() { return 1; }\n'
    repo = write_files(pathlib.Path(tmp) / "scoped-analysis", {
        "src/payload.ts": source,
        "src/helper.ts": 'export function format(value: string): string { return value; }\n',
        "src/region.ts": 'export function region(value: string): string { return value; }\n',
        "src/client.ts": 'export const client = connect(settings.PRIMARY);\n',
        "README.md": 'Unrelated documentation.\n',
    })
    def observe(text, selector="serialize"):
        (repo / "src/payload.ts").write_text(text)
        return dependencies.observe(repo, {"path": "src/payload.ts", "method": "typescript_ast", "selector": selector, "targets": ["field"]})
    initial = observe(source)
    formatted = 'import {format} from \'./helper\'\n// shifted citation\n\nexport function serialize( user:User ):Payload {\n return {\n email:format(user.email),\n }\n}\nexport function unrelated(){return 999}\n'
    same = observe(formatted)
    results.check("[privacy-datamap] TypeScript AST ignores comments, layout, quotes, trailing commas and unrelated declarations",
                  same["fingerprint"] == initial["fingerprint"] and same["refs"] != initial["refs"])
    for name, modified in {
        "personal property": source.replace('email: format(user.email)', 'email: format(user.email), phone: user.phone'),
        "property name": source.replace('email: format', 'address: format'),
        "literal value": source.replace('return 1', 'return 2').replace('format(user.email)', '"new-value"'),
        "call": source.replace('format(user.email)', 'other(user.email)'),
        "condition": source.replace('return { email:', 'if (user.active) return { email:'),
        "type": source.replace('user: User', 'user: OtherUser'),
    }.items():
        results.check(f"[privacy-datamap] TypeScript AST preserves {name}", observe(modified)["fingerprint"] != initial["fingerprint"])
    results.check("[privacy-datamap] TypeScript AST retains significant return line breaks",
                  observe('function serialize() { return value; }')["fingerprint"] != observe('function serialize() { return\nvalue; }')["fingerprint"])
    results.check("[privacy-datamap] TypeScript AST retains optional-chain grouping",
                  observe('function serialize() { return a?.b.c; }')["fingerprint"] != observe('function serialize() { return (a?.b).c; }')["fingerprint"])
    results.check("[privacy-datamap] TypeScript AST retains async line-break semantics",
                  observe('async function serialize() { return 1; }')["fingerprint"] != observe('async\nfunction serialize() { return 1; }')["fingerprint"])
    results.check("[privacy-datamap] TypeScript AST retains prototype shorthand semantics",
                  observe('function serialize() { return {__proto__}; }')["fingerprint"] != observe('function serialize() { return {__proto__:__proto__}; }')["fingerprint"])
    results.check("[privacy-datamap] TypeScript object return types and semicolonless type aliases normalize consistently",
                  observe('type Payload = {email: string;};\nfunction serialize(user: User): {email: string} { return {email:user.email}; }')["fingerprint"]
                  == observe('type Payload = {email:string}\nfunction serialize(user:User):{email:string}{return { email:user.email, }}')["fingerprint"])
    results.check("[privacy-datamap] TypeScript generic client calls preserve type arguments",
                  observe('function serialize() { return client<User>(config.url); }')["fingerprint"]
                  != observe('function serialize() { return client<Admin>(config.url); }')["fingerprint"])
    failed = False
    try:
        observe('function serialize() { return <Widget />; }')
    except ValueError:
        failed = True
    results.check("[privacy-datamap] unsupported TypeScript syntax is an explicit evidence gap", failed)
    observe(source)
    original_import = dependencies.observe(repo, {"path": "src/payload.ts", "method": "typescript_ast", "selector": "import:format", "targets": ["field"]})
    (repo / "src/payload.ts").write_text(source.replace('./helper','./other-helper'))
    changed_import = dependencies.observe(repo, {"path": "src/payload.ts", "method": "typescript_ast", "selector": "import:format", "targets": ["field"]})
    results.check("[privacy-datamap] explicit imported binding dependencies detect changed helper sources", original_import["fingerprint"] != changed_import["fingerprint"])
    observe(source)
    document = {"proposals": [{"entity_id": "db/accounts/email", "refs": ["src/payload.ts:2"], "rationale": "Email is serialized for the account flow."},
                              {"entity_id": "db/accounts/region", "refs": ["src/region.ts:1"], "rationale": "Region is independent."}],
                "system_proposals": [{"entity_id": "api", "refs": ["src/client.ts:1"], "rationale": "The API uses this client.",
                    "processing_activities": [{"activity_id": "send", "refs": ["src/payload.ts:2", "src/client.ts:1"], "purpose": "Send account payloads"}]}],
                "reasoning_groups": {}, "dependency_proposals": [], "evidence_dependencies": [],
                "relationship_proposal": {"graph": {"edges": [{"id": "connection", "rationale": "Client binding"}]}, "evidence_dependencies": []}}
    def register(identity, path, selector, targets):
        spec = {"id": identity, "path": path, "method": "typescript_ast", "selector": selector, "targets": targets}
        spec["fingerprint"] = dependencies.observe(repo, spec)["fingerprint"]
        document["evidence_dependencies"].append(spec)
    register("serializer", "src/payload.ts", "serialize", ["db/accounts/email", "activity:api/send"])
    register("import", "src/payload.ts", "import:format", ["db/accounts/email", "activity:api/send"])
    register("helper", "src/helper.ts", "format", ["db/accounts/email", "activity:api/send"])
    register("region", "src/region.ts", "region", ["db/accounts/region"])
    register("client", "src/client.ts", "client", ["system:api", "activity:api/send", "relationship:connection"])
    results.check("[privacy-datamap] proposal evidence binds fields, activities and relationships independently", not analysis.refresh(document, repo))
    discovery = analysis.discover(repo)
    analysis.refresh_discovery(document, discovery)
    independent = json.loads(json.dumps(document["proposals"][1]))
    (repo / "README.md").write_text('Changed only the README.\n')
    results.check("[privacy-datamap] unrelated documentation preserves scoped analysis and discovery state",
                  not analysis.refresh(document, repo) and not analysis.refresh_discovery(document, analysis.discover(repo))
                  and document["proposals"][1] == independent)
    (repo / "src/payload.ts").write_text(formatted.replace('return 999','return 1'))
    results.check("[privacy-datamap] moved TypeScript citations refresh without an investigation",
                  not analysis.refresh(document, repo) and document["proposals"][0]["refs"] == ["src/payload.ts:4"]
                  and not analysis.refresh_discovery(document, analysis.discover(repo)))
    (repo / "src/payload.ts").write_text(source.replace('email: format(user.email)', 'email: format(user.email), phone: user.phone'))
    analysis.refresh(document, repo)
    states = document["evidence_state"]
    results.check("[privacy-datamap] serializer changes investigate only dependent fields and payload flows",
                  states["db/accounts/email"]["status"] == states["activity:api/send"]["status"] == "investigate"
                  and states["db/accounts/region"]["status"] == states["relationship:connection"]["status"] == "current"
                  and document["proposals"][1] == independent)
    results.check("[privacy-datamap] registered declaration changes need no duplicate new-scope review",
                  not analysis.refresh_discovery(document, analysis.discover(repo)))
    observe(source)
    (repo / "src/client.ts").write_text('export const client = connect(settings.SECONDARY);\n')
    analysis.refresh(document, repo)
    states = document["evidence_state"]
    results.check("[privacy-datamap] connection changes investigate datastore relationships and affected activities",
                  states["relationship:connection"]["status"] == states["activity:api/send"]["status"] == "investigate"
                  and states["db/accounts/email"]["status"] == states["db/accounts/region"]["status"] == "current")
    (repo / "src/client.ts").write_text('export const client = connect(settings.PRIMARY);\n')
    (repo / "src/new-integration.ts").write_text('import { Storage } from "object-store";\nexport const archive = new Storage();\n')
    pending = analysis.refresh_discovery(document, analysis.discover(repo))
    results.check("[privacy-datamap] broad discovery finds a new unregistered integration while preserving existing analysis",
                  [row["path"] for row in pending] == ["src/new-integration.ts"] and not analysis.refresh(document, repo))
    (repo / "src/payload.ts").write_text(source + 'export const archive = connect(settings.ARCHIVE);\n')
    pending = analysis.refresh_discovery(document, analysis.discover(repo))
    results.check("[privacy-datamap] broad discovery finds new declarations even in a registered source file",
                  "src/payload.ts" in [row["path"] for row in pending])
    results.check("[privacy-datamap] whole-file dependencies do not hide newly added declarations",
                  "src/payload.ts" in [row["path"] for row in analysis.discovery_changes(discovery, analysis.discover(repo),
                     [{"path": "src/payload.ts", "method": "typescript_ast"}])])

    observe(source)
    (repo / "src/helper.ts").rename(repo / "src/moved-helper.ts")
    results.check("[privacy-datamap] unique helper moves refresh proposal citations and dependencies",
                  not analysis.refresh(document, repo)
                  and next(s for s in document["evidence_dependencies"] if s["id"] == "helper")["path"] == "src/moved-helper.ts")
    (repo / "src/moved-helper.ts").unlink()
    analysis.refresh(document, repo)
    results.check("[privacy-datamap] missing dependencies mark only their dependent proposals unresolved",
                  document["evidence_state"]["db/accounts/email"]["status"] == "unresolved"
                  and document["evidence_state"]["db/accounts/region"]["status"] == "current")
    helper = 'export function format(value: string): string { return value; }\n'
    (repo / "src/copy-a.ts").write_text(helper)
    (repo / "src/copy-b.ts").write_text(helper)
    analysis.refresh(document, repo)
    results.check("[privacy-datamap] ambiguous dependency moves remain unresolved on repeated reconciliation",
                  any(i["action"] == "identity_ambiguity" for i in document["evidence_state"]["db/accounts/email"]["issues"])
                  and bool(analysis.refresh(document, repo)))


def test_datamap_candidate_collection(results, tmp):
    scripts = PRIVACY_DATAMAP / "scripts"
    module_spec = importlib.util.spec_from_file_location("candidate_workflow", scripts / "reconcile.py")
    workflow = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(workflow)
    repo = write_files(pathlib.Path(tmp) / "candidate-collection", {
        "db/schema.sql": SQL_FIXTURE,
        "src/connection.py": 'SCHEMA_PATH = "db/schema.sql"\nDATASTORE = "primary_store"\n',
    })
    summary, _ = datamap_scan(repo)
    manifest = repo / ".noru/privacy-datamap.yml"
    manifest.write_text(accepted_datamap_text(summary["derived_digest"]))
    reconcile_command = ["python3", str(scripts / "reconcile.py"), f"--repo={repo}", "--output=json"]
    review_command = ["python3", str(scripts / "review.py"), f"--repo={repo}", "--output=json"]
    assert run(reconcile_command + ["--seal"]).returncode == 0
    assert run(reconcile_command).returncode == 0
    assert run(review_command).returncode == 0
    cache = repo / ".noru/.cache"
    proposals_path = cache / "privacy-datamap.proposals.json"
    proposals = json.loads(proposals_path.read_text())
    graph = {"nodes": [
        {"id": "runtime", "kind": "runtime", "key": "repository"},
        {"id": "client", "kind": "client"}, {"id": "connection", "kind": "connection"},
        {"id": "store", "kind": "datastore", "key": "primary_store"},
        {"id": "schema", "kind": "schema", "paths": ["db/schema.sql"]},
    ], "edges": []}
    specs = []
    for source, target in (("runtime", "client"), ("client", "connection"), ("connection", "store"), ("client", "schema")):
        identity = source + "_" + target
        spec = {"id": identity, "method": "python_ast", "path": "src/connection.py", "targets": ["relationship:" + identity]}
        spec["fingerprint"] = workflow.dependencies.observe(repo, spec)["fingerprint"]
        specs.append(spec)
        graph["edges"].append({"id": identity, "source": source, "target": target, "dependencies": [identity],
                               "rationale": "The synthetic connection configuration selects this schema and store.", "confidence": "high"})
    proposals["relationship_proposal"] = {"graph": graph, "evidence_dependencies": specs,
                                           "decision_summary": "Assign account storage to the configured primary store."}
    proposals_path.write_text(json.dumps(proposals))
    assert run(["python3", str(scripts / "validate_manifest.py"), str(manifest),
                f"--emit-parsed={cache / 'privacy-datamap.parsed.json'}", "--quiet"]).returncode == 0
    protected = {p: p.read_bytes() for p in (repo / ".noru").rglob("*") if p.is_file()}
    (repo / ".fides").mkdir(exist_ok=True)
    export = repo / ".fides/datamap.yml"
    export.write_text("Existing accepted export remains unchanged.\n")
    protected[export] = export.read_bytes()
    candidate_command = ["node", str(scripts / "collect.mjs"), f"--repo={repo}", "--candidate", "--output=json"]
    preview = cache / "privacy-datamap-preview"
    collected = run(candidate_command)
    results.check("[privacy-datamap] candidate collection consumes proposals without acceptance", collected.returncode == 0, collected.stderr)
    derived = json.loads((preview / "privacy-datamap.derived.json").read_text())
    results.check("[privacy-datamap] proposed connection corrects candidate collection and runtime datastore boundaries",
                  [d["fides_key"] for d in derived["datasets"]] == ["primary_store"]
                  and derived["datasets"][0]["collections"][0]["name"] == "accounts"
                  and derived["systems"][0]["dataset_references"] == ["primary_store"])
    first_observations = (preview / "privacy-datamap.derived.json").read_bytes()
    repeated = run(candidate_command)
    results.check("[privacy-datamap] unchanged candidate collection is byte-identical",
                  repeated.returncode == 0 and (preview / "privacy-datamap.derived.json").read_bytes() == first_observations)
    reconciled = run(reconcile_command + ["--candidate"])
    results.check("[privacy-datamap] candidate reconciliation generates a separate review queue",
                  reconciled.returncode == 0 and not json.loads(reconciled.stdout)["accepted_current"], reconciled.stderr)
    candidate_path = preview / "privacy-datamap.candidate.yml"
    candidate = load_datamap_yaml(candidate_path)
    results.check("[privacy-datamap] candidate manifest contains proposed graph and corrected fields",
                  candidate["relationship_graph"] == graph and candidate["dataset"][0]["fides_key"] == "primary_store"
                  and bool(candidate["evidence_dependencies"]))
    incomplete = run(review_command + ["--candidate"])
    results.check("[privacy-datamap] corrected preview still requires complete enrichment",
                  incomplete.returncode == 1 and json.loads(incomplete.stdout)["status"] == "enrichment incomplete")
    preview_proposals_path = preview / "privacy-datamap.proposals.json"
    document = json.loads(preview_proposals_path.read_text())
    document["decisions"] = {"meaning": {"decision_summary": "Establish the remaining field meanings."}}
    for row in document["proposals"]:
        row.update(decision_id="meaning", proposal_kind="ambiguous", proposed_categories=[],
                   rationale="Storage is established; business use is absent from this fixture.", confidence="low",
                   unresolved_question="Which business process uses these values?",
                   resolution_needed="Identify the process and its subjects.", decision_impact="Determines personal-data categories.",
                   analysis={"schema": "Read the SQL definition.", "relationships": "Traced the explicit client binding.",
                             "service_or_serialization": "Only connection configuration is present."})
    for row in document["system_proposals"]:
        row.update(rationale="The connection selects the primary store.", refs=["src/connection.py:1"], confidence="high",
                   processing_activities=[{"activity_id": "accounts", "purpose": "Account processing purpose remains unresolved",
                       "decision_summary": "Establish the account processing purpose.", "proposed_data_uses": [], "proposed_data_subjects": [],
                       "dataset_references": ["primary_store"], "relationship_rationale": "The proposed connection targets the primary store.",
                       "rationale": "The fixture establishes a destination but no business process.", "refs": ["src/connection.py:1"], "confidence": "low",
                       "unresolved_question": "What process uses this account store?", "resolution_needed": "Identify the process and its subjects.",
                       "decision_impact": "Determines purpose and subject classifications."}])
    document["store_investigation"].update(search_scope="Read the schema and connection configuration.",
        rationale="No additional persistent integration exists in this synthetic fixture.", refs=["src/connection.py:1"], confidence="high", findings=[])
    preview_proposals_path.write_text(json.dumps(document))
    reviewed = run(review_command + ["--candidate"])
    evidence = json.loads((preview / "privacy-datamap.evidence.json").read_text())
    results.check("[privacy-datamap] human review sees the corrected candidate map with unresolved business context",
                  reviewed.returncode == 0 and json.loads(reviewed.stdout)["status"] == "ready for human review"
                  and evidence["data_map"]["stores"][0]["id"] == "primary_store"
                  and "Candidate collection" in (preview / "privacy-datamap.map.md").read_text(), reviewed.stderr or reviewed.stdout)
    results.check("[privacy-datamap] candidate flow preserves accepted files, normal review artifacts and export",
                  all(p.read_bytes() == content for p, content in protected.items()))
    del document["relationship_proposal"]
    preview_proposals_path.write_text(json.dumps(document))
    missing_mapping = run(review_command + ["--candidate"])
    results.check("[privacy-datamap] candidate review cannot omit the mapping decision",
                  missing_mapping.returncode == 1 and "requires the collected relationship_proposal" in missing_mapping.stdout)
    document["relationship_proposal"] = proposals["relationship_proposal"]
    preview_proposals_path.write_text(json.dumps(document))
    assert run(review_command + ["--candidate"]).returncode == 0

    completed_analysis = json.loads(preview_proposals_path.read_text())
    completed_review = (preview / "privacy-datamap.review.md").read_bytes()
    repeat_collection = run(candidate_command)
    repeat_reconciliation = run(reconcile_command + ["--candidate"])
    results.check("[privacy-datamap] repeated candidate scans preserve completed analysis and review",
                  repeat_collection.returncode == 0 and repeat_reconciliation.returncode == 0
                  and json.loads(preview_proposals_path.read_text()) == completed_analysis
                  and (preview / "privacy-datamap.review.md").read_bytes() == completed_review)
    results.check("[privacy-datamap] candidate mode cannot seal or run acceptance checks",
                  run(reconcile_command + ["--candidate", "--seal"]).returncode == 2
                  and run(candidate_command + ["--check"]).returncode == 2)
    preview_before = {p: p.read_bytes() for p in preview.iterdir() if p.is_file()}
    preview_derived_path = preview / "privacy-datamap.derived.json"
    preview_derived_path.write_text(first_observations.decode().replace('"primary_store"', '"edited_store"'))
    results.check("[privacy-datamap] edited candidate observations cannot reach review",
                  run(review_command + ["--candidate"]).returncode != 0)
    preview_derived_path.write_bytes(preview_before[preview_derived_path])
    original_manifest = manifest.read_bytes()
    manifest.write_bytes(original_manifest + b"\n")
    results.check("[privacy-datamap] accepted baseline changes require a fresh candidate collection",
                  run(reconcile_command + ["--candidate"]).returncode != 0)
    manifest.write_bytes(original_manifest)
    proposals["relationship_proposal"]["decision_summary"] = "Changed mapping proposal requires a fresh preview."
    proposals_path.write_text(json.dumps(proposals))
    results.check("[privacy-datamap] changed source proposal invalidates cached candidate review",
                  run(review_command + ["--candidate"]).returncode != 0
                  and all(p.read_bytes() == content for p, content in preview_before.items()))
    proposals["relationship_proposal"]["decision_summary"] = "Assign account storage to the configured primary store."
    proposals_path.write_text(json.dumps(proposals))
    (repo / "src/connection.py").write_text('SCHEMA_PATH = "db/schema.sql"\nDATASTORE = "different_store"\n')
    rejected = run(candidate_command)
    results.check("[privacy-datamap] stale candidate evidence fails before overwriting existing preview artifacts",
                  rejected.returncode != 0 and "evidence changed" in rejected.stderr
                  and run(reconcile_command + ["--candidate"]).returncode != 0
                  and all(p.read_bytes() == content for p, content in preview_before.items()), rejected.stderr)
    (repo / "src/connection.py").write_text('SCHEMA_PATH = "db/schema.sql"\nDATASTORE = "primary_store"\n')
    specs[0]["path"] = "missing.py"
    proposals_path.write_text(json.dumps(proposals))
    results.check("[privacy-datamap] missing candidate evidence never falls back to accepted bindings", run(candidate_command).returncode != 0)
    specs[0]["path"] = "src/connection.py"
    graph["edges"][0]["target"] = "unknown"
    proposals_path.write_text(json.dumps(proposals))
    results.check("[privacy-datamap] malformed candidate graph is rejected without changing the accepted map", run(candidate_command).returncode != 0)
    graph["edges"][0]["target"] = "client"
    proposals_path.write_text(json.dumps(proposals))
    graph["nodes"][3]["key"] = "db"
    proposals_path.write_text(json.dumps(proposals))
    unchanged_preview = run(candidate_command)
    unchanged_reconciliation = run(reconcile_command + ["--candidate"])
    results.check("[privacy-datamap] structurally unchanged proposed mappings still require review and never export",
                  unchanged_preview.returncode == 0 and json.loads(unchanged_preview.stdout)["rendered"] is None
                  and unchanged_reconciliation.returncode == 0
                  and not json.loads(unchanged_reconciliation.stdout)["accepted_current"]
                  and json.loads(unchanged_reconciliation.stdout)["agent_required"]
                  and export.read_bytes() == protected[export])
    manifest.unlink()
    results.check("[privacy-datamap] candidate collection works before any accepted manifest exists",
                  run(candidate_command).returncode == 0 and not manifest.exists())


def test_datamap_connection_graph(results, tmp):
    scripts = PRIVACY_DATAMAP / "scripts"
    spec = importlib.util.spec_from_file_location("relationship_workflow", scripts / "reconcile.py")
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)
    repo = write_files(pathlib.Path(tmp) / "connection-graph", {
        "models/schema.sql": "CREATE TABLE accounts (\n    email TEXT\n);\n",
        "src/connect.py": 'PRIMARY = "first"\nSECONDARY = "second"\n',
    })
    graph = {"nodes": [
        {"id": "runtime", "kind": "runtime", "key": "repository"},
        {"id": "schema", "kind": "schema", "paths": ["models/schema.sql"]},
    ], "edges": []}
    specs = []
    for name in ("primary", "secondary"):
        graph["nodes"].extend([
            {"id": name, "kind": "client"},
            {"id": name + "_connection", "kind": "connection", "unresolved_question": "Which database does this connection reach?",
             "resolution_needed": "Identify the deployed connection destination."},
        ])
        for suffix, source, target in (("runtime", "runtime", name), ("connection", name, name + "_connection"), ("schema", name, "schema")):
            identity = name + "_" + suffix
            dependency = {"id": identity, "path": "src/connect.py", "method": "python_ast", "targets": ["relationship:" + identity]}
            dependency["fingerprint"] = workflow.dependencies.observe(repo, dependency)["fingerprint"]
            specs.append(dependency)
            graph["edges"].append({"id": identity, "source": source, "target": target, "dependencies": [identity],
                                   "rationale": "Synthetic client construction binds this connection and schema.", "confidence": "high"})
    results.check("[privacy-datamap] generic graph validates evidence-backed connection bindings", not workflow.relationships.validate(graph, specs))
    manifest = repo / ".noru" / "privacy-datamap.yml"
    manifest.parent.mkdir(exist_ok=True)
    document = {"relationship_graph": graph, "evidence_dependencies": specs}
    manifest.write_text(workflow.to_yaml(document))
    summary, derived = datamap_scan(repo)
    keys = {d["fides_key"] for d in derived["datasets"]}
    results.check("[privacy-datamap] shared schema preserves two unresolved connection identities",
                  keys == {"connection_primary_connection", "connection_secondary_connection"}
                  and len(derived["relationship_mapping"]["questions"]) == 2, str(keys) + str(derived["relationship_mapping"]["questions"]))
    results.check("[privacy-datamap] explicit runtime client flows survive a cross-directory schema",
                  set(derived["systems"][0]["dataset_references"]) == keys)
    graph["nodes"].reverse()
    graph["edges"].reverse()
    manifest.write_text(workflow.to_yaml(document))
    repeated, _ = datamap_scan(repo)
    results.check("[privacy-datamap] graph order does not change structural identity", summary["derived_digest"] == repeated["derived_digest"])
    broken = json.loads(json.dumps(graph))
    broken["edges"][0]["target"] = "missing"
    results.check("[privacy-datamap] graph rejects dangling endpoints", bool(workflow.relationships.validate(broken, specs)))
    broken = json.loads(json.dumps(graph))
    broken["edges"][0]["dependencies"] = []
    results.check("[privacy-datamap] graph rejects unmonitored relationship claims", bool(workflow.relationships.validate(broken, specs)))
    (repo / "src/connect.py").write_text('PRIMARY = "changed"\nSECONDARY = "second"\n')
    _, changes = workflow.dependencies.reconcile(repo, specs, {})
    results.check("[privacy-datamap] relationship source changes queue affected edges",
                  len(changes) == 6 and all(c["action"] == "investigate" and c["targets"][0].startswith("relationship:") for c in changes))
    graph["nodes"].append({"id": "destination", "kind": "datastore", "key": "shared_store"})
    for name in ("primary", "secondary"):
        node = next(n for n in graph["nodes"] if n["id"] == name + "_connection")
        node.pop("unresolved_question")
        node.pop("resolution_needed")
        identity = name + "_destination"
        dependency = {"id": identity, "path": "src/connect.py", "method": "python_ast", "targets": ["relationship:" + identity]}
        dependency["fingerprint"] = workflow.dependencies.observe(repo, dependency)["fingerprint"]
        specs.append(dependency)
        graph["edges"].append({"id": identity, "source": name + "_connection", "target": "destination", "dependencies": [identity],
                               "rationale": "Both synthetic connections explicitly use the same database.", "confidence": "high"})
    manifest.write_text(workflow.to_yaml(document))
    _, shared = datamap_scan(repo)
    results.check("[privacy-datamap] shared destination requires explicit connection edges",
                  [d["fides_key"] for d in shared["datasets"]] == ["shared_store"] and not shared["relationship_mapping"]["questions"], str([d["fides_key"] for d in shared["datasets"]]) + json.dumps(shared["relationship_mapping"]))
    (repo / "models/schema.sql").unlink()
    (repo / "models/schema.ts").write_text('export const accounts = pgTable("accounts", {\n  email: text("email"),\n});\n')
    next(n for n in graph["nodes"] if n["id"] == "schema")["paths"] = ["models/schema.ts"]
    (repo / "drizzle.config.ts").write_text('export default { schema: "./models/*.ts", out: "./migrations" };\n')
    (repo / "migrations").mkdir()
    (repo / "migrations/001.sql").write_text('CREATE TABLE accounts (\n  legacy_email TEXT\n);\n')
    manifest.write_text(workflow.to_yaml(document))
    _, migrated = datamap_scan(repo)
    results.check("[privacy-datamap] explicit bindings retain linked migrations as history of the same store",
                  [d["fides_key"] for d in migrated["datasets"]] == ["shared_store"]
                  and set(fields_of(migrated, "accounts")) == {"email"})
    (repo / "models/other.ts").write_text('export const events = pgTable("events", {\n  id: text("id"),\n});\n')
    _, incomplete = datamap_scan(repo)
    results.check("[privacy-datamap] mixed migration connection bindings expose a coverage gap",
                  any(g["format"] == "relationship_migrations" for g in incomplete["coverage"]["unparsed_candidates"]))
    graph["nodes"].append({"id": "second_destination", "kind": "datastore", "key": "another_store"})
    edge = dict(graph["edges"][-1], id="conflict", target="second_destination", dependencies=["conflict"])
    dependency = dict(specs[-1], id="conflict", targets=["relationship:conflict"])
    results.check("[privacy-datamap] a connection cannot silently combine multiple destinations",
                  bool(workflow.relationships.validate({"nodes": graph["nodes"], "edges": graph["edges"] + [edge]}, specs + [dependency])))


def test_datamap_repeatable_evidence_baseline(results, tmp):
    scripts = PRIVACY_DATAMAP / "scripts"
    spec = importlib.util.spec_from_file_location("repeatable_datamap", scripts / "reconcile.py")
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)
    source = 'def serialize(value):\n    return {"account": value}\n\ndef unrelated():\n    return 1\n'
    repo = write_files(pathlib.Path(tmp) / "repeatable-evidence", {"db/schema.sql": SQL_FIXTURE, "src/payload.py": source})
    summary, derived = datamap_scan(repo)
    manifest_path = repo / ".noru" / "privacy-datamap.yml"
    cache = repo / ".noru" / ".cache"
    lock_path = repo / ".noru" / "privacy-datamap.lock.json"
    dependency = {"id": "account-payload", "path": "src/payload.py", "method": "python_ast", "selector": "serialize",
                  "targets": ["db/accounts/weird_column", "system:repository"]}
    dependency["fingerprint"] = workflow.dependencies.observe(repo, dependency)["fingerprint"]
    document = workflow.VALIDATOR.load_yaml(accepted_datamap_text(summary["derived_digest"]))[0]
    graph = {"nodes": [
        {"id": "runtime", "kind": "runtime", "key": "repository"},
        {"id": "client", "kind": "client"}, {"id": "connection", "kind": "connection"},
        {"id": "datastore", "kind": "datastore", "key": "db"},
        {"id": "schema", "kind": "schema", "paths": ["db/schema.sql"]},
    ], "edges": []}
    for source_id, target_id in (("runtime", "client"), ("client", "connection"), ("connection", "datastore"), ("client", "schema")):
        identity = source_id + "_" + target_id
        dependency["targets"].append("relationship:" + identity)
        graph["edges"].append({"id": identity, "source": source_id, "target": target_id,
                               "dependencies": [dependency["id"]], "rationale": "Reviewed synthetic fixture binding.", "confidence": "high"})
    dependency["targets"].sort()
    document["relationship_graph"] = graph
    document["evidence_dependencies"] = [dependency]
    manifest_path.write_text(workflow.to_yaml(document))
    command = ["python3", str(scripts / "reconcile.py"), f"--repo={repo}", "--output=json", "--quiet"]
    sealed = run(command + ["--seal"])
    results.check("[privacy-datamap] sealing records reviewed code dependencies and runtime topology", sealed.returncode == 0, sealed.stderr)
    baseline_lock = lock_path.read_bytes()
    baseline_manifest = manifest_path.read_bytes()
    before = json.loads(run(command).stdout)
    proposal_path = cache / "privacy-datamap.proposals.json"
    proposals = json.loads(proposal_path.read_text())
    proposals["store_investigation"]["rationale"] = "Completed analysis is retained on identical input."
    proposal_path.write_text(json.dumps(proposals))
    datamap_scan(repo)
    after = json.loads(run(command).stdout)
    results.check("[privacy-datamap] repeated scans preserve the comparison and completed proposal cache",
                  before == after and json.loads(proposal_path.read_text()) == proposals)
    results.check("[privacy-datamap] accepted unchanged evidence requires no investigation", not after["drift"] and not after["blocked"] and not after["agent_required"])
    accepted_review = run(["python3", str(scripts / "review.py"), f"--repo={repo}", "--output=json"])
    results.check("[privacy-datamap] unchanged accepted scans need neither fresh enrichment nor human review",
                  accepted_review.returncode == 0 and json.loads(accepted_review.stdout)["status"] == "accepted", accepted_review.stderr or accepted_review.stdout)

    # Schema citation movement, AST formatting and unrelated code do not alter observed meaning.
    (repo / "db/schema.sql").write_text("\n\n" + SQL_FIXTURE)
    (repo / "src/payload.py").write_text('# comment\n\ndef serialize( value ):\n    return { "account" : value }\n\ndef unrelated():\n    return 999\n')
    (repo / "README.md").write_text("Unrelated documentation.\n")
    moved_summary, moved_derived = datamap_scan(repo)
    moved = json.loads(run(command).stdout)
    results.check("[privacy-datamap] formatting, line movement, unrelated code and file counts do not reopen decisions",
                  moved_summary["derived_digest"] == summary["derived_digest"] and not moved["drift"]
                  and [row["path"] for row in moved["discovery_required"]] == ["src/payload.py"]
                  and not moved["proposal_required"] and not moved["investigation_required"])
    results.check("[privacy-datamap] moved evidence citations refresh mechanically",
                  moved["dependency_changes"][0]["action"] == "refresh_evidence" and moved["counts"]["citation_only"] == 5)
    reordered = json.loads(json.dumps(moved_derived))
    reordered["datasets"].reverse()
    for dataset in reordered["datasets"]:
        dataset["collections"].reverse()
        for collection in dataset["collections"]:
            collection["fields"].reverse()
    data = pathlib.Path(tmp) / "reordered-datamap.json"
    data.write_text(json.dumps(reordered))
    js = f'import {{digestOf}} from {json.dumps((scripts / "collect.mjs").as_uri())}; import {{readFileSync}} from "node:fs"; console.log(digestOf(JSON.parse(readFileSync({json.dumps(str(data))}, "utf8"))));'
    results.check("[privacy-datamap] structural digest ignores enumeration order", run(["node", "--input-type=module", "-e", js]).stdout.strip() == summary["derived_digest"])
    parsed_path = cache / "privacy-datamap.parsed.json"
    valid = run(["python3", str(scripts / "validate_manifest.py"), str(manifest_path), f"--emit-parsed={parsed_path}", "--quiet"])
    results.check("[privacy-datamap] unregistered changed code blocks export without reopening known field decisions", valid.returncode == 1)
    (repo / "src/payload.py").write_text("# moved citation\n" + source)
    datamap_scan(repo)
    valid = run(["python3", str(scripts / "validate_manifest.py"), str(manifest_path), f"--emit-parsed={parsed_path}", "--quiet"])
    results.check("[privacy-datamap] unchanged evidence validates before export", valid.returncode == 0, valid.stderr)
    (repo / "src/payload.py").write_text(source.replace('"account"', '"external_account"'))
    code_summary, _ = datamap_scan(repo)
    changed = json.loads(run(command).stdout)
    results.check("[privacy-datamap] processing changes are scoped investigations rather than confirmed privacy changes",
                  not changed["drift"] and changed["blocked"] and changed["agent_required"]
                  and changed["investigation_required"][0]["targets"] == dependency["targets"]
                  and changed["investigation_required"][0]["previous"]["fingerprint"] == dependency["fingerprint"]
                  and changed["investigation_required"][0]["current"]["fingerprint"] != dependency["fingerprint"])
    results.check("[privacy-datamap] changed evidence cannot render an old parsed cache or seal unchanged decisions",
                  code_summary["rendered"] is None and run(command + ["--seal"]).returncode == 1)
    ci = run(["python3", str(ROOT / "scripts/ci_check.py"), "--piece=privacy-datamap", f"--repo={repo}", "--as-of=2026-09-08", "--output=json", "--quiet"])
    findings = json.loads(ci.stdout)["findings"]
    results.check("[privacy-datamap] CI blocks evidence investigations without labeling them schema drift",
                  ci.returncode != 0 and any(row["kind"] == "invalid" for row in findings) and not any(row["kind"] == "drift" for row in findings), ci.stdout[:300])
    results.check("[privacy-datamap] investigation never modifies accepted files", lock_path.read_bytes() == baseline_lock and manifest_path.read_bytes() == baseline_manifest)
    # An explicit reviewed fixture update establishes the next baseline, not an agent proposal.
    document["evidence_dependencies"][0]["fingerprint"] = workflow.dependencies.observe(repo, dependency)["fingerprint"]
    manifest_path.write_text(workflow.to_yaml(document))
    accepted = run(command + ["--seal"])
    accepted_result = json.loads(run(command).stdout)
    results.check("[privacy-datamap] accepting the reviewed fingerprint clears the next scan", accepted.returncode == 0 and not accepted_result["blocked"] and not accepted_result["drift"], accepted.stderr)
    # Unique exact evidence continuity is a refresh; multiple possible destinations are not guessed.
    path = repo / "src/payload.py"
    path.rename(repo / "src/moved.py")
    datamap_scan(repo)
    renamed = json.loads(run(command).stdout)
    candidate = load_datamap_yaml(cache / "privacy-datamap.candidate.yml")
    results.check("[privacy-datamap] unique code moves preserve decisions and refresh the candidate path",
                  not renamed["blocked"] and not renamed["drift"] and candidate["evidence_dependencies"][0]["path"] == "src/moved.py")
    (repo / "src/duplicate.py").write_text((repo / "src/moved.py").read_text())
    ambiguous = json.loads(run(command).stdout)
    results.check("[privacy-datamap] ambiguous evidence moves require investigation", ambiguous["blocked"] and ambiguous["investigation_required"][0]["action"] == "identity_ambiguity")
    (repo / "src/duplicate.py").unlink()
    (repo / "src/moved.py").rename(path)
    shape_js = f'import {{normalizeShape}} from {json.dumps((scripts / "collect.mjs").as_uri())}; console.log(JSON.stringify([normalizeShape(\'text ( ) . notNull ( )\'), normalizeShape(\'text().notNull()\'), normalizeShape(\'text().default("two  spaces")\'), normalizeShape(\'text().default("two spaces")\')]));'
    shapes = json.loads(run(["node", "--input-type=module", "-e", shape_js]).stdout)
    results.check("[privacy-datamap] shape normalization ignores punctuation spacing but preserves literal values",
                  shapes[0] == shapes[1] and shapes[2] != shapes[3])
    # JSON formatting is structural; changing a configuration value is observable.
    config = repo / "settings.json"
    config.write_text('{"provider": "archive", "training": false}')
    config_spec = {"path": "settings.json", "method": "json", "targets": ["system:repository"]}
    first = workflow.dependencies.observe(repo, config_spec)["fingerprint"]
    config.write_text('{\n "training": false, "provider": "archive"\n}')
    same = workflow.dependencies.observe(repo, config_spec)["fingerprint"]
    config.write_text('{"provider": "archive", "training": true}')
    results.check("[privacy-datamap] JSON key order and formatting are ignored but configuration values are tracked",
                  first == same and same != workflow.dependencies.observe(repo, config_spec)["fingerprint"])
    # Collector changes and taxonomy changes get their own maintenance classification.
    lock = json.loads(lock_path.read_text())
    lock["taxonomy_digest"] = "0" * 64
    lock_path.write_text(json.dumps(lock))
    taxonomy = json.loads(run(command).stdout)
    results.check("[privacy-datamap] taxonomy changes are separate from confirmed repository drift",
                  not taxonomy["drift"] and taxonomy["blocked"] and any(row["kind"] == "taxonomy_changed" for row in taxonomy["control_changes"]))
    lock["taxonomy_digest"] = workflow.taxonomy_digest()
    lock["dependencies"]["account-payload"]["normalizer"] = "python_ast:previous"
    lock_path.write_text(json.dumps(lock))
    upgrade = json.loads(run(command).stdout)
    results.check("[privacy-datamap] evidence normalizer upgrades are maintenance rather than code investigations",
                  not upgrade["drift"] and not upgrade["investigation_required"] and any(row["kind"] == "evidence_normalizer_changed" for row in upgrade["control_changes"]))
    lock["dependencies"]["account-payload"]["normalizer"] = workflow.dependencies.observe(repo, dependency)["normalizer"]
    lock_path.write_text(json.dumps(lock))
    (repo / "db/schema.sql").write_text(SQL_FIXTURE.replace("weird_column  TEXT,", "weird_column  JSON,"))
    stale_seal = run(command + ["--seal"])
    structural = json.loads(run(command).stdout)
    results.check("[privacy-datamap] seal recollects live schemas and real shape changes include before and after",
                  stale_seal.returncode == 1 and structural["drift"] and any(row["action"] == "material_change" and row["previous"]["shape"] != row["current"]["shape"] for row in structural["actions"]))
    (repo / "db/schema.sql").write_text(SQL_FIXTURE)
    (repo / "db/oversized.sql").write_text(" " * 1_000_001)
    _, oversized = datamap_scan(repo)
    results.check("[privacy-datamap] oversized schema files are explicit coverage gaps",
                  any(row["ref"] == "db/oversized.sql:1" for row in oversized["coverage"]["unparsed_candidates"]))
    (repo / "db/oversized.sql").unlink()
    (repo / "src/unsupported.ts").write_text('const records = new mongoose.Schema({ label: String });\n')
    datamap_scan(repo)
    coverage = json.loads(run(command).stdout)
    results.check("[privacy-datamap] missing structural coverage blocks a clean result and cannot be sealed away",
                  coverage["blocked"] and bool(coverage["coverage_gaps"]["unparsed_candidates"]) and run(command + ["--seal"]).returncode == 1)


def test_datamap_reconciles_only_the_privacy_delta(results, tmp):
    """The agent queue is selected by facts, not by a fresh model pass over the repository."""
    repo = write_files(pathlib.Path(tmp) / "privacy-reconcile", {"db/schema.sql": SQL_FIXTURE})
    summary, _derived = datamap_scan(repo)
    reconcile = PRIVACY_DATAMAP / "scripts" / "reconcile.py"

    bootstrap = run(
        ["python3", str(reconcile), f"--repo={repo}", "--output=json", "--quiet"]
    )
    if not results.check(
        "[privacy-datamap] a first scan enters bootstrap mode",
        bootstrap.returncode == 0 and json.loads(bootstrap.stdout)["mode"] == "bootstrap",
        (bootstrap.stderr or bootstrap.stdout)[:300],
    ):
        return
    bootstrap_payload = json.loads(bootstrap.stdout)
    results.check(
        "[privacy-datamap] bootstrap sends every context-dependent field to the agent",
        [row["field"] for row in bootstrap_payload["proposal_required"]]
        == ["id", "weird_column"],
        bootstrap_payload["proposal_required"],
    )

    manifest = repo / ".noru" / "privacy-datamap.yml"
    scan_state = json.loads(
        (repo / ".noru" / ".cache" / "privacy-datamap.scan.json").read_text(encoding="utf-8")
    )
    manifest.write_text(
        accepted_datamap_text(scan_state["legacy_derived_digest"]), encoding="utf-8"
    )
    migrated = run(
        ["python3", str(reconcile), f"--repo={repo}", "--output=json", "--quiet"]
    )
    migrated_payload = json.loads(migrated.stdout)
    results.check(
        "[privacy-datamap] a reviewed pre-lock manifest migrates without agent reclassification",
        migrated.returncode == 0
        and migrated_payload["mode"] == "migration"
        and migrated_payload["counts"]["proposal_required"] == 0,
        migrated_payload,
    )

    manifest.write_text(accepted_datamap_text(summary["derived_digest"]), encoding="utf-8")
    datamap_scan(repo)
    sealed = run(
        ["python3", str(reconcile), f"--repo={repo}", "--seal", "--output=json", "--quiet"]
    )
    results.check(
        "[privacy-datamap] a current valid manifest can seal the accepted observation lock",
        sealed.returncode == 0 and (repo / ".noru" / "privacy-datamap.lock.json").is_file(),
        (sealed.stderr or sealed.stdout)[:300],
    )
    lock_document = json.loads(
        (repo / ".noru" / "privacy-datamap.lock.json").read_text(encoding="utf-8")
    )
    lock_schema = json.loads(
        (ROOT / "contract" / "privacy-datamap-lock.schema.json").read_text(encoding="utf-8")
    )
    results.check(
        "[privacy-datamap] the sealed lock satisfies its public contract",
        validate_json_schema(lock_document, lock_schema, lock_schema) == [],
        validate_json_schema(lock_document, lock_schema, lock_schema),
    )

    unchanged = run(
        ["python3", str(reconcile), f"--repo={repo}", "--output=json", "--quiet"]
    )
    unchanged_payload = json.loads(unchanged.stdout)
    results.check(
        "[privacy-datamap] an unchanged accepted repository schedules no agent work",
        unchanged.returncode == 0
        and unchanged_payload["counts"]["unchanged"] == 5
        and unchanged_payload["counts"]["proposal_required"] == 0,
        unchanged_payload["counts"],
    )

    # Citation movement preserves the structural digest and requires no model reinterpretation.
    (repo / "db" / "schema.sql").write_text("\n" + SQL_FIXTURE, encoding="utf-8")
    datamap_scan(repo)
    citation = run(
        ["python3", str(reconcile), f"--repo={repo}", "--output=json", "--quiet"]
    )
    citation_payload = json.loads(citation.stdout)
    results.check(
        "[privacy-datamap] line movement is citation-only and invokes no agent",
        citation.returncode == 0
        and citation_payload["counts"]["citation_only"] == 5
        and citation_payload["counts"]["proposal_required"] == 0,
        citation_payload["counts"],
    )

    materially_changed = SQL_FIXTURE.replace("weird_column  TEXT,", "weird_column  JSON,")
    (repo / "db" / "schema.sql").write_text(materially_changed, encoding="utf-8")
    datamap_scan(repo)
    material = run(
        ["python3", str(reconcile), f"--repo={repo}", "--output=json", "--quiet"]
    )
    material_payload = json.loads(material.stdout)
    results.check(
        "[privacy-datamap] a changed ambiguous field alone returns to the agent",
        material.returncode == 0
        and material_payload["counts"]["materially_changed"] == 1
        and [row["field"] for row in material_payload["proposal_required"]]
        == ["weird_column"],
        material_payload,
    )

    changed = SQL_FIXTURE.replace(
        "weird_column  TEXT,",
        "weird_column  TEXT,\n    phone_number TEXT,\n    profile_notes TEXT,",
    )
    (repo / "db" / "schema.sql").write_text(changed, encoding="utf-8")
    datamap_scan(repo)
    delta = run(
        ["python3", str(reconcile), f"--repo={repo}", "--output=json", "--quiet"]
    )
    delta_payload = json.loads(delta.stdout)
    results.check(
        "[privacy-datamap] an exact addition is deterministic and an ambiguous addition alone reaches the agent",
        delta.returncode == 0
        and delta_payload["counts"]["deterministically_classified"] == 1
        and [row["field"] for row in delta_payload["proposal_required"]] == ["profile_notes"],
        delta_payload,
    )
    results.check(
        "[privacy-datamap] a structural delta invalidates only its collection sign-off",
        delta_payload["collection_review_required"] == ["db/accounts"],
        delta_payload["collection_review_required"],
    )
    proposals_document = json.loads(
        (repo / ".noru" / ".cache" / "privacy-datamap.proposals.json").read_text(
            encoding="utf-8"
        )
    )
    proposals_schema = json.loads(
        (ROOT / "contract" / "privacy-datamap-proposals.schema.json").read_text(
            encoding="utf-8"
        )
    )
    results.check(
        "[privacy-datamap] the bounded agent queue satisfies its public contract",
        validate_json_schema(proposals_document, proposals_schema, proposals_schema) == [],
        validate_json_schema(proposals_document, proposals_schema, proposals_schema),
    )
    review_report = (
        repo / ".noru" / ".cache" / "privacy-datamap.review.md"
    ).read_text(encoding="utf-8")
    results.check(
        "[privacy-datamap] proposal review is compact and grouped by collection and field family",
        "## db / accounts" in review_report
        and "content and structured values" in review_report
        and "profile_notes" in review_report
        and "observation_digest" not in review_report
        and "shape" not in review_report,
        review_report[:1000],
    )


def test_datamap_bootstrap_ignores_invalid_manifest_baseline(results, tmp):
    repo = write_files(pathlib.Path(tmp) / "bootstrap-baseline", {"db/schema.sql": SQL_FIXTURE})
    summary, _derived = datamap_scan(repo)
    manifest = repo / ".noru" / "privacy-datamap.yml"
    manifest.write_text(
        f"""version: 0.8.1
piece: privacy-datamap
source:
  slug: fixture/bootstrap
  commit_sha: 4f3c1a9e77b2d5c8a10e6b4f2d9c3a71e5b80d64
  branch: main
  generated_by: privacy-datamap@0.8.1
  derived_digest: {summary["derived_digest"]}
dataset:
  - fides_key: db
    name: contaminated dataset name
    description: UNTRUSTED_DATASET_DESCRIPTION
    collections:
      - name: accounts
        description: UNTRUSTED_COLLECTION_DESCRIPTION
        refs: ["old/schema.sql:1"]
        structure_digest: {"0" * 64}
        fields: []
system:
  - fides_key: repository
    name: contaminated system name
    description: UNTRUSTED_SYSTEM_DESCRIPTION
    system_type: Third Party
    dataset_references: [stale_dataset]
    privacy_declarations:
      - name: UNTRUSTED_DECLARATION
        data_use: essential.service
        data_subjects: [customer]
        data_categories: [user.contact.email]
        refs: ["old/service.ts:1"]
""",
        encoding="utf-8",
    )
    reconcile = PRIVACY_DATAMAP / "scripts" / "reconcile.py"
    completed = run(
        ["python3", str(reconcile), f"--repo={repo}", "--output=json", "--quiet"]
    )
    payload = json.loads(completed.stdout)
    candidate_path = repo / ".noru" / ".cache" / "privacy-datamap.candidate.yml"
    candidate_text = candidate_path.read_text(encoding="utf-8")
    candidate = load_datamap_yaml(candidate_path)
    declaration = candidate["system"][0]["privacy_declarations"][0]
    results.check(
        "[privacy-datamap] bootstrap candidate ignores every invalid manifest semantic",
        completed.returncode == 0
        and payload["mode"] == "bootstrap"
        and "UNTRUSTED_" not in candidate_text
        and "old/" not in candidate_text
        and "stale_dataset" not in candidate_text
        and candidate["dataset"][0]["name"] == "db"
        and "description" not in candidate["dataset"][0]
        and "description" not in candidate["dataset"][0]["collections"][0]
        and candidate["system"][0]["name"] == "repository"
        and candidate["system"][0]["system_type"] == "Application"
        and candidate["system"][0]["dataset_references"] == ["db"]
        and declaration["name"] == ""
        and declaration["needs_review"] is True,
        candidate_text[:2000],
    )


def test_datamap_normalizes_logical_topology(results, tmp):
    """Files are evidence for logical datastores and runtimes, not topology nodes themselves."""
    files = {
        "apps/gateway/package.json": json.dumps(
            {"scripts": {"start": "node src/server.js"}, "main": "src/server.js"}
        ),
        "apps/gateway/src/server.js": "import http from 'node:http'\nhttp.createServer(() => {}).listen(3000)\n",
        "apps/jobs/package.json": json.dumps(
            {"scripts": {"worker": "node src/worker.js"}, "main": "src/worker.js"}
        ),
        "apps/jobs/src/worker.js": "export async function runJobs() { return true }\n",
        "packages/helpers/package.json": json.dumps({"name": "helpers", "main": "index.js"}),
        "packages/helpers/index.js": "export const add = (a, b) => a + b\n",
        "packages/storage/schema/accounts.ts": (
            'export const accounts = pgTable("accounts", {\n'
            '  id: uuid("id").primaryKey(),\n'
            '  email: text("email").notNull(),\n'
            '  preferences: jsonb("preferences").$type<{ locale: string, flags: string[] }>(),\n'
            '})\n'
        ),
        "packages/storage/schema/events.ts": (
            'export const events = mysqlTable(\n  "events",\n  {\n'
            '    id: int("id").primaryKey(),\n    ip_address: varchar("ip_address", { length: 64 }),\n'
            '  },\n)\n'
        ),
        "packages/storage/migrations/001_create.sql": (
            "CREATE TABLE accounts (\n  id UUID PRIMARY KEY,\n  legacy_email TEXT\n);\n"
        ),
        "packages/storage/migrations/002_alter.sql": (
            "ALTER TABLE accounts ADD COLUMN email TEXT;\n"
        ),
    }
    derived, _repo = datamap_repo(tmp, "logical-topology", files)
    results.check(
        "[privacy-datamap] schema files sharing a boundary become one logical dataset",
        len(derived["datasets"]) == 1
        and derived["datasets"][0]["name"] == "packages/storage"
        and {c["name"] for c in derived["datasets"][0]["collections"]} == {"accounts", "events"},
        derived["datasets"],
    )
    results.check(
        "[privacy-datamap] canonical Drizzle structure supersedes migration history",
        set(fields_of(derived, "accounts")) == {"id", "email", "preferences"}
        and "legacy_email" not in fields_of(derived, "accounts"),
        fields_of(derived, "accounts"),
    )
    results.check(
        "[privacy-datamap] only the two runnable packages become systems",
        [system["name"] for system in derived["systems"]] == ["apps/gateway", "apps/jobs"]
        and any(
            item["kind"] == "executable_script"
            for system in derived["systems"]
            if system["name"] == "apps/jobs"
            for item in system["runtime_evidence"]
        ),
        derived["systems"],
    )
    dataset_keys = {dataset["fides_key"] for dataset in derived["datasets"]}
    referenced_keys = {
        ref for system in derived["systems"] for ref in system["dataset_references"]
    }
    results.check(
        "[privacy-datamap] all runtime dataset references resolve and generated keys are unique",
        referenced_keys <= dataset_keys
        and len(dataset_keys) == len(derived["datasets"])
        and len({system["fides_key"] for system in derived["systems"]}) == len(derived["systems"]),
        {"datasets": dataset_keys, "references": referenced_keys},
    )
    results.check(
        "[privacy-datamap] the raw observations retain canonical and migration evidence",
        len(derived["observations"]) == 4
        and {item["role"] for item in derived["observations"]} == {"canonical", "migration"}
        and len(derived["migration_operations"]) == 2,
        derived["observations"],
    )
    all_refs = [ref for dataset in derived["datasets"] for collection in dataset["collections"] for field in collection["fields"] for ref in field["refs"]]
    results.check(
        "[privacy-datamap] normalized fields retain useful file-and-line evidence",
        all(re.match(r"^[^:]+:\d+$", ref) for ref in all_refs)
        and any(ref.startswith("packages/storage/schema/") for ref in all_refs),
        all_refs,
    )
    implementation = (PRIVACY_DATAMAP / "scripts" / "collect.mjs").read_text(encoding="utf-8")
    results.check(
        "[privacy-datamap] generic topology code contains no synthetic repository identifiers",
        not any(name in implementation for name in (
            "apps/gateway", "apps/jobs", "packages/storage", "packages/helpers",
            "apps/portal", "packages/persistence", "analytics",
        )),
        "a fixture-specific topology identifier leaked into the collector",
    )


def test_datamap_uses_drizzle_config_for_cross_directory_topology(results, tmp):
    repo = git_repo(
        pathlib.Path(tmp) / "drizzle-config-topology",
        {
            "apps/portal/drizzle.config.ts": (
                'import { defineConfig } from "drizzle-kit"\n'
                "export default defineConfig({\n"
                '  schema: "../../packages/persistence/src/schema/*",\n'
                '  out: "./generated-migrations",\n'
                "})\n"
            ),
            "packages/persistence/src/schema/accounts.ts": (
                'export const accounts = pgTable("accounts", {\n'
                '  id: uuid("id"),\n'
                '  email: text("email"),\n'
                "})\n"
            ),
            "packages/persistence/src/schema/projects.ts": (
                'export const projects = pgTable("projects", {\n'
                '  id: uuid("id"),\n'
                '  title: text("title"),\n'
                "})\n"
            ),
            "apps/portal/generated-migrations/001.sql": (
                "CREATE TABLE accounts (\n  id UUID,\n  legacy_contact TEXT\n);\n"
                "CREATE TABLE projects (\n  id UUID,\n  title TEXT\n);\n"
                "CREATE TABLE retired_records (\n  id UUID\n);\n"
            ),
            "analytics/schema/accounts.ts": (
                'export const accounts = pgTable("accounts", {\n'
                '  event_count: integer("event_count"),\n'
                "})\n"
            ),
        },
    )
    _summary, derived = datamap_scan(repo)
    datasets = {dataset["fides_key"]: dataset for dataset in derived["datasets"]}
    linked = datasets.get("packages_persistence_src", {})
    linked_collections = {
        collection["name"]: collection for collection in linked.get("collections", [])
    }
    results.check(
        "[privacy-datamap] tracked Drizzle config joins cross-directory schema and migration paths",
        set(datasets) == {"analytics", "packages_persistence_src"}
        and derived["datastore_links"] == [
            {
                "config_ref": "apps/portal/drizzle.config.ts:3",
                "boundary": "packages/persistence/src",
                "schema_patterns": ["packages/persistence/src/schema/*"],
                "output_path": "apps/portal/generated-migrations",
            }
        ],
        {"datasets": datasets, "links": derived["datastore_links"]},
    )
    results.check(
        "[privacy-datamap] cross-directory canonical Drizzle schema wins over migration history",
        set(linked_collections) == {"accounts", "projects"}
        and {field["name"] for field in linked_collections["accounts"]["fields"]}
        == {"id", "email"}
        and "retired_records" not in linked_collections,
        linked_collections,
    )
    results.check(
        "[privacy-datamap] table-name overlap never merges an unlinked datastore",
        "accounts" in {
            collection["name"] for collection in datasets["analytics"]["collections"]
        }
        and datasets["analytics"]["name"] == "analytics",
        datasets["analytics"],
    )
    linked_observations = {
        observation["path"]: (observation["boundary"], observation["role"])
        for observation in derived["observations"]
        if observation["path"].startswith("apps/portal/generated-migrations/")
        or observation["path"].startswith("packages/persistence/src/schema/")
    }
    results.check(
        "[privacy-datamap] linked migrations remain cited history under the canonical boundary",
        linked_observations
        == {
            "apps/portal/generated-migrations/001.sql": (
                "packages/persistence/src", "migration"
            ),
            "packages/persistence/src/schema/accounts.ts": (
                "packages/persistence/src", "canonical"
            ),
            "packages/persistence/src/schema/projects.ts": (
                "packages/persistence/src", "canonical"
            ),
        },
        linked_observations,
    )


def test_datamap_canonical_schema_makes_migration_replay_non_blocking(results, tmp):
    derived, _repo = datamap_repo(
        tmp,
        "canonical-with-unsupported-migration",
        {
            "db/schema/accounts.ts": (
                'export const accounts = pgTable("accounts", {\n'
                '  id: uuid("id"),\n'
                '  email: text("email"),\n'
                "})\n"
            ),
            "db/migrations/001.sql": (
                "CREATE TABLE accounts (\n  id UUID,\n  email TEXT\n);\n"
            ),
            "db/migrations/002.sql": (
                "ALTER TABLE accounts ALTER COLUMN email TYPE VARCHAR(320);\n"
            ),
        },
    )
    results.check(
        "[privacy-datamap] canonical-backed migration replay failures are not coverage gaps",
        derived["coverage"]["migration_gaps"] == []
        and set(fields_of(derived, "accounts")) == {"id", "email"},
        derived["coverage"],
    )
    results.check(
        "[privacy-datamap] canonical-backed migrations remain auditable history",
        any(
            operation["ref"] == "db/migrations/002.sql:1"
            for operation in derived["migration_operations"]
        ),
        derived["migration_operations"],
    )


def test_datamap_separates_datastores_and_falls_back_for_runtime(results, tmp):
    derived, _repo = datamap_repo(
        tmp,
        "separate-datastores",
        {
            "primary/schema/users.ts": 'export const users = pgTable("users", {\n  id: uuid("id"),\n})\n',
            "analytics/schema/events.ts": 'export const events = sqliteTable("events", {\n  id: integer("id"),\n})\n',
            "packages/utility/package.json": '{"name":"utility"}\n',
            "packages/utility/index.js": "export const value = 1\n",
            "tests/fixtures/workload.yaml": "kind: Deployment\nmetadata:\n  name: synthetic\n",
        },
    )
    keys = [dataset["fides_key"] for dataset in derived["datasets"]]
    refs = [ref for system in derived["systems"] for ref in system["dataset_references"]]
    results.check(
        "[privacy-datamap] genuinely separate schema boundaries remain separate datastores",
        keys == ["analytics", "primary"],
        keys,
    )
    results.check(
        "[privacy-datamap] a library package alone is not a system and root fallback references every dataset",
        [system["name"] for system in derived["systems"]] == ["repository"]
        and sorted(refs) == keys,
        derived["systems"],
    )


def test_datamap_runtime_discovery_ignores_test_files(results, tmp):
    derived, _repo = datamap_repo(
        tmp,
        "test-runtime-markers",
        {
            "db/schema.sql": SQL_FIXTURE,
            "__tests__/server.ts": "serve(() => 'test')\n",
            "src/__fixtures__/worker.ts": "new Worker('fixture.js')\n",
            "src/http.test.ts": "createServer(() => {}).listen(3000)\n",
            "src/http.spec.js": "app.listen(3001)\n",
        },
    )
    results.check(
        "[privacy-datamap] test and fixture runtime markers do not create systems",
        [system["name"] for system in derived["systems"]] == ["repository"]
        and [
            evidence["kind"]
            for evidence in derived["systems"][0]["runtime_evidence"]
        ]
        == ["repository_fallback"],
        derived["systems"],
    )


def test_datamap_replays_supported_migrations_and_reports_unsafe_ones(results, tmp):
    supported, _repo = datamap_repo(
        tmp,
        "migration-current-state",
        {
            "db/migrations/001.sql": (
                "CREATE TABLE people (\n  id INTEGER,\n  contact TEXT,\n  obsolete TEXT\n);\n"
                "CREATE TABLE discarded (\n  id INTEGER\n);\n"
            ),
            "db/migrations/002.sql": (
                "ALTER TABLE people ADD COLUMN email TEXT;\n"
                "ALTER TABLE people RENAME COLUMN contact TO phone_number;\n"
                "ALTER TABLE people DROP COLUMN obsolete;\n"
                "ALTER TABLE people RENAME TO customers;\n"
                "DROP TABLE discarded;\n"
            ),
        },
    )
    results.check(
        "[privacy-datamap] ordered migration replay produces only the current table and fields",
        [collection["name"] for collection in supported["datasets"][0]["collections"]] == ["customers"]
        and set(fields_of(supported, "customers")) == {"id", "phone_number", "email"},
        supported["datasets"],
    )
    results.check(
        "[privacy-datamap] renamed fields preserve their creation and rename citations",
        len(fields_of(supported, "customers")["phone_number"]["refs"]) == 2,
        fields_of(supported, "customers")["phone_number"],
    )

    constraints, _repo = datamap_repo(
        tmp,
        "migration-constraints",
        {
            "db/migrations/001.sql": (
                "CREATE TABLE audit_events (\n"
                "  operation TEXT,\n"
                "  related_record_id TEXT,\n"
                "  CHECK (\n"
                "    operation = 'created'\n"
                "    OR\n"
                "    (operation = 'updated' AND related_record_id IS NOT NULL)\n"
                "  )\n"
                ");\n"
                "--> statement-breakpoint\n"
                "ALTER TABLE audit_events ADD CONSTRAINT audit_events_operation_fk\n"
                "  FOREIGN KEY (related_record_id) REFERENCES records(id);\n"
            ),
        },
    )
    results.check(
        "[privacy-datamap] migration delimiters and field-neutral constraints create no coverage gaps",
        constraints["coverage"]["migration_gaps"] == [],
        constraints["coverage"],
    )
    results.check(
        "[privacy-datamap] multiline CHECK bodies do not become SQL columns",
        set(fields_of(constraints, "audit_events")) == {"operation", "related_record_id"},
        fields_of(constraints, "audit_events"),
    )

    unsafe, _repo = datamap_repo(
        tmp,
        "migration-gap",
        {
            "db/migrations/001.sql": "CREATE TABLE users (\n  id INTEGER,\n  email TEXT\n);\n",
            "db/migrations/002.sql": "ALTER TABLE users ALTER COLUMN email TYPE VARCHAR(320);\n",
        },
    )
    results.check(
        "[privacy-datamap] unsupported structural migration syntax is visible and no partial dataset is guessed",
        unsafe["datasets"] == []
        and len(unsafe["coverage"]["migration_gaps"]) == 1
        and unsafe["coverage"]["migration_gaps"][0]["ref"] == "db/migrations/002.sql:1",
        unsafe["coverage"],
    )
    conflict, _repo = datamap_repo(
        tmp,
        "schema-conflict",
        {
            "db/schema/one.ts": 'export const users = pgTable("users", {\n  value: text("value"),\n})\n',
            "db/schema/two.ts": 'export const users = pgTable("users", {\n  value: integer("value"),\n})\n',
        },
    )
    results.check(
        "[privacy-datamap] conflicting canonical field shapes are coverage, not a guessed merge",
        conflict["datasets"] == []
        and len(conflict["coverage"]["schema_conflicts"]) == 1,
        conflict["coverage"],
    )

    duplicate, _repo = datamap_repo(
        tmp,
        "migration-duplicate-table",
        {
            "db/migrations/001.sql": "CREATE TABLE records (\n  id INTEGER\n);\n",
            "db/migrations/002.sql": "CREATE TABLE records (\n  id INTEGER\n);\n",
        },
    )
    results.check(
        "[privacy-datamap] duplicate migration tables remain explicit coverage gaps",
        duplicate["datasets"] == []
        and [gap["reason"] for gap in duplicate["coverage"]["migration_gaps"]]
        == ["table 'records' already exists during replay"],
        duplicate["coverage"],
    )


def test_datamap_reads_evidence_backed_supplemental_stores(results, tmp):
    supplement = {
        "$schema": "https://raw.githubusercontent.com/noru-tech/noru-grc-engineering/v0/contract/privacy-datamap-stores.schema.json",
        "version": "1.0.0",
        "datastores": [
            {
                "fides_key": "customer_objects",
                "name": "Customer object storage",
                "store_type": "object_storage",
                "provider": "gcs",
                "refs": ["src/storage.ts:5"],
                "system_references": ["src"],
                "collections": [
                    {
                        "name": "uploaded_files",
                        "refs": ["src/storage.ts:1"],
                        "fields": [
                            {
                                "name": "object_key",
                                "shape": "string",
                                "evidence_kind": "typed_contract",
                                "refs": ["src/storage.ts:2"],
                            },
                            {
                                "name": "content_type",
                                "shape": "string",
                                "evidence_kind": "upload_payload",
                                "refs": ["src/storage.ts:3"],
                            },
                        ],
                    }
                ],
            }
        ],
    }
    schema = json.loads(
        (ROOT / "contract" / "privacy-datamap-stores.schema.json").read_text(encoding="utf-8")
    )
    results.check(
        "[privacy-datamap] the supplemental datastore fixture satisfies its public contract",
        validate_json_schema(supplement, schema, schema) == [],
        validate_json_schema(supplement, schema, schema),
    )
    derived, _repo = datamap_repo(
        tmp,
        "supplemental-store",
        {
            "src/storage.ts": (
                "export type StoredObject = {\n"
                "  object_key: string\n"
                "  content_type: string\n"
                "}\n"
                "const bucket = storage.bucket('uploads')\n"
                "export const upload = (value: StoredObject) => bucket.upload(value)\n"
                "serve(() => 'ok')\n"
            ),
            ".noru/privacy-datamap-stores.json": json.dumps(supplement, indent=2) + "\n",
        },
    )
    dataset = derived["datasets"][0]
    results.check(
        "[privacy-datamap] an evidence-backed non-schema store joins the logical topology",
        dataset["fides_key"] == "customer_objects"
        and dataset["store_type"] == "object_storage"
        and dataset["provider"] == "gcs"
        and set(fields_of(derived, "uploaded_files")) == {"object_key", "content_type"},
        dataset,
    )
    results.check(
        "[privacy-datamap] supplemental system references attach the store to discovered runtime evidence",
        derived["systems"][0]["fides_key"] == "src"
        and derived["systems"][0]["dataset_references"] == ["customer_objects"],
        derived["systems"],
    )

    sdk_only, _repo = datamap_repo(
        tmp,
        "object-sdk-only",
        {"src/storage.ts": "const bucket = storage.bucket('uploads')\n"},
    )
    results.check(
        "[privacy-datamap] a bucket client call alone never invents object fields or a dataset",
        sdk_only["datasets"] == [],
        sdk_only["datasets"],
    )

    collector = PRIVACY_DATAMAP / "scripts" / "collect.mjs"
    bad_citation = json.loads(json.dumps(supplement))
    bad_citation["datastores"][0]["collections"][0]["fields"][0]["refs"] = [
        "src/storage.ts:99"
    ]
    bad_repo = write_files(
        pathlib.Path(tmp) / "supplement-bad-citation",
        {
            "src/storage.ts": (
                "export type StoredObject = {\n"
                "  object_key: string\n"
                "  content_type: string\n"
                "}\n"
                "const bucket = storage.bucket('uploads')\n"
                "export const upload = (value: StoredObject) => bucket.upload(value)\n"
                "serve(() => 'ok')\n"
            ),
            ".noru/privacy-datamap-stores.json": json.dumps(bad_citation),
        },
    )
    bad_result = run(["node", str(collector), f"--repo={bad_repo}", "--quiet"])
    results.check(
        "[privacy-datamap] a supplemental field citation beyond the source file stops the scan",
        bad_result.returncode == 2 and "cites line 99" in bad_result.stderr,
        bad_result.stderr,
    )

    unknown_system = json.loads(json.dumps(supplement))
    unknown_system["datastores"][0]["system_references"] = ["missing_runtime"]
    unknown_repo = write_files(
        pathlib.Path(tmp) / "supplement-unknown-system",
        {
            "src/storage.ts": (
                "export type StoredObject = {\n"
                "  object_key: string\n"
                "  content_type: string\n"
                "}\n"
                "const bucket = storage.bucket('uploads')\n"
            ),
            ".noru/privacy-datamap-stores.json": json.dumps(unknown_system),
        },
    )
    unknown_result = run(["node", str(collector), f"--repo={unknown_repo}", "--quiet"])
    results.check(
        "[privacy-datamap] a supplemental link to an undiscovered runtime stops the scan",
        unknown_result.returncode == 2 and "references undiscovered system" in unknown_result.stderr,
        unknown_result.stderr,
    )

    tracked_repo = git_repo(
        pathlib.Path(tmp) / "supplement-untracked",
        {"src/storage.ts": "export const storage = true\n"},
        then={".noru/privacy-datamap-stores.json": json.dumps(supplement)},
    )
    untracked_result = run(["node", str(collector), f"--repo={tracked_repo}", "--quiet"])
    results.check(
        "[privacy-datamap] an untracked supplemental declaration stops a git-backed scan",
        untracked_result.returncode == 2 and "exists but is not tracked" in untracked_result.stderr,
        untracked_result.stderr,
    )


def test_datamap_keys_are_unique_or_fail_actionably(results, tmp):
    hidden, _repo = datamap_repo(
        tmp,
        "hidden-boundary",
        {".example/schema.sql": "CREATE TABLE records (\n  id INTEGER\n);\n"},
    )
    results.check(
        "[privacy-datamap] a hidden directory does not collapse to the repository key",
        [dataset["fides_key"] for dataset in hidden["datasets"]] == ["example"],
        hidden["datasets"],
    )
    repo = write_files(
        pathlib.Path(tmp) / "key-collision",
        {
            "a-b/schema.sql": "CREATE TABLE one (\n  id INTEGER\n);\n",
            "a_b/schema.sql": "CREATE TABLE two (\n  id INTEGER\n);\n",
        },
    )
    collector = PRIVACY_DATAMAP / "scripts" / "collect.mjs"
    collision = run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"])
    results.check(
        "[privacy-datamap] normalized key collisions fail before a manifest is written",
        collision.returncode == 2
        and "dataset key collision 'a_b'" in collision.stderr
        and not (repo / ".noru" / "privacy-datamap.yml").exists(),
        collision.stderr,
    )


def test_datamap_output_is_byte_deterministic(results, tmp):
    repo = write_files(
        pathlib.Path(tmp) / "deterministic-topology",
        {
            "db/schema/one.ts": 'export const one = pgTable("one", {\n  email: text("email"),\n})\n',
            "db/schema/two.ts": 'export const two = pgTable("two", {\n  id: uuid("id"),\n})\n',
        },
    )
    datamap_scan(repo)
    first = (repo / ".noru" / ".cache" / "privacy-datamap.derived.json").read_bytes()
    datamap_scan(repo)
    second = (repo / ".noru" / ".cache" / "privacy-datamap.derived.json").read_bytes()
    results.check(
        "[privacy-datamap] identical scans produce byte-identical normalized observations",
        first == second,
        hashlib.sha256(first).hexdigest() + " vs " + hashlib.sha256(second).hexdigest(),
    )


def test_datamap_reconciles_file_ids_to_logical_ids(results, tmp):
    repo = write_files(
        pathlib.Path(tmp) / "identity-migration",
        {"db/schema.ts": 'export const users = pgTable("users", {\n  id: uuid("id"),\n  email: text("email"),\n})\n'},
    )
    _summary, derived = datamap_scan(repo)
    current_fields = fields_of(derived, "users")
    digest = hashlib.sha256("email\nid".encode("utf-8")).hexdigest()
    manifest = repo / ".noru" / "privacy-datamap.yml"
    manifest.write_text(
        f"""version: 0.7.2
piece: privacy-datamap
source:
  slug: fixture/identity
  commit_sha: 4f3c1a9
  branch: main
  generated_by: privacy-datamap@0.7.2
dataset:
  - fides_key: db_migrations_001
    name: db/migrations/001.sql
    collections:
      - name: users
        refs: [\"db/migrations/001.sql:1\"]
        structure_digest: {digest}
        interpretation:
          owner: Dana Okafor
          decided_at: \"2026-08-20\"
          expires_at: \"2027-08-20\"
          rationale: Reviewed the synthetic user schema and its field classifications.
        fields:
          - name: email
            data_categories: [user.contact.email]
            refs: [\"db/migrations/001.sql:3\"]
          - name: id
            data_categories: []
            refs: [\"db/migrations/001.sql:2\"]
system:
  - fides_key: repository
    name: repository
    system_type: Application
    dataset_references: [db_migrations_001]
    privacy_declarations:
      - name: Operate accounts
        data_use: essential.service
        data_subjects: [customer]
        refs: [\"db/migrations/001.sql:1\"]
        interpretation:
          owner: Dana Okafor
          decided_at: \"2026-08-20\"
          expires_at: \"2027-08-20\"
          rationale: Account records support the synthetic service operation.
""",
        encoding="utf-8",
    )
    lock = {
        "version": "1.0.0",
        "piece": "privacy-datamap",
        "source": {"slug": "fixture/identity", "derived_digest": "0" * 64},
        "taxonomy_digest": "0" * 64,
        "accepted_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "collections": {
            "db_migrations_001/users": {
                "semantic_digest": "0" * 64,
                "refs": ["db/migrations/001.sql:1"],
            }
        },
        "entities": {
            f"db_migrations_001/users/{name}": {
                "kind": "field",
                "shape": "uuid" if name == "id" else "text",
                "semantic_digest": "0" * 64,
                "refs": [f"db/migrations/001.sql:{2 if name == 'id' else 3}"],
            }
            for name, field in current_fields.items()
        },
    }
    (repo / ".noru" / "privacy-datamap.lock.json").write_text(
        json.dumps(lock, indent=2) + "\n", encoding="utf-8"
    )
    reconcile = PRIVACY_DATAMAP / "scripts" / "reconcile.py"
    completed = run(["python3", str(reconcile), f"--repo={repo}", "--output=json", "--quiet"])
    payload = json.loads(completed.stdout)
    candidate = (repo / ".noru" / ".cache" / "privacy-datamap.candidate.yml").read_text(encoding="utf-8")
    results.check(
        "[privacy-datamap] unique evidence-supported file identities migrate without add/remove churn",
        completed.returncode == 0
        and payload["counts"]["identity_migrated"] == 2
        and payload["counts"]["added"] == 0
        and payload["counts"]["removed"] == 0,
        payload,
    )
    results.check(
        "[privacy-datamap] identity migration carries the accepted field classification",
        "fides_key: db\n" in candidate and "user.contact.email" in candidate,
        candidate[:1200],
    )
    lock["entities"]["db_migrations_002/users/email"] = {
        **lock["entities"]["db_migrations_001/users/email"],
        "refs": ["db/migrations/002.sql:3"],
    }
    (repo / ".noru" / "privacy-datamap.lock.json").write_text(
        json.dumps(lock, indent=2) + "\n", encoding="utf-8"
    )
    ambiguous = run(["python3", str(reconcile), f"--repo={repo}", "--output=json", "--quiet"])
    ambiguous_payload = json.loads(ambiguous.stdout)
    results.check(
        "[privacy-datamap] ambiguous identity migrations are surfaced rather than guessed",
        ambiguous.returncode == 0
        and ambiguous_payload["identity_ambiguities"]
        and ambiguous_payload["identity_ambiguities"][0]["entity_id"] == "db/users/email",
        ambiguous_payload.get("identity_ambiguities"),
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

    manifest = repo / ".noru" / "privacy-datamap.yml"
    collector = piece / "scripts" / "collect.mjs"
    digest = json.loads(
        run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"]).stdout
    )["derived_digest"]
    manifest.write_text(accepted_datamap_text(digest), encoding="utf-8")
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
        json.dumps(
            {
                "fetched_at": "2026-08-27T09:00:00Z",
                "connection": {
                    "organization": {"id": "org_fixture", "name": "Fixture Organization"},
                    "endpoint": "https://api.noru.tech/v1/mcp",
                    "scopes": ["*"],
                },
            }
        ),
        encoding="utf-8",
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



def test_scaffold_template_scans_what_ci_checks_out(results, tmp):
    """The collector every new piece is stamped from must not hand it this defect again.

    Three pieces walked the working tree behind a fixed denylist, and all three got it from
    `scripts/templates/collect.mjs.tmpl`. Fixing the three without fixing the template fixes
    nothing: the fourth piece scaffolded from it starts with the same disagreement between a
    developer's scan and CI's, and nothing in the contract test would notice.

    A template is not importable, so the only honest way to assert what a scaffolded piece does is
    to stamp one the way scaffold-piece.mjs does and run it.
    """
    collector = pathlib.Path(tmp) / "stamped-collect.mjs"
    collector.write_text(
        TEMPLATE_COLLECTOR.read_text(encoding="utf-8").replace("__PIECE__", "stamped"),
        encoding="utf-8",
    )

    def stamped_scan(repo):
        result = run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"])
        if result.returncode != 0:
            raise RuntimeError(
                f"stamped collector exited {result.returncode}: {result.stderr[:300]}"
            )
        derived = json.loads(
            (repo / ".noru" / ".cache" / "stamped.derived.json").read_text(encoding="utf-8")
        )
        return json.loads(result.stdout), derived

    tracked = {".gitignore": "worktrees/\n", "src/app.ts": "export const x = 1\n"}
    repo = git_repo(
        pathlib.Path(tmp) / "stamped-worktree",
        {**tracked, "worktrees/agent-1/src/app.ts": "export const x = 1\n"},
        then={"src/draft.ts": "export const y = 2\n"},
    )
    summary, derived = stamped_scan(repo)
    results.check(
        "[scaffold template] a scaffolded collector counts the tracked files, not the working tree",
        derived["files_scanned"] == len(tracked)
        and derived["coverage"].get("enumerated_by") == "git",
        f"{derived['files_scanned']} file(s), "
        f"enumerated_by={derived['coverage'].get('enumerated_by')}",
    )

    checkout = write_files(pathlib.Path(tmp) / "stamped-ci-checkout", tracked)
    ci_summary, ci_derived = stamped_scan(checkout)
    results.check(
        "[scaffold template] a working tree and a checkout of the same files agree on the digest",
        ci_summary["derived_digest"] == summary["derived_digest"],
        f"{summary['derived_digest'][:12]} vs {ci_summary['derived_digest'][:12]}",
    )
    results.check(
        "[scaffold template] a directory that is not a work tree falls back to reading the disk",
        ci_derived["coverage"].get("enumerated_by") == "walk",
        ci_derived["coverage"].get("enumerated_by"),
    )

    # test_digest_ignores_the_collectors_own_version asserts this for every *plugin*, and cannot
    # reach a template. The template stamped both `generated_by` and `coverage` into the hash, so a
    # piece scaffolded from it would have failed that test on the day it was added — and reported
    # drift in every repository that had already run :scan, on nothing but a version bump.
    probe = pathlib.Path(tmp) / "stamped-digest-probe.mjs"
    probe.write_text(
        "import { digestOf } from %r;\n"
        "const facts = { piece: 'x', generated_by: 'x@0.1.0', findings: [1, 2] };\n"
        "console.log(JSON.stringify({\n"
        "  version: digestOf(facts) === digestOf({ ...facts, generated_by: 'x@9.9.9' }),\n"
        "  absent: digestOf(facts) === digestOf({ piece: 'x', findings: [1, 2] }),\n"
        "  coverage:\n"
        "    digestOf(facts) === digestOf({ ...facts, coverage: { enumerated_by: 'walk' } }),\n"
        "}));\n" % str(collector),
        encoding="utf-8",
    )
    result = run(["node", str(probe)])
    if results.check(
        "[scaffold template] digestOf is callable", result.returncode == 0, result.stderr[:200]
    ):
        out = json.loads(result.stdout)
        results.check(
            "[scaffold template] the digest ignores the collector's own version",
            out["version"] and out["absent"],
            out,
        )
        results.check(
            "[scaffold template] and it ignores how the file list was enumerated",
            out["coverage"],
            out,
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


# Canned GitHub responses. Enough shape for the exporter to work through, and a 403 on branch
# protection — which is what the Actions token really gets, and what killed a whole export before
# `tolerate` existed.
GITHUB_ROUTES = {
    "/repos/o/r": (200, {"default_branch": "main"}),
    "/repos/o/r/branches/main/protection": (403, {"message": "Resource not accessible by integration"}),
    "/repos/o/r/contents/.github/CODEOWNERS": (200, {"name": "CODEOWNERS"}),
    "/repos/o/r/environments": (200, {"environments": []}),
    "/repos/o/r/deployments": (200, []),
    "/repos/o/r/pulls": (200, [{
        "number": 7,
        "title": "Add a thing",
        "user": {"login": "alice", "type": "User"},
        "created_at": "2026-07-02T09:00:00Z",
        "merged_at": "2026-07-03T09:00:00Z",
        "merged_by": {"login": "alice", "type": "User"},
        "merge_commit_sha": "deadbeefcafe",
        "html_url": "https://example/pull/7",
    }]),
    "/repos/o/r/pulls/7/reviews": (200, [
        {"user": {"login": "alice"}, "state": "APPROVED", "submitted_at": "2026-07-03T08:00:00Z"},
    ]),
}


def _serve(routes):
    """A stdlib HTTP server returning canned JSON. Returns (base_url, shutdown)."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            status, body = routes.get(path, (404, {"message": "Not Found"}))
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_port}", server.shutdown


def test_change_control_export_survives_a_forbidden_setting(results, tmp):
    """The exporters' HTTP layer, against a server that answers the way GitHub really does.

    docs/verification.md said these had never met a live forge, and the first run against one failed
    exactly where that gap predicted: branch protection answers **403** for a token that may not ask
    it, not 404, and a probe tolerating only 404 killed the whole export. The exporters take
    `--api=`, so the layer is testable without a forge, and this is that test.

    What it asserts is the honest-reporting rule the piece is built on: an unreadable setting is
    omitted, never reported as a false one. `protected: false` where the answer is "nobody could
    find out" is a wrong compliance claim, and worse than a missing one.
    """
    base, shutdown = _serve(GITHUB_ROUTES)
    out = pathlib.Path(tmp) / "export" / "change-events.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [
                "node",
                str(PLUGINS / "change-control" / "scripts" / "export" / "github.mjs"),
                "--repo=o/r", "--since=2026-07-01", "--until=2026-07-31",
                f"--out={out}", f"--api={base}", "--output=json", "--quiet",
            ],
            capture_output=True, text=True, check=False,
            env={**os.environ, "GITHUB_TOKEN": "not-a-real-token"},
        )
    finally:
        shutdown()

    if not results.check(
        "[change-control] a 403 on branch protection does not kill the export",
        completed.returncode == 0,
        completed.stderr[:400],
    ):
        return

    document = json.loads(out.read_text(encoding="utf-8"))
    settings = document["settings"]
    results.check(
        "[change-control] an unreadable setting is omitted, not reported false",
        "protected" not in settings and "enforce_admins" not in settings,
        json.dumps(settings),
    )
    results.check(
        "[change-control] what it could read is still recorded",
        settings["default_branch"] == "main" and settings["codeowners_present"] is True,
        json.dumps(settings),
    )
    change = document["changes"][0]
    results.check(
        "[change-control] the change itself came through",
        change["key"] == "pr-7" and change["authored_by"].startswith("alice@"),
        json.dumps(change)[:200],
    )
    results.check(
        "[change-control] a self-approval survives the round trip as data",
        change["approvals"][0]["by"] == change["authored_by"],
        json.dumps(change["approvals"]),
    )

    # And the other direction: a 403 on something the export cannot do without must still fail.
    broken = dict(GITHUB_ROUTES)
    broken["/repos/o/r/pulls"] = (403, {"message": "Resource not accessible by integration"})
    base, shutdown = _serve(broken)
    try:
        completed = subprocess.run(
            [
                "node",
                str(PLUGINS / "change-control" / "scripts" / "export" / "github.mjs"),
                "--repo=o/r", "--since=2026-07-01", "--until=2026-07-31",
                f"--out={out}", f"--api={base}", "--quiet",
            ],
            capture_output=True, text=True, check=False,
            env={**os.environ, "GITHUB_TOKEN": "not-a-real-token"},
        )
    finally:
        shutdown()
    results.check(
        "[change-control] a 403 on the pull requests themselves still fails",
        completed.returncode == 1,
        f"exit {completed.returncode}",
    )
    results.check(
        "[change-control] and the token is not echoed into the error",
        "not-a-real-token" not in completed.stderr,
        completed.stderr[:200],
    )


# --- entry points -------------------------------------------------------------------------------
#
# Every runnable script here ends with a guard that compares its own module URL against
# `process.argv[1]` and calls main() only if they match. Both sides have to be reduced to the same
# form first: `import.meta.url` is always the realpath, URL-encoded, while `process.argv[1]` is the
# path as it was typed. A guard that compares them raw is false whenever the two differ, and a
# collector whose guard is false exits 0 having scanned nothing — which is indistinguishable, to a
# CI job and to the person reading its log, from a clean scan.

ENTRY_POINT_GUARD = (
    "return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;"
)


def entry_points():
    """Every script that decides whether to run main() by comparing its URL against argv[1]."""
    for root in (PLUGINS, ROOT / "scripts" / "templates"):
        for path in sorted(root.rglob("*")):
            if path.suffix not in (".mjs", ".tmpl") or not path.is_file():
                continue
            if "import.meta.url ===" in path.read_text(encoding="utf-8"):
                yield path


def test_entry_point_runs_from_a_symlinked_path(results, tmp):
    """A collector reached through a symlink must still collect.

    `/tmp` and `/var` are symlinks on macOS, so a CI job or a developer running a collector from a
    temporary directory reaches it through one by default. Node resolves `import.meta.url` to the
    realpath while `process.argv[1]` keeps the symlinked spelling, so an unresolved guard compares
    `file:///private/tmp/...` against `file:///tmp/...`, never matches, and main() does not run.

    The exit code cannot detect this: the script falls off the end and exits 0. So this asserts on
    what a scan is *for* — the derived facts — rather than on the status.
    """
    # The whole plugin is linked, not just the script: the collector loads its own siblings
    # (references/, lib/) relative to itself, and linking deeper would test a different thing.
    link = pathlib.Path(tmp) / "linked-plugin"
    os.symlink(PRIVACY_DATAMAP, link)
    repo = write_files(pathlib.Path(tmp) / "symlinked-entry-point", {"db/schema.sql": SQL_FIXTURE})

    collector = link / "scripts" / "collect.mjs"
    result = run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"])
    derived = repo / ".noru" / ".cache" / "privacy-datamap.derived.json"
    results.check(
        "[entry points] a collector run through a symlinked path writes its derived facts",
        derived.is_file(),
        f"exit {result.returncode}, stdout {result.stdout[:120]!r}, stderr {result.stderr[:200]!r}",
    )
    results.check(
        "[entry points] and reports the scan on stdout rather than exiting 0 in silence",
        result.returncode == 0 and result.stdout.strip() != "",
        f"exit {result.returncode}, stdout {result.stdout[:200]!r}",
    )


def test_entry_point_runs_from_a_path_needing_encoding(results, tmp):
    """A repository checked out under a path with a space in it must still scan.

    Same guard, second reason to fail: `import.meta.url` percent-encodes, `process.argv[1]` does
    not, so `.../dir with space/collect.mjs` compares as `dir%20with%20space` against `dir with
    space`. Independent of the symlink case — resolving the path is not enough on its own, and a
    fix that only realpaths would still leave this one broken.
    """
    holder = pathlib.Path(tmp) / "a directory with spaces"
    holder.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PRIVACY_DATAMAP, holder / "privacy-datamap", dirs_exist_ok=True)
    repo = write_files(pathlib.Path(tmp) / "encoded-entry-point", {"db/schema.sql": SQL_FIXTURE})

    collector = holder / "privacy-datamap" / "scripts" / "collect.mjs"
    result = run(["node", str(collector), f"--repo={repo}", "--output=json", "--quiet"])
    derived = repo / ".noru" / ".cache" / "privacy-datamap.derived.json"
    results.check(
        "[entry points] a collector run from a path containing a space writes its derived facts",
        derived.is_file(),
        f"exit {result.returncode}, stdout {result.stdout[:120]!r}, stderr {result.stderr[:200]!r}",
    )


def test_entry_point_guard_tolerates_a_non_path_argv(results):
    """Resolving argv[1] must not turn "not invoked as a script" into a crash.

    `node -e` and `node --input-type=module` leave `process.argv[1]` as whatever followed `--`,
    which is usually not a path at all — and test_change_control_rules_agree_across_languages
    imports the rules that way to compare them against the Python implementation. Resolving a
    string that is not a file throws, so the guard has to treat that as "no" rather than exit 1.
    """
    collector = PRIVACY_DATAMAP / "scripts" / "collect.mjs"
    script = (
        f"const m = await import({str(collector)!r});\n"
        "console.log(JSON.stringify({ piece: m.PIECE }));\n"
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", '{"not":"a path"}'],
        capture_output=True, text=True, timeout=180, check=False,
    )
    results.check(
        "[entry points] importing a collector with a non-path argv[1] neither throws nor runs main",
        completed.returncode == 0 and json.loads(completed.stdout)["piece"] == "privacy-datamap",
        f"exit {completed.returncode}: {completed.stderr[:300]}",
    )


def test_every_entry_point_resolves_before_it_compares(results):
    """One collector proving the point is not enough — the guard is copied into every script.

    It is also copied out of `scripts/templates/`, so a piece scaffolded tomorrow inherits whatever
    is written there. Checking the text of every guard is what stops this from being fixed once and
    reintroduced by the next `scaffold-piece.mjs` run.
    """
    scripts = list(entry_points())
    unresolved = sorted(
        str(p.relative_to(ROOT)) for p in scripts
        if ENTRY_POINT_GUARD not in p.read_text(encoding="utf-8")
    )
    results.check(
        "[entry points] every script resolves and encodes its path before comparing it to argv[1]",
        unresolved == [],
        f"raw comparison in {unresolved}",
    )
    # A guard that stops matching the string above is a guard this test no longer reads. If the
    # form changes deliberately, this count is the reminder to change it here too.
    results.check(
        "[entry points] and the templates a new piece is scaffolded from are among them",
        len(scripts) >= 31 and any(p.suffix == ".tmpl" for p in scripts),
        f"{len(scripts)} entry point(s) found",
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
            test_ai_inventory_scans_what_ci_checks_out(results, tmp)
            test_datamap_reads_every_declared_format(results, tmp)
            test_datamap_scans_what_ci_checks_out(results, tmp)
            test_datamap_parses_drizzle_without_execution(results, tmp)
            test_datamap_classifies_only_what_it_knows(results, tmp)
            test_datamap_normalises_naming_styles(results, tmp)
            test_datamap_citations_point_at_the_real_line(results, tmp)
            test_datamap_surfaces_special_category_data(results, tmp)
            test_datamap_never_overwrites_a_reviewed_manifest(results, tmp)
            test_discovery_acceptance_gate(results, tmp)
            test_scoped_reconciliation_cli(results, tmp)
            test_typescript_and_scoped_analysis(results, tmp)
            test_datamap_candidate_collection(results, tmp)
            test_datamap_connection_graph(results, tmp)
            test_datamap_repeatable_evidence_baseline(results, tmp)
            test_datamap_enrichment_gate(results, tmp)
            test_datamap_reconciles_only_the_privacy_delta(results, tmp)
            test_datamap_bootstrap_ignores_invalid_manifest_baseline(results, tmp)
            test_datamap_compacts_non_personal_review_state(results, tmp)
            test_datamap_fides_projection_is_privacy_only(results, tmp)
            test_datamap_normalizes_logical_topology(results, tmp)
            test_datamap_uses_drizzle_config_for_cross_directory_topology(results, tmp)
            test_datamap_canonical_schema_makes_migration_replay_non_blocking(results, tmp)
            test_datamap_separates_datastores_and_falls_back_for_runtime(results, tmp)
            test_datamap_runtime_discovery_ignores_test_files(results, tmp)
            test_datamap_replays_supported_migrations_and_reports_unsafe_ones(results, tmp)
            test_datamap_reads_evidence_backed_supplemental_stores(results, tmp)
            test_datamap_keys_are_unique_or_fail_actionably(results, tmp)
            test_datamap_output_is_byte_deterministic(results, tmp)
            test_datamap_reconciles_file_ids_to_logical_ids(results, tmp)
            test_datamap_digest_agrees_across_languages(results, tmp)
            test_datamap_render_is_gated_and_matches_the_push(results, tmp)
            test_digest_ignores_the_collectors_own_version(results, tmp)
            test_scaffold_template_scans_what_ci_checks_out(results, tmp)
            test_change_control_rules_agree_across_languages(results, tmp)
            test_change_control_export_survives_a_forbidden_setting(results, tmp)
            test_iac_never_copies_the_line(results, tmp)
            test_iac_identity_survives_a_move(results, tmp)
            test_iac_absence_is_detectable(results, tmp)
            test_iac_classification(results, tmp)
            test_iac_reports_what_stopped_reproducing(results, tmp)
            test_iac_skeleton_never_decides(results, tmp)
            test_iac_scans_what_ci_checks_out(results, tmp)
            test_audit_pack_sample_is_redrawable(results, tmp)
            test_audit_pack_gap_analysis(results, tmp)
            test_audit_pack_assembles_upstream_manifests(results, tmp)
            test_audit_pack_renders_only_a_validated_pack(results, tmp)
            test_entry_point_runs_from_a_symlinked_path(results, tmp)
            test_entry_point_runs_from_a_path_needing_encoding(results, tmp)
        test_missing_disclosure_fixture_alerts(results)
        test_iac_every_status_has_an_expiry_horizon(results)
        test_audit_pack_every_conclusion_has_an_assurance_horizon(results)
        test_entry_point_guard_tolerates_a_non_path_argv(results)
        test_every_entry_point_resolves_before_it_compares(results)
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
