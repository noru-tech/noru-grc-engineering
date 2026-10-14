#!/usr/bin/env python3
"""Prove confirmation by a reviewed plan: a passing merge publishes exactly the reviewed plan, or nothing.

Standard library only, no network beyond loopback sockets. The publication cases drive the real
scripts/ci_check.py and scripts/mcp_exec.mjs against the loopback MCP server of
scripts/test_mcp_exec.py, in a real git repository whose HEAD is the merge commit. The fetch cases
drive scripts/reviewed_plan.py against a loopback GitHub server that records every request.

What it asserts, in one line each:

  * a pull request's diff writes the reviewed plan and its digest, and a summary a reviewer reads
  * a merge that passed acc publishes exactly the reviewed plan, and says what confirmed it
  * a re-run after the publication has nothing to write and is not refused
  * Noru's state moving between review and merge refuses, and nothing is written
  * no independent approval (ACC003 not passing) refuses, and nothing is written
  * an open finding at or above acc's threshold refuses
  * a pull request that changed the workflow producing its own reviewed plan refuses
  * a merge that did not change the manifest refuses
  * a plan from another workflow, or for another head commit, refuses
  * no reviewed plan (a fork) skips the publication, never passes it
  * the three confirmation flags only come together
  * fetch finds the plan of the pull request a merge came from, and records where it came from
  * fetch returns nothing for a fork, another workflow, or a direct push
  * fetch never sends the token to the artifact storage host

Usage:
    python3 scripts/test_reviewed_plan.py [--output=json] [--quiet]
Exit codes: 0 = pass, 1 = a case failed, 2 = usage / setup error.
"""
import http.server
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import reviewed_plan  # noqa: E402
from test_ci_mode import Results, build_green_repo  # noqa: E402
from test_mcp_exec import Loopback, ci, step  # noqa: E402

WORKFLOW = ".github/workflows/noru-publish.yml"
USAGE = "usage: test_reviewed_plan.py [--output=json] [--quiet]\n"
PIECE = "iac-scan"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def merged_repo(tmp, name):
    """A repository whose HEAD merged the manifest: a base commit without it, then the merge."""
    repo, manifest = build_green_repo(pathlib.Path(tmp) / name, PIECE)
    text = manifest.read_text(encoding="utf-8")
    manifest.unlink()
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    manifest.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "merge the manifest")
    return repo, git(repo, "rev-parse", "HEAD")


def acc_manifest(merge, head="pr-head", acc003="pass", findings=()):
    change = "github:acme/api:pr:7"
    return {
        "version": "0.4",
        "events": {
            "window": {"complete": True},
            "changes": [{"id": change, "head_sha": head, "merge_commit_sha": merge, "reviews_complete": True}],
        },
        "policy": {"fail_on": "medium"},
        "assessments": [{"change_id": change, "rule_id": "ACC003", "status": acc003, "reason": "x"}],
        "findings": [
            {"change_id": change, "rule_id": rule, "severity": "high", "disposition": {"status": "open"}}
            for rule in findings
        ],
    }


def provenance(head="pr-head", **overrides):
    p = {
        "repository": "acme/api", "pull_request": 7, "head_sha": head, "same_repository": True,
        "workflow": WORKFLOW, "run_id": 1, "changed_workflow": False, "changed_manifest": True,
    }
    p.update(overrides)
    return p


def review(repo, lb, tmp, name):
    """The pull request's job: a plan and its digest, as its workflow would upload them."""
    out = pathlib.Path(tmp) / f"{name}-reviewed-plan.json"
    completed, report = ci(
        repo, PIECE, lb.env(), "--steps=scan,validate,state,diff", f"--write-reviewed-plan={out}"
    )
    return out, completed, report


