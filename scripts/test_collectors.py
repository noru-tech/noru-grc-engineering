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

Usage:
    python3 scripts/test_collectors.py [--output=json] [--quiet]
Exit codes: 0 = all tests pass, 1 = a test failed, 2 = usage / setup error.
"""
import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
AI_INVENTORY = ROOT / "plugins" / "ai-inventory"
COLLECTOR = AI_INVENTORY / "scripts" / "collect.mjs"
VALIDATOR = AI_INVENTORY / "scripts" / "validate_manifest.py"


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
        test_missing_disclosure_fixture_alerts(results)
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
