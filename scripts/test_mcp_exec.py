#!/usr/bin/env python3
"""Prove the headless MCP half: CI builds the snapshot, writes the plan, and nothing else.

Standard library only, no network beyond a loopback socket. Every case drives the real
scripts/ci_check.py and scripts/mcp_exec.mjs against a loopback MCP server started here, which
speaks MCP Streamable HTTP, records every request it receives, and keeps enough state (findings
upserted on source + externalId, evidence searchable by description) that a second run can be
judged on what the first run actually wrote.

What it asserts, in one line each:

  * state is built from the declared read tools, with connection from getMcpCapabilities
  * a CI run performs exactly the planned writes, and says so only with a receipt
  * the second run is a no-op: no write reaches the server
  * a call that needs an id from an earlier create gets it, and only that field changes
  * no key: state and push are skipped, and the server never hears from the job
  * a piece that declares no headless snapshot is skipped with its reason
  * the key reaching a different organization than the plan was built for: nothing is written
  * an edited call list, including an edited substitution, is refused before the network
  * a plan bound to another endpoint is refused before the network; a non-https origin is refused
  * the first failing call stops the run; the receipt says which ran, which failed, which did not
  * a key echoed back in an error never reaches stdout, stderr, the report or the receipt
  * a server answering in Server-Sent Events works the same
  * a read tool the connection does not offer is refused, and a short page is refused, not written

Usage:
    python3 scripts/test_mcp_exec.py [--output=json] [--quiet]
Exit codes: 0 = pass, 1 = a case failed, 2 = usage / setup error.
"""
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_ci_mode import AS_OF, CI_CHECK, Results, build_green_repo  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
MCP_EXEC = ROOT / "scripts" / "mcp_exec.mjs"
PLUGINS = ROOT / "plugins"

# Assembled from fragments: a credential-shaped literal in a source file is exactly what
# scripts/check_repo.py exists to reject, test or not.
FAKE_KEY = "noru_" + "t3stk3y" * 3

USAGE = "usage: test_mcp_exec.py [--output=json] [--quiet]\n"

READ_TOOLS = (
    "getMcpCapabilities", "getSecurityFindings", "getOrganizationEvidence", "findOrganization",
)
WRITE_TOOLS = ("createSecurityFinding", "createEvidence", "linkEvidenceToControl")


class FakeNoru:
    """The organization the loopback server pretends to be. Mutable between runs on purpose."""

    def __init__(self):
        self.org = {"id": "org_fixture_a", "name": "Fixture Organization"}
        self.scopes = ["read:organization", "read:risks", "read:evidence", "read:controls", "read:assets",
                       "write:risks", "write:evidence"]
        self.read_tools = list(READ_TOOLS)
        self.findings = {}
        self.evidence = []
        self.requests = []
        self.page_cap = None
        self.lie_about_total = False
        self.fail_on_call = None  # (tool, nth) -> answer that call with an error
        self.echo_auth = False
        self.sse = False
        self.calls_seen = {}
        self.lock = threading.Lock()

    def writes(self):
        return [r for r in self.requests if r.get("tool") in WRITE_TOOLS]

    def call(self, name, args, auth):
        self.calls_seen[name] = self.calls_seen.get(name, 0) + 1
        if self.fail_on_call == (name, self.calls_seen[name]):
            text = "upstream rejected the write"
            if self.echo_auth:
                text += f" (request had Authorization: {auth})"
            return {"isError": True, "content": [{"type": "text", "text": text}]}
        if name == "getMcpCapabilities":
            return self.ok({
                "organization": self.org,
                "connection": {"grantedScopes": self.scopes, "tokenType": "api_key"},
                "capabilities": {
                    "read": [{"name": t} for t in self.read_tools],
                    "write": [{"name": t} for t in WRITE_TOOLS],
                },
            })
        if name == "getSecurityFindings":
            rows = [f for f in self.findings.values() if f["source"] == args.get("source")]
            return self.ok({"findings": rows, "count": len(rows)})
        if name == "getOrganizationEvidence":
            needle = args.get("search") or ""
            rows = [e for e in self.evidence if needle in e["title"] or needle in e["description"]]
            limit = min(args.get("limit", 50), self.page_cap or 200)
            offset = args.get("offset", 0)
            page = rows[offset:offset + limit]
            total = len(rows) + (5 if self.lie_about_total else 0)
            return self.ok({"evidence": page, "count": len(page), "total": total, "limit": limit, "offset": offset})
        if name == "createSecurityFinding":
            key = (args["source"], args["externalId"])
            existing = self.findings.get(key)
            row = {**args, "id": existing["id"] if existing else f"fnd_{len(self.findings) + 1}"}
            self.findings[key] = row
            return self.ok({"id": row["id"], "created": existing is None})
        if name == "createEvidence":
            row = {
                "id": f"ev_{len(self.evidence) + 1}",
                "title": args["title"],
                "description": args.get("description", ""),
                "expiresAt": None,
                "linkedControls": [{"id": c} for c in args.get("controlIds", [])],
            }
            self.evidence.append(row)
            # Nested on purpose: the executor must find the one id without being told the shape.
            return self.ok({"evidence": row})
        if name == "linkEvidenceToControl":
            target = next((e for e in self.evidence if e["id"] == args.get("evidenceId")), None)
            if target is None:
                return {"isError": True, "content": [{"type": "text", "text": "evidence not found"}]}
            target["linkedControls"].append({"id": args["controlId"]})
            return self.ok({"linked": True})
        return {"isError": True, "content": [{"type": "text", "text": f"unknown tool {name}"}]}

    @staticmethod
    def ok(payload):
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def make_handler(noru):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_DELETE(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            auth = self.headers.get("authorization", "")
            with noru.lock:
                record = {"method": body.get("method"), "auth": auth,
                          "session": self.headers.get("mcp-session-id")}
                if body.get("method") == "tools/call":
                    record["tool"] = body["params"]["name"]
                    record["arguments"] = body["params"].get("arguments", {})
                noru.requests.append(record)
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                if body["method"] == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "loopback", "version": "0"}}
                elif body["method"] == "tools/call":
                    result = noru.call(body["params"]["name"], body["params"].get("arguments", {}), auth)
                else:
                    result = None
            message = {"jsonrpc": "2.0", "id": body["id"]}
            if result is None:
                message["error"] = {"code": -32601, "message": "method not found"}
            else:
                message["result"] = result
            data = json.dumps(message).encode()
            self.send_response(200)
            self.send_header("mcp-session-id", "session-1")
            if noru.sse:
                data = b"event: message\ndata: " + data + b"\n\n"
                self.send_header("content-type", "text/event-stream")
            else:
                self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