def publish(repo, lb, tmp, name, reviewed, acc, **prov):
    doc = json.loads(reviewed.read_text())
    doc["provenance"] = provenance(**prov)
    fetched = pathlib.Path(tmp) / f"{name}-fetched.json"
    fetched.write_text(json.dumps(doc))
    acc_path = pathlib.Path(tmp) / f"{name}-acc.json"
    acc_path.write_text(json.dumps(acc))
    return ci(
        repo, PIECE, lb.env(), "--steps=all",
        f"--reviewed-plan={fetched}", f"--acc-manifest={acc_path}", f"--publish-workflow={WORKFLOW}",
    )


# --- publication --------------------------------------------------------------------------------

def case_review_and_publish(results, tmp):
    repo, merge = merged_repo(tmp, "publish")
    with Loopback() as lb:
        reviewed, completed, report = review(repo, lb, tmp, "publish")
        doc = json.loads(reviewed.read_text())
        results.check(
            "review: the pull request's diff writes the reviewed plan and its digest",
            completed.returncode == 0 and doc["digest"].startswith("sha256:")
            and step(report, "diff").get("reviewed_plan", {}).get("digest") == doc["digest"],
            completed.stderr or step(report, "diff"),
        )
        summary = reviewed.with_suffix(".md").read_text()
        results.check(
            "review: the summary names every operation and the digest",
            doc["digest"] in summary and "createSecurityFinding" in summary,
            summary[:200],
        )
        planned = [op for op in doc["plan"]["operations"] if op["effect"] != "skip"]
        completed, report = publish(repo, lb, tmp, "publish", reviewed, acc_manifest(merge))
        results.check(
            "publish: a passing merge writes exactly the reviewed plan",
            completed.returncode == 0 and step(report, "push").get("status") == "pass"
            and [w["arguments"] for w in lb.noru.writes()] == [op["arguments"] for op in planned],
            completed.stderr or step(report, "push"),
        )
        results.check(
            "publish: the report names the digest that confirmed it",
            (report or {}).get("confirmed_by", {}).get("reviewed_plan") == doc["digest"],
            (report or {}).get("confirmed_by"),
        )
        before = len(lb.noru.writes())
        completed, report = publish(repo, lb, tmp, "publish-again", reviewed, acc_manifest(merge))
        results.check(
            "publish: a re-run after the publication has nothing to write and is not refused",
            completed.returncode == 0 and len(lb.noru.writes()) == before
            and step(report, "push").get("status") == "pass",
            completed.stderr or step(report, "push"),
        )


def refused(results, tmp, name, label, acc=None, mutate=None, **prov):
    repo, merge = merged_repo(tmp, name)
    with Loopback() as lb:
        reviewed, _, _ = review(repo, lb, tmp, name)
        if mutate:
            mutate(lb, json.loads(reviewed.read_text()))
        completed, report = publish(repo, lb, tmp, name, reviewed, acc or acc_manifest(merge), **prov)
        push = step(report, "push")
        results.check(
            f"refused: {label}",
            completed.returncode == 6 and push.get("status") == "error"
            and "not confirmed by a reviewed plan" in push.get("detail", "") and lb.noru.writes() == [],
            push,
        )
        return push


def case_refusals(results, tmp):
    def move_state(lb, doc):
        # Between review and merge, someone filed one of the planned findings with other content,
        # so the plan built after the merge updates it instead of creating it.
        first = next(op for op in doc["plan"]["operations"] if op["effect"] == "create")["arguments"]
        lb.noru.findings[(first["source"], first["externalId"])] = {
            **first, "id": "filed-meanwhile", "severity": "low" if first["severity"] != "low" else "high",
        }

    push = refused(results, tmp, "moved", "Noru's state moved between review and merge", mutate=move_state)
    results.check("refused: the moved-state refusal says why", "state changed" in push.get("detail", ""), push)
    for name, label, acc_kwargs, prov in [
        ("acc003", "no independent approval (ACC003 fails)", {"acc003": "fail"}, {}),
        ("finding", "an open finding at or above acc's threshold", {"findings": ("ACC002",)}, {}),
        ("workflow", "the pull request changed its own publishing workflow", {}, {"changed_workflow": True}),
        ("manifest", "the merge did not change the manifest", {}, {"changed_manifest": False}),
        ("other-workflow", "a plan from another workflow", {}, {"workflow": ".github/workflows/other.yml"}),
        ("other-head", "a plan for another head commit", {}, {"head": "not-the-head"}),
        ("fork", "a plan from a fork", {}, {"same_repository": False}),
    ]:
        repo, merge = merged_repo(tmp, name)
        with Loopback() as lb:
            reviewed, _, _ = review(repo, lb, tmp, name)
            completed, report = publish(repo, lb, tmp, name, reviewed, acc_manifest(merge, **acc_kwargs), **prov)
            push = step(report, "push")
            results.check(
                f"refused: {label}",
                completed.returncode == 6 and push.get("status") == "error"
                and "not confirmed by a reviewed plan" in push.get("detail", "") and lb.noru.writes() == [],
                push,
            )


