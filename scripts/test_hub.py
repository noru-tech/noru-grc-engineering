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
CONTEXT = ROOT / "plugins" / "noru" / "scripts" / "context.mjs"
ORCHESTRATION = ROOT / "plugins" / "noru" / "references" / "orchestration.json"
ROUTING = ROOT / "plugins" / "noru" / "references" / "routing.json"
REVIEW_COMMAND = ROOT / "plugins" / "noru" / "commands" / "review.md"
STATUS_COMMAND = ROOT / "plugins" / "noru" / "commands" / "status.md"


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
    orchestration = json.loads(ORCHESTRATION.read_text(encoding="utf-8"))
    routed_names = {
        piece["name"] for piece in json.loads(ROUTING.read_text(encoding="utf-8"))["pieces"]
    }
    results.check(
        "orchestration covers every routed piece exactly once",
        set(orchestration["pieces"]) == routed_names,
        orchestration["pieces"].keys(),
    )
    piece_contracts_match = True
    piece_contract_detail = []
    for name, entry in orchestration["pieces"].items():
        contract = json.loads((ROOT / "plugins" / name / "piece.json").read_text(encoding="utf-8"))
        expected_scan = f"/{name}:scan"
        expected_diff = f"/{name}:diff"
        ok = (
            entry["manifest"] == contract["artifact"]
            and entry["scan_command"] == expected_scan
            and entry["diff_command"] == expected_diff
            and entry.get("generated_files", [])
            == [output["path"] for output in contract.get("outputs", [])]
        )
        piece_contracts_match = piece_contracts_match and ok
        if not ok:
            piece_contract_detail.append(name)
    results.check(
        "review orchestration agrees with each independently installed piece contract",
        piece_contracts_match,
        piece_contract_detail,
    )
    status_sections = orchestration["status_sections"]
    results.check(
        "status capability matrix is read-only",
        all(
            section["scope"].startswith("read:")
            and all(tool.startswith(("find", "get", "list")) for tool in section["tools"])
            for section in status_sections.values()
        ),
        status_sections,
    )

    review_command = REVIEW_COMMAND.read_text(encoding="utf-8")
    status_command = STATUS_COMMAND.read_text(encoding="utf-8")
    results.check(
        "review command enforces capability discovery and the no-push boundary",
        "getMcpCapabilities" in review_command
        and "Never invoke a `:push`" in review_command
        and "capabilities.write" in review_command
        and "Nothing was written to Noru" in review_command,
    )
    results.check(
        "status command covers filters, partial scopes, links and privacy reconciliation",
        all(
            fragment in status_command
            for fragment in (
                "--framework=",
                "--domain=",
                "--control=",
                "--due-before=",
                "getMcpCapabilities",
                "nextCursor",
                "counts.byKind",
                "Unavailable or partial sections",
                "Nothing was written to Noru",
            )
        ),
    )
    results.check(
        "status report orders blockers before facts and recommendations",
        status_command.index("Blockers and expired items")
        < status_command.index("Live Noru facts")
        < status_command.index("Recommendations"),
    )

    with tempfile.TemporaryDirectory(prefix="noru-hub-") as tmp:
        repo = pathlib.Path(tmp) / "repo"
        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.email", "fixture@example.com")
        git(repo, "config", "user.name", "Fixture")
        git(
            repo,
            "remote",
            "add",
            "origin",
            "https://fixture-user:fixture-password@example.com/acme/repo.git?token=hidden#part",
        )
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "initial")

        context_result = run(
            ["node", str(CONTEXT), f"--repo={repo}", "--output=json", "--quiet"], repo
        )
        context_payload = json.loads(context_result.stdout)
        results.check(
            "review context includes a credential-sanitized repository remote",
            context_result.returncode == 0
            and context_payload["provenance"]["remote"]
            == "https://example.com/acme/repo.git"
            and context_payload["provenance"]["slug"] == "acme/repo",
            context_payload,
        )

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
        subset, payload = review(repo, "--available-pieces=privacy-datamap")
        pieces = dispositions(payload)
        results.check(
            "independently installed subset marks relevant absent pieces unavailable",
            subset.returncode == 0
            and pieces["privacy-datamap"]["run_state"] == "ready"
            and pieces["privacy-datamap"]["installed"] is True
            and pieces["ai-inventory"]["disposition"] == "selected"
            and pieces["ai-inventory"]["run_state"] == "unavailable"
            and pieces["ai-inventory"]["installed"] is False,
            pieces,
        )
        diff_alias, payload = review(repo, "--run-diff")
        results.check(
            "run-diff alias records the optional diff request",
            diff_alias.returncode == 0 and payload["requested_diff"] is True,
            payload,
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
        bad_piece = review(repo, "--available-pieces=not-a-piece")[0]
        results.check(
            "unknown installed piece names fail explicitly",
            bad_piece.returncode == 2 and "unknown available piece" in bad_piece.stderr,
            bad_piece.stderr,
        )

        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (repo / "publish.js").write_text(
            'const sourceSlug = "noru-tech/noru";\n'
            'fetch("https://api.noru.tech/v1/privacy/datamaps", { token: "do-not-print" });\n',
            encoding="utf-8",
        )
        (workflows / "privacy.yml").write_text(
            'on:\n  push:\n    paths: [".fides/datamap.yml"]\n'
            'env:\n  NORU_SOURCE_SLUG: noru-tech/noru\n',
            encoding="utf-8",
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
            "publish.js:2" in writer_check["detail"]
            and ".github/workflows/privacy.yml:3" in writer_check["detail"]
            and "do-not-print" not in json.dumps(writer_check),
            writer_check,
        )
        results.check(
            "doctor detects repeated source slugs without printing the slug",
            "repeated source slug declaration(s)" in writer_check["detail"]
            and "publish.js:1" in writer_check["detail"]
            and ".github/workflows/privacy.yml:5" in writer_check["detail"]
            and "noru-tech/noru" not in writer_check["detail"],
            writer_check,
        )

    # A sanitized copy of noru-tech/noru's real sync-privacy-data-map workflow. It legitimately
    # contains a path trigger, a slug declaration and a REST write in one file; those are signals
    # for one publisher, not three separate writers.
    with tempfile.TemporaryDirectory(prefix="noru-hub-real-workflow-") as tmp:
        repo = pathlib.Path(tmp) / "repo"
        workflow = repo / ".github" / "workflows" / "sync-privacy-data-map.yml"
        workflow.parent.mkdir(parents=True)
        git(repo.parent, "init", "-b", "main", str(repo))
        git(repo, "config", "user.email", "fixture@example.com")
        git(repo, "config", "user.name", "Fixture")
        workflow.write_text(
            "name: Sync privacy data map\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "    paths:\n"
            "      - \".fides/datamap.yml\"\n"
            "jobs:\n"
            "  sync:\n"
            "    steps:\n"
            "      - name: Convert and push data map\n"
            "        env:\n"
            "          NORU_API_TOKEN: ${{ secrets.NORU_PRIVACY_DATAMAP_TOKEN }}\n"
            "          NORU_SOURCE_SLUG: ${{ github.repository }}\n"
            "        run: |\n"
            "          jq -n --arg slug \"$GITHUB_REPOSITORY\" '{slug:$slug}'\n"
            "          curl -X POST \"$NORU_API_URL/v1/privacy/datamaps\" --data @-\n",
            encoding="utf-8",
        )
        git(repo, "add", ".github/workflows/sync-privacy-data-map.yml")
        git(repo, "commit", "-m", "add current Noru privacy sync workflow")
        doctor = run(
            ["node", str(DOCTOR), f"--repo={repo}", "--output=json", "--quiet"], repo
        )
        doctor_payload = json.loads(doctor.stdout)
        writer_check = next(
            check for check in doctor_payload["checks"] if check["id"] == "privacy-writers"
        )
        results.check(
            "doctor treats the Noru monorepo privacy sync workflow as one writer",
            doctor.returncode == 0
            and writer_check["ok"]
            and "one possible datamap writer" in writer_check["detail"]
            and ".github/workflows/sync-privacy-data-map.yml" in writer_check["detail"]
            and "NORU_PRIVACY_DATAMAP_TOKEN" not in writer_check["detail"],
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
