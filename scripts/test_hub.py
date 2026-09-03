#!/usr/bin/env python3
"""Exercise the hub's deterministic branch reviewer with no network or Noru connection."""

import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
REVIEW = ROOT / "plugins" / "noru" / "scripts" / "review.mjs"
DOCTOR = ROOT / "plugins" / "noru" / "scripts" / "doctor.mjs"


class Results:
    def __init__(self):
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append((name, bool(ok), str(detail)[:500]))


def run(command, cwd):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False, timeout=30)


def git(repo, *args):
    result = run(["git", *args], repo)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def review(repo, *args):
    result = run(
        ["node", str(REVIEW), f"--repo={repo}", "--base-ref=main", "--output=json", "--quiet", *args],
        repo,
    )
    payload = json.loads(result.stdout) if result.returncode == 0 else None
    return result, payload


def dispositions(payload):
    return {piece["name"]: piece for piece in payload["pieces"]}


def main(argv):
    quiet = "--quiet" in argv
    unknown = [arg for arg in argv if arg != "--quiet"]
    if unknown:
        print(f"error: unknown option '{unknown[0]}'", file=sys.stderr)
        return 2

    results = Results()
    with tempfile.TemporaryDirectory(prefix="noru-hub-") as tmp:
        repo = pathlib.Path(tmp) / "repo"
        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.email", "fixture@example.com")
        git(repo, "config", "user.name", "Fixture")
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "initial")

        unchanged, payload = review(repo)
        results.check("unchanged branch succeeds", unchanged.returncode == 0, unchanged.stderr)
        results.check("unchanged branch is clean", payload and payload["clean"], payload)
        results.check(
            "unchanged branch explains every skipped piece",
            payload
            and len(payload["pieces"]) == 8
            and all(piece["disposition"] == "skipped" and piece["reasons"] for piece in payload["pieces"]),
            payload,
        )

        git(repo, "switch", "-c", "feature")
        (repo / "db").mkdir()
        (repo / "db" / "schema.sql").write_text(
            "CREATE TABLE users (email text);\n", encoding="utf-8"
        )
        (repo / "src").mkdir()
        (repo / "src" / "agent.ts").write_text(
            'import { generateText } from "ai";\n', encoding="utf-8"
        )
        git(repo, "add", "db/schema.sql", "src/agent.ts")
        git(repo, "commit", "-m", "add data and ai")

        changed, payload = review(repo)
        pieces = dispositions(payload)
        results.check("changed branch succeeds", changed.returncode == 0, changed.stderr)
        results.check(
            "schema and AI changes select the matching pieces",
            pieces["privacy-datamap"]["disposition"] == "selected"
            and pieces["ai-inventory"]["disposition"] == "selected",
            pieces,
        )
        results.check(
            "content reasons carry line citations",
            any("src/agent.ts:1" in reason for reason in pieces["ai-inventory"]["reasons"]),
            pieces["ai-inventory"],
        )
        results.check(
            "unmatched pieces remain visible with reasons",
            all(piece["reasons"] for piece in pieces.values())
            and pieces["audit-pack"]["disposition"] == "skipped",
            pieces,
        )

        (repo / "README.md").write_text("fixture\nmeeting minutes\n", encoding="utf-8")
        working, payload = review(repo)
        results.check(
            "tracked working-tree changes are included before commit",
            working.returncode == 0
            and dispositions(payload)["governance-records"]["disposition"] == "selected"
            and any(file["path"] == "README.md" for file in payload["changed_files"]),
            payload,
        )

        (repo / "infra.tf").write_text('resource "aws_s3_bucket" "example" {}\n', encoding="utf-8")
        excluded, payload = review(repo)
        results.check(
            "untracked files are reported but excluded by default",
            "infra.tf" in payload["excluded_untracked"]
            and dispositions(payload)["iac-scan"]["disposition"] == "skipped",
            payload,
        )
        included, payload = review(repo, "--include-untracked")
        results.check(
            "untracked files can be included explicitly",
            included.returncode == 0
            and dispositions(payload)["iac-scan"]["disposition"] == "selected",
            payload,
        )

        explicit, payload = review(repo, "--pieces=review-signoff")
        pieces = dispositions(payload)
        results.check(
            "explicit selection does not add inferred pieces",
            explicit.returncode == 0
            and pieces["review-signoff"]["disposition"] == "selected"
            and sum(piece["disposition"] == "selected" for piece in pieces.values()) == 1,
            pieces,
        )
        results.check("review writes no .noru directory", not (repo / ".noru").exists())

        bad = run(
            ["node", str(REVIEW), f"--repo={repo}", "--base-ref=missing-ref", "--quiet"], repo
        )
        results.check(
            "an unresolved base ref fails explicitly",
            bad.returncode == 2 and "error:" in bad.stderr,
            bad.stderr,
        )

        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (repo / "publish.js").write_text(
            'fetch("https://api.noru.tech/v1/privacy/datamaps", { token: "do-not-print" });\n',
            encoding="utf-8",
        )
        (workflows / "privacy.yml").write_text(
            'on:\n  push:\n    paths: [".fides/datamap.yml"]\n', encoding="utf-8"
        )
        git(repo, "add", "publish.js", ".github/workflows/privacy.yml")
        git(repo, "commit", "-m", "add competing datamap writers")
        doctor = run(
            ["node", str(DOCTOR), f"--repo={repo}", "--output=json", "--quiet"], repo
        )
        doctor_payload = json.loads(doctor.stdout)
        writer_check = next(
            check for check in doctor_payload["checks"] if check["id"] == "privacy-writers"
        )
        results.check(
            "doctor warns about multiple privacy writers without failing readiness",
            doctor.returncode == 0 and not writer_check["ok"] and not writer_check["required"],
            writer_check,
        )
        results.check(
            "privacy writer warning cites files without echoing source content",
            "publish.js:1" in writer_check["detail"]
            and ".github/workflows/privacy.yml:3" in writer_check["detail"]
            and "do-not-print" not in json.dumps(writer_check),
            writer_check,
        )

    failures = [row for row in results.rows if not row[1]]
    for name, ok, detail in results.rows:
        if not ok:
            print(f"  FAIL  {name}\n        {detail}")
        elif not quiet:
            print(f"  ok    {name}")
    if failures:
        print(f"\nFAILED: {len(failures)} of {len(results.rows)} test(s).")
        return 1
    print(f"\nOK: {len(results.rows)} hub review test(s) passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