def case_no_reviewed_plan(results, tmp):
    repo, merge = merged_repo(tmp, "absent")
    with Loopback() as lb:
        acc_path = pathlib.Path(tmp) / "absent-acc.json"
        acc_path.write_text(json.dumps(acc_manifest(merge)))
        completed, report = ci(
            repo, PIECE, lb.env(), "--steps=all", f"--reviewed-plan={pathlib.Path(tmp) / 'missing.json'}",
            f"--acc-manifest={acc_path}", f"--publish-workflow={WORKFLOW}",
        )
        push = step(report, "push")
        results.check(
            "no reviewed plan (a fork): the publication is skipped, never passed",
            completed.returncode == 0 and push.get("status") == "skipped" and lb.noru.writes() == [],
            push,
        )
        completed, _ = ci(repo, PIECE, lb.env(), "--steps=all", "--reviewed-plan=x.json")
        results.check(
            "usage: the three confirmation flags only come together",
            completed.returncode == 2 and "go together" in completed.stderr,
            completed.stderr,
        )


# --- fetch --------------------------------------------------------------------------------------

class FakeGitHub:
    def __init__(self, reviewed, fork=False, workflow=WORKFLOW, files=(".noru/iac-scan.yml",)):
        self.reviewed = reviewed
        self.fork = fork
        self.workflow = workflow
        self.files = list(files)
        self.requests = []

    def archive(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("reviewed-plan.json", json.dumps(self.reviewed))
        return buf.getvalue()


def github_handler(fake, origin_box):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body=b"", headers=()):
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("&per_page")[0].split("?per_page")[0]
            fake.requests.append({"path": self.path, "auth": self.headers.get("authorization")})
            repo = "acme/api"
            head_repo = "someone/fork" if fake.fork else repo
            if path == f"/repos/{repo}/commits/merge123/pulls":
                return self.send(200, json.dumps([{
                    "number": 7, "merged_at": "2026-10-14T00:00:00Z", "merge_commit_sha": "merge123",
                    "head": {"sha": "pr-head", "repo": {"full_name": head_repo}},
                }]).encode())
            if path.startswith(f"/repos/{repo}/commits/") and path.endswith("/pulls"):
                return self.send(200, b"[]")
            if path == f"/repos/{repo}/actions/runs?head_sha=pr-head&event=pull_request":
                return self.send(200, json.dumps({"workflow_runs": [
                    {"id": 55, "path": fake.workflow, "head_sha": "pr-head", "conclusion": "success", "run_attempt": 1},
                    {"id": 56, "path": WORKFLOW, "head_sha": "pr-head", "conclusion": "failure", "run_attempt": 2},
                ]}).encode())
            if path == f"/repos/{repo}/actions/runs/55/artifacts":
                return self.send(200, json.dumps({"artifacts": [
                    {"id": 900, "name": "noru-reviewed-plan-iac-scan", "expired": False},
                    {"id": 901, "name": "noru-reviewed-plan-other", "expired": False},
                ]}).encode())
            if path == f"/repos/{repo}/actions/artifacts/900/zip":
                return self.send(302, headers=[("Location", f"{origin_box[0]}/blob/artifact-900")])
            if path == "/blob/artifact-900":
                return self.send(200, fake.archive())
            if path == f"/repos/{repo}/pulls/7/files":
                return self.send(200, json.dumps([{"filename": f} for f in fake.files]).encode())
            return self.send(404, b"{}")

    return Handler


