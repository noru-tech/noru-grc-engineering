#!/usr/bin/env python3
"""Prove the GitLab half: the merge request note, and the two CI/CD component templates, run for real.

Standard library only, no network beyond a loopback socket. A loopback server started here plays the
part of the GitLab REST API — the user endpoint and the merge request notes endpoints — and records
every request, with its headers, so a case can be judged on what actually reached it.

What it asserts, in one line each:

  * no note token, or no merge request: the note is skipped, exit 0, and GitLab never hears from it
  * a CI_API_V4_URL that is not https is never sent the token
  * the first run posts one note; every later run updates that note, never a second one
  * a note carrying the marker but written by someone else is never edited
  * the job's own note is found on a later page
  * the token travels only in the PRIVATE-TOKEN header, and never in output, even when echoed back
  * a redirect is not followed, so the token cannot be carried to another host
  * each template's inputs are all declared and all used, and NORU_API_KEY is never one of them
  * noru-ci.yml, run as GitLab would run its script, passes a true record, fails drift with exit 3,
    writes every report its artifacts declare, and writes one note that it then updates
  * the note never decides the job: a GitLab error or a missing token leaves the check's exit code
  * a toolkit tag that does not resolve to the pinned commit stops the job before anything runs
  * noru-review.yml routes a merge request's changes, writes its Code Quality report and a note

Usage:
    python3 scripts/test_gitlab_ci.py [--output=json] [--quiet]
Exit codes: 0 = pass, 1 = a case failed, 2 = usage / setup error.
"""
import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_ci_mode import AS_OF, NEW_PROVIDER_FILE, Results, add_new_provider, build_green_repo, git_init  # noqa: E402
from publish_actions import marketplace_version  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTE = ROOT / "scripts" / "gitlab_mr_note.py"
TEMPLATES = ROOT / "templates" / "gitlab"
sys.path.insert(0, str(ROOT / "contract" / "lib"))
from yaml_mini import load_yaml  # noqa: E402

# Shaped like nothing scripts/check_repo.py looks for, because neither is a credential. The second
# is GitLab-token-shaped on purpose, to prove the shape is scrubbed even where the value is unknown.
FAKE_TOKEN = "fixture-note-token-" + "0123456789" * 2
FAKE_JOB_TOKEN = "fixture-job-token-" + "9876543210"
GITLAB_SHAPED = "glpat-" + "fixture" + "x" * 14
OWN_USER = {"id": 41, "username": "project_1_bot_fixture"}
OTHER_USER = {"id": 7, "username": "a.person"}
INPUT_RE = re.compile(r"\$\[\[\s*inputs\.([a-z0-9-]+)\s*\]\]")

USAGE = "usage: test_gitlab_ci.py [--output=json] [--quiet]\n"


class FakeGitLab:
    """The merge request notes of one project, as the REST API presents them."""

    def __init__(self):
        self.notes = []
        self.requests = []
        self.per_page_cap = 100
        self.fail_status = None
        self.echo_token = False
        self.redirect_to = None
        self.lock = threading.Lock()

    def add(self, body, author, system=False):
        note = {"id": 1000 + len(self.notes), "body": body, "author": dict(author), "system": system}
        self.notes.append(note)
        return note