class Loopback:
    def __enter__(self):
        self.noru = FakeNoru()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.noru))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def env(self, key=True):
        env = {"NORU_API_URL": self.origin}
        if key:
            env["NORU_API_KEY"] = FAKE_KEY
        return env


def run(cmd, env):
    merged = dict(os.environ)
    merged.pop("NORU_API_KEY", None)
    merged.pop("NORU_API_URL", None)
    merged.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False, env=merged)


def ci(repo, piece, env, *args, report=None):
    extra = [f"--report={report}"] if report else []
    completed = run(
        [sys.executable, str(CI_CHECK), f"--piece={piece}", f"--repo={repo}", f"--as-of={AS_OF}",
         "--output=json", "--quiet", *extra, *args],
        env,
    )
    try:
        return completed, json.loads(completed.stdout)
    except json.JSONDecodeError:
        return completed, None


def mcp(command, repo, piece, env):
    return run(["node", str(MCP_EXEC), command, f"--piece={piece}", f"--repo={repo}",
                "--output=json", "--quiet"], env)


def step(report, name):
    return next((s for s in (report or {}).get("steps", []) if s["step"] == name), {})


def receipt_of(repo, piece):
    path = pathlib.Path(repo) / ".noru" / ".cache" / f"{piece}.receipt.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


ALL = ("--steps=all",)


# --- cases --------------------------------------------------------------------------------------