def with_github(fake, fn):
    box = [None]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), github_handler(fake, box))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    box[0] = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        gh = reviewed_plan.GitHub(box[0], "ghs_" + "testtoken" * 3)
        return fn(gh)
    finally:
        server.shutdown()
        server.server_close()


def case_fetch(results, tmp):
    doc = {"format": reviewed_plan.FORMAT, "piece": PIECE, "digest": "sha256:abc", "plan": {}}
    fake = FakeGitHub(doc)
    got, why = with_github(fake, lambda gh: reviewed_plan.fetch(gh, "acme/api", "merge123", PIECE, WORKFLOW))
    results.check(
        "fetch: the plan of the pull request the merge came from, with where it came from",
        got is not None and got["digest"] == "sha256:abc"
        and got["provenance"] == provenance(run_id=55),
        why or got,
    )
    blob = [r for r in fake.requests if r["path"].startswith("/blob/")]
    results.check(
        "fetch: the token is never sent to the artifact storage host",
        blob and all(r["auth"] is None for r in blob)
        and all(r["auth"] for r in fake.requests if not r["path"].startswith("/blob/")),
        fake.requests,
    )
    changed = FakeGitHub(doc, files=(".noru/iac-scan.yml", WORKFLOW))
    got, _ = with_github(changed, lambda gh: reviewed_plan.fetch(gh, "acme/api", "merge123", PIECE, WORKFLOW))
    results.check(
        "fetch: records that the pull request changed the publishing workflow",
        got is not None and got["provenance"]["changed_workflow"] is True,
        got and got["provenance"],
    )
    for label, fake, commit in [
        ("a fork", FakeGitHub(doc, fork=True), "merge123"),
        ("another workflow", FakeGitHub(doc, workflow=".github/workflows/other.yml"), "merge123"),
        ("a direct push", FakeGitHub(doc), "direct456"),
    ]:
        got, why = with_github(fake, lambda gh, c=commit: reviewed_plan.fetch(gh, "acme/api", c, PIECE, WORKFLOW))
        results.check(f"fetch: nothing for {label}", got is None and why, why)


def case_digest(results, tmp):
    plan = {
        "piece": PIECE, "piece_version": "1", "manifest": ".noru/iac-scan.yml", "manifest_sha256": "a" * 64,
        "target": {"organization_id": "o"}, "required_scopes": [], "operations": [{"effect": "create"}],
        "generated_at": "2026-01-01T00:00:00Z", "repository": {"commit_sha": "pr-head"},
    }
    moved = dict(plan, generated_at="2026-02-02T00:00:00Z", repository={"commit_sha": "merge"})
    results.check(
        "digest: when and where a plan was built does not change it",
        reviewed_plan.digest(plan) == reviewed_plan.digest(moved),
        "",
    )
    other = dict(plan, operations=[{"effect": "update"}])
    results.check("digest: what it does does", reviewed_plan.digest(plan) != reviewed_plan.digest(other), "")


CASES = (case_digest, case_review_and_publish, case_refusals, case_no_reviewed_plan, case_fetch)


def main(argv):
    output_json = "--output=json" in argv
    quiet = "--quiet" in argv
    unknown = [a for a in argv if a not in ("--output=json", "--output=text", "--quiet")]
    if unknown:
        sys.stderr.write(f"error: unknown option '{unknown[0]}'\n" + USAGE)
        return 2
    results = Results()
    with tempfile.TemporaryDirectory(prefix="noru-reviewed-plan-") as tmp:
        for case in CASES:
            try:
                case(results, tmp)
            except Exception as exc:  # noqa: BLE001 - a crashing case is a failing case
                results.check(case.__name__, False, f"raised {type(exc).__name__}: {exc}")
    if output_json:
        sys.stdout.write(json.dumps({"ok": not results.failures, "tests": results.rows}, sort_keys=True) + "\n")
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
