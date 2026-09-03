#!/usr/bin/env python3
"""Exercise the consolidated, structurally read-only CI review."""

import json
import pathlib
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parent.parent
REVIEW = ROOT / "scripts" / "ci_review.py"


def run(command, cwd):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False, timeout=120)


def git(repo, *args):
    result = run(["git", *args], repo)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def check(name, condition, detail=""):
    if condition:
        return True
    print(f"  FAIL  {name}\n        {str(detail)[:1000]}")
    return False


def main(argv):
    quiet = "--quiet" in argv
    unknown = [arg for arg in argv if arg != "--quiet"]
    if unknown:
        print(f"error: unknown option '{unknown[0]}'", file=sys.stderr)
        return 2

    outcomes = []
    with tempfile.TemporaryDirectory(prefix="noru-ci-review-test-") as tmp:
        repo = pathlib.Path(tmp) / "repo"
        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.email", "fixture@example.com")
        git(repo, "config", "user.name", "Fixture")
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "initial")
        git(repo, "switch", "-c", "feature")
        (repo / "db").mkdir()
        (repo / "db" / "schema.sql").write_text(
            "CREATE TABLE users (email text);\n", encoding="utf-8"
        )
        git(repo, "add", "db/schema.sql")
        git(repo, "commit", "-m", "add user schema")

        warn_report = pathlib.Path(tmp) / "warn.json"
        warn = run(
            [
                sys.executable,
                str(REVIEW),
                f"--repo={repo}",
                "--base-ref=main",
                "--mode=warn",
                f"--report={warn_report}",
                "--output=json",
                "--quiet",
            ],
            repo,
        )
        warn_payload = json.loads(warn_report.read_text(encoding="utf-8"))
        outcomes.append(
            check(
                "branch routing selects privacy and runs a warning review",
                warn.returncode == 0
                and warn_payload["selected_pieces"] == ["privacy-datamap"]
                and warn_payload["status"] == "warn",
                warn_payload,
            )
        )
        outcomes.append(
            check(
                "consolidated CI review cannot run diff or push",
                warn_payload["write_boundary"].endswith("diff and push unavailable")
                and all(
                    step["step"] not in ("diff", "push")
                    for result in warn_payload["results"]
                    for step in result["steps"]
                ),
                warn_payload,
            )
        )

        gate_report = pathlib.Path(tmp) / "gate.json"
        gate = run(
            [
                sys.executable,
                str(REVIEW),
                f"--repo={repo}",
                "--base-ref=main",
                "--pieces=privacy-datamap",
                "--mode=gate",
                f"--report={gate_report}",
                "--quiet",
            ],
            repo,
        )
        gate_payload = json.loads(gate_report.read_text(encoding="utf-8"))
        outcomes.append(
            check(
                "gate mode fails when a selected piece finds drift",
                gate.returncode == 1 and gate_payload["status"] == "fail",
                gate_payload,
            )
        )

        bad = run(
            [sys.executable, str(REVIEW), f"--repo={repo}", "--base-ref=missing"], repo
        )
        outcomes.append(
            check(
                "an unavailable base fails explicitly",
                bad.returncode == 6 and "does not resolve" in bad.stderr,
                bad.stderr,
            )
        )

        git(repo, "switch", "main")
        clean_report = pathlib.Path(tmp) / "clean.json"
        clean = run(
            [
                sys.executable,
                str(REVIEW),
                f"--repo={repo}",
                "--base-ref=main",
                f"--report={clean_report}",
                "--quiet",
            ],
            repo,
        )
        clean_payload = json.loads(clean_report.read_text(encoding="utf-8"))
        outcomes.append(
            check(
                "an unchanged branch is a clean pass with no selected pieces",
                clean.returncode == 0
                and clean_payload["status"] == "pass"
                and clean_payload["selected_pieces"] == [],
                clean_payload,
            )
        )

    if not all(outcomes):
        print(f"\nFAILED: {sum(not outcome for outcome in outcomes)} consolidated review test(s).")
        return 1
    if not quiet:
        print("  ok    consolidated branch routing")
        print("  ok    structural no-write boundary")
        print("  ok    enforcement mode")
        print("  ok    missing-base failure")
        print("  ok    unchanged branch")
    print(f"\nOK: {len(outcomes)} consolidated CI review test(s) passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