def case_end_to_end(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "e2e-iac", "iac-scan")
    with Loopback() as lb:
        completed, report = ci(repo, "iac-scan", lb.env(), *ALL)
        state = json.loads((repo / ".noru" / ".cache" / "noru-state.json").read_text())
        planned = json.loads((repo / ".noru" / ".cache" / "iac-scan.plan.json").read_text())
        expected = [op for op in planned["operations"] if op["effect"] != "skip"]
        results.check("e2e: exits 0", completed.returncode == 0, completed.stderr or report)
        results.check("e2e: state step passed", step(report, "state").get("status") == "pass", step(report, "state"))
        results.check(
            "e2e: connection comes from getMcpCapabilities, endpoint from the job",
            state["connection"]["organization"]["id"] == "org_fixture_a"
            and state["connection"]["endpoint"] == f"{lb.origin}/v1/mcp",
            state["connection"],
        )
        writes = lb.noru.writes()
        results.check("e2e: the plan had writes to make", len(expected) > 0, planned["summary"])
        results.check(
            "e2e: exactly the planned writes reached the server, in order",
            [(w["tool"], w["arguments"]) for w in writes]
            == [(op["operation"], op["arguments"]) for op in expected],
            [w["tool"] for w in writes],
        )
        push = step(report, "push")
        receipt = receipt_of(repo, "iac-scan")
        results.check(
            "e2e: push is reported written only with a receipt that says so",
            push.get("status") == "pass" and push.get("written") == len(expected)
            and receipt and receipt["ok"] is True
            and all(c["status"] == "ok" and c.get("response_sha256") for c in receipt["calls"]),
            push,
        )
        results.check(
            "e2e: every request carried the key as a bearer, and only to the loopback server",
            all(r["auth"] == f"Bearer {FAKE_KEY}" for r in lb.noru.requests),
            {r["auth"][:12] for r in lb.noru.requests},
        )

        before = len(lb.noru.writes())
        completed, report = ci(repo, "iac-scan", lb.env(), *ALL)
        results.check(
            "e2e: the second run is a no-op — no write reaches the server",
            completed.returncode == 0 and len(lb.noru.writes()) == before
            and step(report, "push").get("written") == 0,
            step(report, "push"),
        )


def case_depends_on(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "e2e-cc", "change-control")
    with Loopback() as lb:
        completed, report = ci(repo, "change-control", lb.env(), *ALL)
        writes = lb.noru.writes()
        created = [e["id"] for e in lb.noru.evidence]
        links = [w for w in writes if w["tool"] == "linkEvidenceToControl"]
        results.check("depends_on: exits 0", completed.returncode == 0, completed.stderr or step(report, "push"))
        results.check(
            "depends_on: the plan creates evidence and links it",
            created and links, [w["tool"] for w in writes],
        )
        results.check(
            "depends_on: each link carries the id the create returned",
            links and all(l["arguments"]["evidenceId"] == created[0] for l in links),
            [l["arguments"] for l in links],
        )
        receipt = receipt_of(repo, "change-control")
        results.check(
            "depends_on: the receipt records the substituted field",
            receipt and any(c.get("substituted") == {"evidenceId": created[0]} for c in receipt["calls"]),
            receipt and receipt["calls"],
        )
        calls = json.loads((repo / ".noru" / ".cache" / "change-control.calls.json").read_text())
        results.check(
            "depends_on: change-control's :push now writes depends_on into the call list",
            any(c.get("depends_on", {}).get("field") == "evidenceId" for c in calls["calls"])
            or not links,
            [c.get("depends_on") for c in calls["calls"]],
        )
        before = len(lb.noru.writes())
        completed, report = ci(repo, "change-control", lb.env(), *ALL)
        results.check(
            "depends_on: the second run is a no-op",
            completed.returncode == 0 and len(lb.noru.writes()) == before,
            step(report, "push"),
        )

    # A call list edited to substitute a different argument is refused, so the one field an
    # executor may fill in cannot be turned into a way to rewrite another.
    repo, _ = build_green_repo(pathlib.Path(tmp) / "e2e-cc-edit", "change-control")
    with Loopback() as lb:
        calls_path = plan_and_emit(repo, "change-control", lb)
        calls = json.loads(calls_path.read_text())
        dependent = [c for c in calls["calls"] if c.get("depends_on")]
        if dependent:
            dependent[0]["depends_on"]["field"] = "controlId"
            calls_path.write_text(json.dumps(calls))
        lb.noru.requests.clear()
        completed = mcp("exec", repo, "change-control", lb.env())
        results.check(
            "depends_on: an edited substitution is refused before the network",
            dependent and completed.returncode == 1 and lb.noru.requests == []
            and "edited call list" in completed.stderr,
            completed.stderr,
        )


def case_no_key(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "nokey", "iac-scan")
    with Loopback() as lb:
        completed, report = ci(repo, "iac-scan", lb.env(key=False), *ALL)
        results.check("no key: exits 0", completed.returncode == 0, completed.stderr)
        results.check(
            "no key: state, diff and push are skipped, not failed",
            [step(report, s).get("status") for s in ("state", "diff", "push")] == ["skipped"] * 3,
            report and report["steps"],
        )
        results.check("no key: the server never heard from the job", lb.noru.requests == [], lb.noru.requests)


def case_not_headless(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "nothead", "ai-inventory")
    with Loopback() as lb:
        completed, report = ci(repo, "ai-inventory", lb.env(), *ALL)
        state = step(report, "state")
        results.check(
            "not headless: state is skipped with the piece's own reason",
            completed.returncode == 0 and state.get("status") == "skipped"
            and "AI frameworks" in state.get("detail", ""),
            state,
        )
        results.check("not headless: no request was made", lb.noru.requests == [], lb.noru.requests)