def make_handler(gitlab):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, payload, headers=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle_any(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            url = urllib.parse.urlsplit(self.path)
            with gitlab.lock:
                gitlab.requests.append({"method": method, "path": url.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": raw.decode("utf-8")})
            token = self.headers.get("PRIVATE-TOKEN")
            if gitlab.redirect_to:
                self.send_response(302)
                self.send_header("Location", gitlab.redirect_to)
                self.end_headers()
                return
            if gitlab.fail_status:
                detail = {"message": "fixture failure"}
                if gitlab.echo_token:
                    detail["message"] += f" (PRIVATE-TOKEN was {token}; token {GITLAB_SHAPED})"
                return self.reply(gitlab.fail_status, detail)
            if token != FAKE_TOKEN:
                return self.reply(401, {"message": "401 Unauthorized"})
            if url.path == "/api/v4/user" and method == "GET":
                return self.reply(200, OWN_USER)
            match = re.fullmatch(r"/api/v4/projects/1/merge_requests/7/notes(?:/(\d+))?", url.path)
            if not match:
                return self.reply(404, {"message": "404 Not Found"})
            if method == "GET":
                query = urllib.parse.parse_qs(url.query)
                per_page = min(int(query.get("per_page", ["20"])[0]), gitlab.per_page_cap)
                page = int(query.get("page", ["1"])[0])
                rows = gitlab.notes[(page - 1) * per_page: page * per_page]
                more = page * per_page < len(gitlab.notes)
                return self.reply(200, rows, {"X-Next-Page": str(page + 1) if more else ""})
            body = json.loads(raw or b"{}")
            if method == "POST" and not match.group(1):
                return self.reply(201, gitlab.add(body["body"], OWN_USER))
            if method == "PUT" and match.group(1):
                note = next((n for n in gitlab.notes if n["id"] == int(match.group(1))), None)
                if note is None or note["author"]["id"] != OWN_USER["id"]:
                    return self.reply(403, {"message": "403 Forbidden"})
                note["body"] = body["body"]
                return self.reply(200, note)
            return self.reply(405, {"message": "405 Method Not Allowed"})

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

        def do_PUT(self):
            self.handle_any("PUT")

    return Handler


class Loopback:
    def __enter__(self):
        self.gitlab = FakeGitLab()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.gitlab))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = f"http://127.0.0.1:{self.server.server_address[1]}/api/v4"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def env(self, **overrides):
        env = {
            "CI_API_V4_URL": self.api,
            "CI_PROJECT_ID": "1",
            "CI_MERGE_REQUEST_IID": "7",
            "CI_JOB_URL": "https://gitlab.example.invalid/example-org/example-app/-/jobs/1",
            "NORU_GITLAB_NOTE_TOKEN": FAKE_TOKEN,
        }
        env.update(overrides)
        return {k: v for k, v in env.items() if v is not None}


def base_env():
    """This process's environment without anything a CI system or a credential put there."""
    return {
        k: v for k, v in os.environ.items()
        if not re.match(r"^(?:CI|CI_.*|GITLAB_.*|GITHUB_.*|RUNNER_.*|NORU_.*)$", k)
    }


def note(report_path, env, *args):
    completed = subprocess.run(
        [sys.executable, str(NOTE), f"--report={report_path}", "--output=json", *args],
        capture_output=True, text=True, timeout=120, check=False, env={**base_env(), **env},
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {}
    return completed, payload


def write_report(tmp, status="pass", piece="ai-inventory"):
    report = {
        "check": "ci", "piece": piece, "mode": "gate", "status": status, "exit_code": 0 if status == "pass" else 3,
        "policy": {"fail_on": ["drift"]}, "steps": [{"step": "scan", "status": "pass" if status == "pass" else "fail"}],
        "findings": [] if status == "pass" else [{"kind": "drift", "message": "the committed manifest no longer matches the repository", "manifest": ".noru/ai-inventory.yml"}],
    }
    path = pathlib.Path(tmp) / f"report-{status}.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


# --- the note ------------------------------------------------------------------------------------
def case_note_skips(results, tmp):
    report = write_report(tmp)
    with Loopback() as server:
        for label, env in (
            ("no NORU_GITLAB_NOTE_TOKEN", server.env(NORU_GITLAB_NOTE_TOKEN=None)),
            ("no CI_MERGE_REQUEST_IID (not a merge request pipeline)", server.env(CI_MERGE_REQUEST_IID=None)),
            ("a CI_API_V4_URL that is not https", server.env(CI_API_V4_URL="http://gitlab.example.invalid/api/v4")),
        ):
            completed, payload = note(report, env)
            results.check(
                f"note: {label} is skipped with exit 0 and a reason",
                completed.returncode == 0 and payload.get("note") == "skipped" and payload.get("detail"),
                completed.stdout + completed.stderr,
            )
        results.check("note: a skipped note never reaches GitLab", not server.gitlab.requests, server.gitlab.requests[:2])
        completed, payload = note(pathlib.Path(tmp) / "absent.json", server.env())
        results.check(
            "note: no report (the check stopped before writing one) is skipped, exit 0",
            completed.returncode == 0 and payload.get("note") == "skipped" and not server.gitlab.requests,
            completed.stdout,
        )


def case_note_posts_then_updates(results, tmp):
    with Loopback() as server:
        server.gitlab.add("A person's review comment.", OTHER_USER)
        server.gitlab.add("merged branch", OTHER_USER, system=True)
        completed, payload = note(write_report(tmp), server.env(), "--key=noru-ci-ai-inventory")
        own = [n for n in server.gitlab.notes if n["author"]["id"] == OWN_USER["id"]]
        results.check(
            "note: the first run posts one note, with the marker and the result",
            completed.returncode == 0 and payload.get("note") == "posted" and len(own) == 1
            and own[0]["body"].startswith("<!-- noru-grc-engineering:merge-request-note key=noru-ci-ai-inventory -->")
            and "PASS" in own[0]["body"],
            (payload, own),
        )
        completed, payload = note(write_report(tmp, "fail"), server.env(), "--key=noru-ci-ai-inventory")
        own = [n for n in server.gitlab.notes if n["author"]["id"] == OWN_USER["id"]]
        results.check(
            "note: a later run updates that note, never posts a second",
            payload.get("note") == "updated" and len(own) == 1 and "FAIL" in own[0]["body"] and "drift" in own[0]["body"],
            (payload, [n["body"][:80] for n in own]),
        )
        completed, payload = note(write_report(tmp), server.env(), "--key=noru-ci-iac-scan")
        own = [n for n in server.gitlab.notes if n["author"]["id"] == OWN_USER["id"]]
        results.check("note: another job's key gets its own note", payload.get("note") == "posted" and len(own) == 2, payload)
        writes = [r for r in server.gitlab.requests if r["method"] in ("POST", "PUT")]
        results.check(
            "note: the token travels only in the PRIVATE-TOKEN header, to the configured API",
            all(r["headers"].get("private-token") == FAKE_TOKEN for r in server.gitlab.requests)
            and all(FAKE_TOKEN not in r["path"] and FAKE_TOKEN not in r["body"] for r in server.gitlab.requests)
            and all(r["path"].startswith("/api/v4/") for r in server.gitlab.requests)
            and writes,
            [(r["method"], r["path"]) for r in server.gitlab.requests],
        )


def case_note_identity(results, tmp):
    with Loopback() as server:
        marker = "<!-- noru-grc-engineering:merge-request-note key=noru-ci-ai-inventory -->"
        quoted = server.gitlab.add(marker + "\nquoted by a person", OTHER_USER)
        completed, payload = note(write_report(tmp), server.env(), "--key=noru-ci-ai-inventory")
        results.check(
            "note: a marker in someone else's note is never edited; this job posts its own",
            payload.get("note") == "posted" and quoted["body"].endswith("quoted by a person")
            and not any(r["method"] == "PUT" for r in server.gitlab.requests),
            payload,
        )
    with Loopback() as server:
        server.gitlab.per_page_cap = 2
        for index in range(4):
            server.gitlab.add(f"comment {index}", OTHER_USER)
        note(write_report(tmp), server.env(), "--key=paged")
        completed, payload = note(write_report(tmp, "fail"), server.env(), "--key=paged")
        own = [n for n in server.gitlab.notes if n["author"]["id"] == OWN_USER["id"]]
        results.check(
            "note: the job's own note is found on a later page and updated",
            payload.get("note") == "updated" and len(own) == 1,
            (payload, len(own)),
        )


def case_note_failures(results, tmp):
    with Loopback() as server:
        server.gitlab.fail_status = 500
        server.gitlab.echo_token = True
        completed = subprocess.run(
            [sys.executable, str(NOTE), f"--report={write_report(tmp)}"],
            capture_output=True, text=True, timeout=120, check=False, env={**base_env(), **server.env()},
        )
        output = completed.stdout + completed.stderr
        results.check(
            "note: a GitLab error exits 1 and says so",
            completed.returncode == 1 and "500" in output,
            output,
        )
        results.check(
            "note: the token never reaches output, even when GitLab echoes it back",
            FAKE_TOKEN not in output and GITLAB_SHAPED not in output,
            output,
        )
    with Loopback() as target, Loopback() as server:
        server.gitlab.redirect_to = f"{target.api}/user"
        completed, payload = note(write_report(tmp), server.env())
        results.check(
            "note: a redirect is not followed, so the token is never carried to another host",
            completed.returncode == 1 and payload.get("note") == "error" and not target.gitlab.requests,
            (payload, target.gitlab.requests[:1]),
        )


def case_note_review(results, tmp):
    review = {
        "check": "ci-review", "mode": "warn", "status": "warn", "selected_pieces": ["privacy-datamap"],
        "results": [{
            "check": "ci", "piece": "privacy-datamap", "mode": "warn", "status": "warn", "exit_code": 0,
            "policy": {"fail_on": ["drift"]}, "steps": [{"step": "scan", "status": "fail"}],
            "findings": [{"kind": "drift", "message": "no committed manifest | yet", "manifest": ".noru/privacy-datamap.yml"}],
        }],
    }
    path = pathlib.Path(tmp) / "review.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    with Loopback() as server:
        completed, payload = note(path, server.env())
        body = (server.gitlab.notes[-1]["body"] if server.gitlab.notes else "")
        results.check(
            "note: a review report renders every selected piece, says nothing was written, and escapes table pipes",
            payload.get("note") == "posted" and "key=review" in body and "`privacy-datamap`" in body
            and "Nothing was written to Noru" in body and "no committed manifest \\| yet" in body,
            body[:400],
        )


# --- the templates -------------------------------------------------------------------------------
def load_template(name):
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    head, _, body = text.partition("\n---\n")
    spec, _ = load_yaml(head)
    jobs, _ = load_yaml(body)
    return text, spec["spec"]["inputs"], jobs


def interpolate(value, inputs):
    def one(match):
        raw = inputs[match.group(1)]
        return ("true" if raw else "false") if isinstance(raw, bool) else str(raw)

    if isinstance(value, str):
        return INPUT_RE.sub(one, value)
    if isinstance(value, list):
        return [interpolate(v, inputs) for v in value]
    if isinstance(value, dict):
        return {interpolate(k, inputs): interpolate(v, inputs) for k, v in value.items()}
    return value


def render_job(name, **given):
    """The job GitLab would build from `include:` with these inputs: (job name, job)."""
    _, spec, jobs = load_template(name)
    inputs = {key: row.get("default") for key, row in spec.items()}
    inputs.update(given)
    missing = [key for key, value in inputs.items() if value is None]
    if missing:
        raise ValueError(f"required inputs not given: {missing}")
    (job_name, job), = jobs.items()
    return interpolate(job_name, inputs), interpolate(job, inputs)


def script_of(job):
    """The job's script as one string: the templates give it as one block, as GitLab allows."""
    script = job["script"]
    return script if isinstance(script, str) else "\n".join(script)


def run_job(job, project, env):
    variables = {k: str(v) for k, v in (job.get("variables") or {}).items()}
    full = {**base_env(), **variables, **env, "CI_PROJECT_DIR": str(project)}
    return subprocess.run(
        ["sh", "-c", script_of(job)], cwd=project, capture_output=True, text=True,
        timeout=600, check=False, env=full,
    )


def make_toolkit(tmp, version):
    """This working tree as a release of the toolkit: a repository with the tag the template clones."""
    toolkit = pathlib.Path(tmp) / "toolkit"
    if toolkit.exists():
        shutil.rmtree(toolkit)
    toolkit.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", ".DS_Store")
    for name in ("scripts", "plugins", "contract"):
        shutil.copytree(ROOT / name, toolkit / name, ignore=ignore)
    shutil.copy2(ROOT / "LICENSE", toolkit / "LICENSE")
    git_init(toolkit, "toolkit")
    subprocess.run(["git", "-C", str(toolkit), "tag", f"v{version}"], check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(toolkit), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    return toolkit.as_uri(), head


def case_template_structure(results, tmp):
    version = marketplace_version()
    for name in ("noru-ci.yml", "noru-review.yml"):
        text, spec, jobs = load_template(name)
        body = text.partition("\n---\n")[2]
        used = set(INPUT_RE.findall(body))
        results.check(f"[{name}] every input it uses is declared", used <= set(spec), sorted(used - set(spec)))
        results.check(f"[{name}] every declared input is used", set(spec) <= used, sorted(set(spec) - used))
        results.check(
            f"[{name}] NORU_API_KEY and the note token are never inputs or job variables",
            not any(re.search(r"(?i)api[-_]?key|token", key) for key in spec)
            and not any(re.search(r"(?i)api[-_]?key|token", key) for job in jobs.values() for key in (job.get("variables") or {})),
            sorted(spec),
        )
        results.check(
            f"[{name}] runs the toolkit at this release: toolkit-version defaults to v{version}",
            spec["toolkit-version"]["default"] == f"v{version}",
            spec["toolkit-version"],
        )
        (_, job), = jobs.items()
        results.check(
            f"[{name}] saves its reports as artifacts whatever the job's result",
            (job.get("artifacts") or {}).get("when") == "always" and (job["artifacts"].get("reports") or {}).get("codequality"),
            job.get("artifacts"),
        )
    _, spec, jobs = load_template("noru-review.yml")
    (_, job), = jobs.items()
    script = script_of(job)
    results.check(
        "[noru-review.yml] is read-only: no steps input, no diff or push, NORU_API_KEY removed",
        "steps" not in spec and "--steps" not in script and "push" not in script
        and "env -u NORU_API_KEY -u NORU_GITLAB_NOTE_TOKEN python3" in script,
        script[:200],
    )
    results.check(
        "[noru-review.yml] runs in merge request pipelines only",
        job.get("rules") == [{"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'}],
        job.get("rules"),
    )
    results.check(
        "[noru-review.yml] starts in warn mode, gating only on what the merge request introduces",
        spec["mode"]["default"] == "warn" and spec["gate-on-new"]["default"] is True,
        (spec["mode"], spec["gate-on-new"]),
    )
    _, spec, jobs = load_template("noru-ci.yml")
    (_, ci_job), = jobs.items()
    script = script_of(ci_job)
    results.check(
        "[noru-ci.yml] the check never inherits the note token, and the note never inherits NORU_API_KEY",
        "env -u NORU_GITLAB_NOTE_TOKEN python3" in script and "env -u NORU_API_KEY python3" in script,
    )
    results.check(
        "[noru-ci.yml] has no rules of its own: the pipeline's workflow rules decide when it runs",
        "rules" not in ci_job,
        ci_job.get("rules"),
    )
    results.check(
        "[noru-ci.yml] has no fetch-reviewed-plan: that confirmation reads GitHub's APIs",
        not any("reviewed" in key for key in spec),
        sorted(spec),
    )
    for name, one in (("noru-ci.yml", ci_job), ("noru-review.yml", job)):
        results.check(
            f"[{name}] clones with full history (GIT_DEPTH 0) and keeps artifacts 30 days by default",
            str((one.get("variables") or {}).get("GIT_DEPTH")) == "0"
            and one["artifacts"]["expire_in"] == "$[[ inputs.expire-in ]]",
            one.get("variables", {}).get("GIT_DEPTH"),
        )
    results.check(
        "both templates default to keeping artifacts 30 days, and to node:22-bookworm",
        all(load_template(n)[1]["expire-in"]["default"] == "30 days" and load_template(n)[1]["image"]["default"] == "node:22-bookworm"
            for n in ("noru-ci.yml", "noru-review.yml")),
    )

    # The review never hands a piece the CI system's job token or the note token either.
    import ci_review  # noqa: PLC0415 - imported here so a broken review script fails this case only
    probe = [sys.executable, "-c", "import os;print(sorted(k for k in ('CI_JOB_TOKEN','NORU_GITLAB_NOTE_TOKEN','NORU_API_KEY') if k in os.environ))"]
    saved = {k: os.environ.get(k) for k in ("CI_JOB_TOKEN", "NORU_GITLAB_NOTE_TOKEN", "NORU_API_KEY")}
    os.environ.update({"CI_JOB_TOKEN": FAKE_JOB_TOKEN, "NORU_GITLAB_NOTE_TOKEN": FAKE_TOKEN, "NORU_API_KEY": "test-value-not-a-real-key"})
    try:
        completed = ci_review.run(probe, str(ROOT))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    results.check(
        "ci_review.py: no piece inherits NORU_API_KEY, the job token or the note token",
        getattr(completed, "stdout", "").strip() == "[]",
        getattr(completed, "stdout", completed),
    )


def ci_job_env(server, **extra):
    env = server.env(GITLAB_CI="true", CI="true", CI_JOB_TOKEN=FAKE_JOB_TOKEN, CI_PIPELINE_SOURCE="merge_request_event")
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


def case_template_ci(results, tmp):
    version = marketplace_version()
    toolkit_url, toolkit_head = make_toolkit(tmp, version)
    project, _ = build_green_repo(pathlib.Path(tmp) / "project")
    git_init(project, "the record matches the code")
    inputs = {"piece": "ai-inventory", "as-of": AS_OF, "toolkit-url": toolkit_url}
    job_name, job = render_job("noru-ci.yml", **inputs)
    results.check("[noru-ci.yml] the job is named for its piece", job_name == "noru-ci-ai-inventory", job_name)

    with Loopback() as server:
        completed = run_job(job, project, ci_job_env(server))
        output = completed.stdout + completed.stderr
        results.check("[noru-ci.yml] a true record passes: exit 0", completed.returncode == 0, output[-600:])
        reports = job["artifacts"]["reports"]
        loaded = {}
        for kind, rel in reports.items():
            try:
                loaded[kind] = json.loads((project / rel).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded[kind] = None
        results.check(
            "[noru-ci.yml] every report its artifacts declare is written: Code Quality and SAST",
            loaded.get("codequality") == [] and isinstance(loaded.get("sast"), dict),
            {k: (v if not isinstance(v, dict) else "dict") for k, v in loaded.items()},
        )
        report_path = project / ".noru/.cache/ci/noru-ci-ai-inventory.json"
        results.check(
            "[noru-ci.yml] the JSON report is written into the artifact paths",
            report_path.is_file() and job["artifacts"]["paths"][0] == ".noru/.cache/ci/",
            job["artifacts"]["paths"],
        )
        own = [n for n in server.gitlab.notes if n["author"]["id"] == OWN_USER["id"]]
        results.check(
            "[noru-ci.yml] one note is posted on the merge request",
            len(own) == 1 and "key=noru-ci-ai-inventory" in own[0]["body"] and "PASS" in own[0]["body"],
            [n["body"][:120] for n in own],
        )
        results.check(
            "[noru-ci.yml] the job token is never sent anywhere",
            all(FAKE_JOB_TOKEN not in json.dumps(r) for r in server.gitlab.requests) and FAKE_TOKEN not in output,
        )

        add_new_provider(project)
        subprocess.run(["git", "-C", str(project), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(project), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "a provider arrives"], check=True, capture_output=True)
        completed = run_job(job, project, ci_job_env(server))
        quality = json.loads((project / reports["codequality"]).read_text(encoding="utf-8"))
        own = [n for n in server.gitlab.notes if n["author"]["id"] == OWN_USER["id"]]
        results.check("[noru-ci.yml] drift fails the job with exit 3", completed.returncode == 3, (completed.stdout + completed.stderr)[-600:])
        results.check(
            "[noru-ci.yml] and the Code Quality report names the file that arrived",
            any(e["location"]["path"] == NEW_PROVIDER_FILE and e["check_name"].endswith("/drift") for e in quality),
            quality,
        )
        results.check(
            "[noru-ci.yml] and the same note is updated to say so",
            len(own) == 1 and "FAIL" in own[0]["body"] and "drift" in own[0]["body"],
            [n["body"][:120] for n in own],
        )

        server.gitlab.fail_status = 500
        completed = run_job(job, project, ci_job_env(server))
        results.check(
            "[noru-ci.yml] a GitLab error writing the note leaves the check's own exit code",
            completed.returncode == 3 and "the check result above stands" in completed.stdout + completed.stderr,
            completed.returncode,
        )
        server.gitlab.fail_status = None
        before = len(server.gitlab.requests)
        completed = run_job(job, project, ci_job_env(server, NORU_GITLAB_NOTE_TOKEN=None))
        results.check(
            "[noru-ci.yml] without the note token the note is skipped and the exit code is the check's",
            completed.returncode == 3 and "skipped" in completed.stdout and len(server.gitlab.requests) == before,
            completed.stdout[-300:],
        )
        _, quiet_job = render_job("noru-ci.yml", **inputs, **{"merge-request-note": False})
        completed = run_job(quiet_job, project, ci_job_env(server))
        results.check(
            "[noru-ci.yml] merge-request-note: false sends nothing to GitLab",
            completed.returncode == 3 and len(server.gitlab.requests) == before,
        )

        _, pinned = render_job("noru-ci.yml", **inputs, **{"toolkit-sha": toolkit_head})
        completed = run_job(pinned, project, ci_job_env(server))
        results.check("[noru-ci.yml] a toolkit-sha that matches the tag runs", completed.returncode == 3, completed.stdout[-300:])
        _, wrong = render_job("noru-ci.yml", **inputs, **{"toolkit-sha": "f" * 40})
        completed = run_job(wrong, project, ci_job_env(server))
        results.check(
            "[noru-ci.yml] a tag that does not resolve to toolkit-sha stops the job before anything runs",
            completed.returncode == 1 and "not the pinned" in completed.stdout,
            completed.stdout[-300:],
        )


def case_template_review(results, tmp):
    version = marketplace_version()
    toolkit_url, _ = make_toolkit(tmp, version)
    project = pathlib.Path(tmp) / "review-project"
    if project.exists():
        shutil.rmtree(project)
    project.mkdir()
    (project / "README.md").write_text("fixture\n", encoding="utf-8")
    git_init(project, "initial")
    base = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (project / "db").mkdir()
    (project / "db" / "schema.sql").write_text("CREATE TABLE users (email text);\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "add a schema"], check=True, capture_output=True)
    _, job = render_job("noru-review.yml", **{"toolkit-url": toolkit_url})
    with Loopback() as server:
        completed = run_job(job, project, ci_job_env(server, CI_MERGE_REQUEST_DIFF_BASE_SHA=base))
        output = completed.stdout + completed.stderr
        report = project / ".noru/.cache/ci/noru-grc-review.json"
        payload = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
        results.check(
            "[noru-review.yml] routes the merge request's changes and reports in warn mode: exit 0",
            completed.returncode == 0 and payload.get("selected_pieces") == ["privacy-datamap"],
            output[-600:],
        )
        quality_path = project / job["artifacts"]["reports"]["codequality"]
        quality = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.is_file() else None
        results.check(
            "[noru-review.yml] writes the Code Quality report its artifacts declare",
            isinstance(quality, list) and all(e["check_name"].startswith("noru/privacy-datamap/") for e in quality),
            quality,
        )
        results.check(
            "[noru-review.yml] and one note on the merge request",
            len(server.gitlab.notes) == 1 and "Noru GRC review" in server.gitlab.notes[0]["body"],
            [n["body"][:120] for n in server.gitlab.notes],
        )


CASES = (
    case_note_skips,
    case_note_posts_then_updates,
    case_note_identity,
    case_note_failures,
    case_note_review,
    case_template_structure,
    case_template_ci,
    case_template_review,
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
    with tempfile.TemporaryDirectory(prefix="noru-gitlab-ci-") as tmp:
        for case in CASES:
            try:
                case(results, tmp)
            except Exception as exc:  # noqa: BLE001 - a crashing case is a failing case
                results.check(case.__name__, False, f"raised {type(exc).__name__}: {exc}")

    if output_json:
        sys.stdout.write(json.dumps({"ok": not results.failures, "tests": results.rows}, indent=None if quiet else 2, sort_keys=True) + "\n")
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
