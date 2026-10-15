#!/usr/bin/env python3
"""Write a CI-mode result to its merge request as one note, updated in place on every run.

The GitLab counterpart of the GitHub job summary. Each run renders the JSON report scripts/ci_check.py
or scripts/ci_review.py wrote and either updates the note an earlier run of the same job left on the
merge request, or posts one. The note carries a hidden marker naming the job, and only a note with
that marker *written by the same account* is ever updated, so a person who quotes the marker in their
own comment cannot make this script edit it, and re-runs never stack up a column of notes.

Credentials: the CI job token cannot do this. https://docs.gitlab.com/ci/jobs/ci_job_token/ lists
the merge request Notes API for `CI_JOB_TOKEN` as GET only. So the note is written with a token from
the CI/CD variable NORU_GITLAB_NOTE_TOKEN — a project access token on the one project, role Reporter,
scope `api` (https://docs.gitlab.com/api/notes/, https://docs.gitlab.com/security/tokens/access_token_scopes/).
It is read from the environment at the point of use, sent in a `PRIVATE-TOKEN` header only to
CI_API_V4_URL, only over https (plain http is accepted for a loopback host, which is how the tests
run), never with a redirect followed, and scrubbed from anything this script prints.

Never a gate: without the token, or outside a merge request pipeline (no CI_MERGE_REQUEST_IID), the
note is skipped with a sentence saying why, and the exit is 0. A fork's merge request pipeline runs
without the parent project's variables, so it is skipped too. Whether the check passed is the
check's exit code, never this script's.

Usage:
    python3 scripts/gitlab_mr_note.py --report=<path.json> [--key=<name>] [--output=json|text] [--quiet]
Exit codes:
    0  the note was written, or skipped for a reason stated in the output
    1  GitLab refused or could not be reached; the output says which, with the token redacted
    2  usage error
"""
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from finding_docs import finding_url  # noqa: E402

TOKEN_VARIABLE = "NORU_GITLAB_NOTE_TOKEN"
MARKER_PREFIX = "<!-- noru-grc-engineering:merge-request-note "
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_PAGES = 50
MAX_FINDINGS = 50
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
KEY_RE = re.compile(r"[^A-Za-z0-9._:/-]+")

USAGE = "usage: gitlab_mr_note.py --report=<path.json> [--key=<name>] [--output=json|text] [--quiet]\n"

STATUS_LABEL = {
    "pass": "PASS",
    "pass-with-warnings": "PASS",
    "warn": "WARN-ONLY",
    "skipped": "SKIPPED",
    "fail": "FAIL",
    "error": "ERROR",
}


class Refused(Exception):
    """GitLab answered with an error, or could not be reached."""


def redact(text, token):
    """The token's value, and anything token-shaped, never reaches output."""
    text = str(text)
    if token:
        text = text.replace(token, "<redacted>")
    text = re.sub(r"\bgl[a-z]{2,6}-[A-Za-z0-9_.-]{8,}", "<redacted>", text)
    return re.sub(r"(?i)(private-token\s*[:=]\s*)\S+", r"\1<redacted>", text)


def marker(key):
    return f"{MARKER_PREFIX}key={KEY_RE.sub('-', key)} -->"


def cell(value, limit):
    return str(value or "").replace("|", "\\|").replace("\n", " ")[:limit]