def case_wrong_organization(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "org", "iac-scan")
    with Loopback() as lb:
        ci(repo, "iac-scan", lb.env(), "--steps=scan,validate,state,diff")
        lb.noru.org = {"id": "org_fixture_b", "name": "Another Organization"}
        completed, report = ci(repo, "iac-scan", lb.env(), "--steps=scan,validate,diff,push")
        receipt = receipt_of(repo, "iac-scan")
        results.check(
            "wrong organization: nothing was written",
            lb.noru.writes() == [] and completed.returncode == ci_exit_tooling(),
            step(report, "push"),
        )
        results.check(
            "wrong organization: the receipt says it was refused",
            receipt and receipt["ok"] is False and "org_fixture_b" in receipt.get("refused", ""),
            receipt,
        )


def ci_exit_tooling():
    import ci_check
    return ci_check.EXIT_TOOLING


def plan_and_emit(repo, piece, lb):
    ci(repo, piece, lb.env(), "--steps=scan,validate,state,diff")
    push = PLUGINS / piece / "scripts" / "push.mjs"
    run(["node", str(push), f"--repo={repo}", "--confirm", "--output=json", "--quiet"], {})
    return repo / ".noru" / ".cache" / f"{piece}.calls.json"


def case_edited_calls(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "edited", "iac-scan")
    with Loopback() as lb:
        calls_path = plan_and_emit(repo, "iac-scan", lb)
        calls = json.loads(calls_path.read_text())
        calls["calls"][0]["arguments"]["severity"] = "low"
        calls_path.write_text(json.dumps(calls))
        lb.noru.requests.clear()
        completed = mcp("exec", repo, "iac-scan", lb.env())
        results.check(
            "edited call list: refused before the network",
            completed.returncode == 1 and lb.noru.requests == [] and "edited call list" in completed.stderr,
            completed.stderr,
        )

        calls["calls"][0]["arguments"]["severity"] = json.loads(
            (repo / ".noru" / ".cache" / "iac-scan.plan.json").read_text()
        )["operations"][0]["arguments"]["severity"]
        calls["calls"].append({**calls["calls"][0], "order": len(calls["calls"]) + 1, "tool": "deleteSecurityFinding"})
        calls_path.write_text(json.dumps(calls))
        completed = mcp("exec", repo, "iac-scan", lb.env())
        results.check(
            "edited call list: an added call is refused before the network",
            completed.returncode == 1 and lb.noru.requests == [],
            completed.stderr,
        )


def case_endpoint(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "endpoint", "iac-scan")
    with Loopback() as lb:
        plan_and_emit(repo, "iac-scan", lb)
        lb.noru.requests.clear()
        # The plan names the loopback endpoint; a job configured for another one must not follow it.
        completed = mcp("exec", repo, "iac-scan", {"NORU_API_KEY": FAKE_KEY, "NORU_API_URL": "https://api.noru.tech"})
        results.check(
            "endpoint: a plan bound to another endpoint is refused before the network",
            completed.returncode == 1 and "is bound to" in completed.stderr and lb.noru.requests == [],
            completed.stderr,
        )
        completed = mcp("exec", repo, "iac-scan", {"NORU_API_KEY": FAKE_KEY, "NORU_API_URL": "http://example.com"})
        results.check(
            "endpoint: plain http to a non-loopback host is a usage error",
            completed.returncode == 2 and "https" in completed.stderr,
            completed.stderr,
        )
        completed = mcp("exec", repo, "iac-scan", {"NORU_API_KEY": FAKE_KEY, "NORU_API_URL": f"{lb.origin}/elsewhere"})
        results.check(
            "endpoint: NORU_API_URL must be an origin",
            completed.returncode == 2 and "origin" in completed.stderr,
            completed.stderr,
        )


def case_stops_on_failure(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "fail", "iac-scan")
    with Loopback() as lb:
        lb.noru.fail_on_call = ("createSecurityFinding", 1)
        lb.noru.echo_auth = True
        report_path = pathlib.Path(tmp) / "fail-report.json"
        completed, report = ci(repo, "iac-scan", lb.env(), *ALL, report=report_path)
        receipt = receipt_of(repo, "iac-scan")
        statuses = [c["status"] for c in (receipt or {}).get("calls", [])]
        results.check(
            "failure: the run stops at the first failing call",
            completed.returncode == ci_exit_tooling()
            and statuses[:1] == ["error"] and all(s == "not_run" for s in statuses[1:])
            and len(lb.noru.writes()) == 1,
            statuses,
        )
        results.check(
            "failure: push is an error, never 'written'",
            step(report, "push").get("status") == "error" and step(report, "push").get("not_run") == len(statuses) - 1,
            step(report, "push"),
        )
        everything = completed.stdout + completed.stderr + report_path.read_text() + json.dumps(receipt)
        results.check(
            "redaction: a key echoed back in an error reaches no output, report or receipt",
            FAKE_KEY not in everything and "upstream rejected" in everything,
            [line for line in everything.splitlines() if "upstream" in line][:2],
        )


def case_sse(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "sse", "iac-scan")
    with Loopback() as lb:
        lb.noru.sse = True
        completed, report = ci(repo, "iac-scan", lb.env(), *ALL)
        results.check(
            "sse: a server answering in Server-Sent Events works the same",
            completed.returncode == 0 and step(report, "push").get("status") == "pass"
            and len(lb.noru.writes()) > 0,
            completed.stderr or step(report, "push"),
        )


def case_state_guards(results, tmp):
    repo, _ = build_green_repo(pathlib.Path(tmp) / "guards", "evidence-push")
    with Loopback() as lb:
        lb.noru.read_tools = [t for t in READ_TOOLS if t != "getOrganizationEvidence"]
        completed = mcp("state", repo, "evidence-push", lb.env())
        state_path = repo / ".noru" / ".cache" / "noru-state.json"
        results.check(
            "state: a read tool the connection does not offer is refused",
            completed.returncode == 1 and "not a read tool" in completed.stderr and not state_path.exists(),
            completed.stderr,
        )
        lb.noru.read_tools = list(READ_TOOLS)
        for i in range(5):
            lb.noru.evidence.append({"id": f"ev_{i}", "title": "t", "expiresAt": None, "linkedControls": [],
                                     "description": f"[noru-grc-engineering:evidence-push#{i:016x}]"})
        lb.noru.page_cap = 2
        completed = mcp("state", repo, "evidence-push", lb.env())
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        results.check(
            "state: pages until total",
            completed.returncode == 0 and len(state.get("evidence", [])) == 5,
            completed.stderr or len(state.get("evidence", [])),
        )
        state_path.unlink()
        lb.noru.lie_about_total = True
        completed = mcp("state", repo, "evidence-push", lb.env())
        results.check(
            "state: a snapshot that stops short of total is refused, not written",
            completed.returncode == 1 and not state_path.exists(),
            completed.stderr,
        )


def case_every_piece_declares(results, tmp):
    for path in sorted(PLUGINS.glob("*/piece.json")):
        decl = json.loads(path.read_text(encoding="utf-8"))
        state = decl.get("state") or {}
        transports = {op.get("transport") for op in decl["push"]["operations"]}
        results.check(
            f"declaration: {decl['piece']} says whether its snapshot is headless",
            state.get("headless") is True or (state.get("headless") is False and len(state.get("reason", "")) >= 20),
            state,
        )
        if state.get("headless"):
            results.check(
                f"declaration: {decl['piece']} reads into the keys its diff uses",
                all(f"state.{r['into']}" in (path.parent / "scripts" / "diff.mjs").read_text(encoding="utf-8")
                    for r in state["reads"]),
                [r["into"] for r in state["reads"]],
            )
        results.check(
            f"declaration: {decl['piece']} publishes over one transport the orchestrator knows",
            transports in ({"mcp"}, {"rest"}),
            transports,
        )


CASES = (
    case_end_to_end,
    case_depends_on,
    case_no_key,
    case_not_headless,
    case_wrong_organization,
    case_edited_calls,
    case_endpoint,
    case_stops_on_failure,
    case_sse,
    case_state_guards,
    case_every_piece_declares,
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
            sys.stdout.write(USAGE)
            return 0
        else:
            sys.stderr.write(f"error: unknown option '{arg}'\n" + USAGE)
            return 2

    results = Results()
    with tempfile.TemporaryDirectory(prefix="noru-mcp-exec-") as tmp:
        for case in CASES:
            try:
                case(results, tmp)
            except Exception as exc:  # noqa: BLE001 - a crashing case is a failing case
                results.check(case.__name__, False, f"raised {type(exc).__name__}: {exc}")

    if output_json:
        sys.stdout.write(
            json.dumps({"ok": not results.failures, "tests": results.rows}, indent=None if quiet else 2, sort_keys=True)
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