def render_check(report, lines):
    """One ci_check report: the same step and finding tables as the GitHub job summary."""
    fail_on = set((report.get("policy") or {}).get("fail_on") or [])
    gating = report.get("mode") == "gate"
    label = STATUS_LABEL.get(report.get("status"), report.get("status"))
    lines.append(f"### Noru CI mode — `{report.get('piece')}` — {label} (exit {report.get('exit_code')})")
    lines.append("")
    lines.append("| step | result | note |")
    lines.append("| --- | --- | --- |")
    for step in report.get("steps") or []:
        lines.append(f"| {step.get('step')} | {step.get('status')} | {cell(step.get('detail'), 180)} |")
    findings = report.get("findings") or []
    if findings:
        lines.append("")
        lines.append("| | kind | where | what |")
        lines.append("| --- | --- | --- | --- |")
        for finding in findings[:MAX_FINDINGS]:
            kind = finding.get("kind", "")
            mark = ("blocking" if gating else "would block") if kind in fail_on else "note"
            where = finding.get("path") or finding.get("manifest") or ""
            lines.append(
                f"| {mark} | [`{kind}`]({finding_url(kind)}) | `{cell(where, 80)}` | "
                f"{cell(finding.get('message'), 600)} |"
            )
        if len(findings) > MAX_FINDINGS:
            lines.append("")
            lines.append(f"_{len(findings) - MAX_FINDINGS} further finding(s) in the JSON report._")


def render(report, key):
    lines = [marker(key)]
    if report.get("check") == "ci-review":
        label = STATUS_LABEL.get(report.get("status"), report.get("status"))
        selected = ", ".join(f"`{p}`" for p in report.get("selected_pieces") or []) or "none"
        lines.append(f"### Noru GRC review — {label} ({report.get('mode')} mode)")
        lines.append("")
        lines.append(f"Pieces selected by this merge request's changes: {selected}.")
        for result in report.get("results") or []:
            lines.append("")
            if result.get("steps") is not None and result.get("findings") is not None:
                render_check(result, lines)
            else:
                lines.append(f"`{result.get('piece')}`: {cell(result.get('status'), 40)} — {cell(result.get('detail'), 400)}")
        lines.append("")
        lines.append("No diff or push step ran. Nothing was written to Noru.")
    else:
        render_check(report, lines)
    job_url = os.environ.get("CI_JOB_URL")
    if job_url:
        lines.append("")
        lines.append(f"_From [this job]({job_url}). The JSON report is in its artifacts._")
    return "\n".join(lines) + "\n"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would carry the token's request to wherever the Location points.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitLab:
    def __init__(self, api, token):
        self.api = api.rstrip("/")
        self.token = token
        self.opener = urllib.request.build_opener(_NoRedirect)

    def call(self, method, path, body=None):
        data = None
        headers = {"PRIVATE-TOKEN": self.token, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.api}{path}", data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=60) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                next_page = response.headers.get("X-Next-Page") or ""
        except urllib.error.HTTPError as error:
            raise Refused(f"GitLab answered {error.code} to {method} {urllib.parse.urlsplit(request.full_url).path}") from None
        except (urllib.error.URLError, OSError) as error:
            raise Refused(f"GitLab could not be reached ({getattr(error, 'reason', error)})") from None
        if len(payload) > MAX_RESPONSE_BYTES:
            raise Refused("GitLab's response exceeds the size limit")
        try:
            return (json.loads(payload) if payload else None), next_page.strip()
        except json.JSONDecodeError:
            raise Refused(f"GitLab answered {method} {path} with something that is not JSON") from None


def api_base(env):
    """CI_API_V4_URL, refused unless it is https (or plain http to a loopback host)."""
    base = (env.get("CI_API_V4_URL") or "").strip()
    url = urllib.parse.urlsplit(base)
    if url.scheme == "https" and url.hostname:
        return base, None
    if url.scheme == "http" and url.hostname in LOOPBACK_HOSTS:
        return base, None
    return None, "CI_API_V4_URL is not an https URL, so the token is not sent to it"


def own_note(gitlab, project, iid, key, user_id):
    """The id of the note an earlier run of this job wrote, or None."""
    wanted = marker(key)
    page = "1"
    for _ in range(MAX_PAGES):
        notes, page = gitlab.call(
            "GET", f"/projects/{project}/merge_requests/{iid}/notes?per_page=100&page={page}"
            "&sort=asc&order_by=created_at",
        )
        for note in notes or []:
            author = (note.get("author") or {}).get("id")
            if not note.get("system") and author == user_id and str(note.get("body", "")).startswith(wanted):
                return note.get("id")
        # The next page number comes from the server; anything but a number ends the walk.
        if not page.isdigit():
            return None
    return None


def publish(env, report, key):
    """(outcome, detail). Outcome is posted, updated or skipped; raises Refused on a GitLab error."""
    token = env.get(TOKEN_VARIABLE) or ""
    iid = (env.get("CI_MERGE_REQUEST_IID") or "").strip()
    project = (env.get("CI_PROJECT_ID") or "").strip()
    if not iid:
        return "skipped", "no CI_MERGE_REQUEST_IID: this is not a merge request pipeline, so there is no merge request to note"
    if not project:
        return "skipped", "no CI_PROJECT_ID: this does not look like a GitLab CI job"
    if not token:
        return "skipped", (
            f"{TOKEN_VARIABLE} is not set, so nothing can write the note (the job token cannot "
            "create merge request notes). A merge request from a fork runs without the project's "
            "variables, so this is its normal state."
        )
    base, problem = api_base(env)
    if problem:
        return "skipped", problem
    gitlab = GitLab(base, token)
    project = urllib.parse.quote(project, safe="")
    iid = urllib.parse.quote(iid, safe="")
    user, _ = gitlab.call("GET", "/user")
    user_id = (user or {}).get("id")
    if user_id is None:
        raise Refused("GitLab did not say which account the token belongs to")
    body = {"body": render(report, key)}
    existing = own_note(gitlab, project, iid, key, user_id)
    if existing is not None:
        gitlab.call("PUT", f"/projects/{project}/merge_requests/{iid}/notes/{existing}", body)
        return "updated", f"updated note {existing} on merge request !{iid}"
    created, _ = gitlab.call("POST", f"/projects/{project}/merge_requests/{iid}/notes", body)
    return "posted", f"posted note {(created or {}).get('id')} on merge request !{iid}"


def default_key(report):
    if report.get("check") == "ci-review":
        return "review"
    return f"ci:{report.get('piece')}"


def main(argv):
    opts = {"report": None, "key": None, "json": False, "quiet": False}
    for arg in argv:
        if arg.startswith("--report="):
            opts["report"] = arg.split("=", 1)[1]
        elif arg.startswith("--key="):
            opts["key"] = arg.split("=", 1)[1]
        elif arg == "--output=json":
            opts["json"] = True
        elif arg == "--output=text":
            opts["json"] = False
        elif arg == "--quiet":
            opts["quiet"] = True
        elif arg in ("-h", "--help"):
            sys.stdout.write(USAGE)
            return 0
        else:
            sys.stderr.write(f"error: unknown option '{arg}'\n{USAGE}")
            return 2
    if not opts["report"]:
        sys.stderr.write("error: --report=<path.json> is required\n" + USAGE)
        return 2

    env = os.environ
    token = env.get(TOKEN_VARIABLE) or ""
    code = 0
    try:
        report = json.loads(pathlib.Path(opts["report"]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # The check failed before it wrote a report; its own log says why. A note saying nothing
        # useful is worse than none.
        outcome, detail = "skipped", f"no readable report at {opts['report']} ({exc.__class__.__name__})"
    else:
        key = opts["key"] or env.get("CI_JOB_NAME") or default_key(report)
        try:
            outcome, detail = publish(env, report, key)
        except Refused as refusal:
            outcome, detail, code = "error", str(refusal), 1
    detail = redact(detail, token)

    if opts["json"]:
        sys.stdout.write(json.dumps({"note": outcome, "detail": detail}, sort_keys=True) + "\n")
    elif not opts["quiet"] or outcome == "error":
        stream = sys.stderr if outcome == "error" else sys.stdout
        stream.write(f"merge request note {outcome}: {detail}\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
