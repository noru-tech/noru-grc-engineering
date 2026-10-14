#!/usr/bin/env python3
"""Confirmation by a reviewed plan: a passing merge confirms exactly the plan its pull request showed.

`:push` refuses without a fresh plan bound to the manifest bytes and an explicit confirmation
(contract requirement 5). In CI there is a second way a plan can be agreed, and only one: the plan
a pull request's own workflow computed and showed for review is the plan a publish job on the
protected branch may execute after that pull request merged, if, and only if, all of these hold:

  1. the pull request came from this repository, not a fork;
  2. the reviewed plan was produced by the declared workflow, on the pull request's head commit,
     and the merge did not change that workflow, so the run used the protected copy of it;
  3. the merge passed acc's policy: an approval independent of the change's effective author,
     complete review and collection data, no open finding at or above the threshold;
  4. the merge changed `.noru/<piece>.yml`, so the manifest bytes are the ones that were reviewed;
  5. the plan rebuilt after the merge has the same reviewable digest. When Noru's state moved
     between review and merge, the digests differ and nothing is written.

The reviewable digest covers what a plan does — piece and version, manifest bytes, target
organization and endpoint, required scopes, every operation with its effect and arguments — and
not when or where it was built, which necessarily differ between the pull request and the merge.

Standard library only. `verify` and `write` need no network. `fetch` reads GitHub with the token in
GITHUB_TOKEN, at the point of use, and is the only part of this toolkit that does.

Usage:
    python3 scripts/reviewed_plan.py fetch --repository=OWNER/REPO --commit=SHA --piece=NAME
        --workflow=.github/workflows/FILE.yml --out=PATH [--output=json]
Exit codes (fetch):
    0  reviewed plan written to --out
    1  no reviewed plan exists for this merge (a fork pull request, a direct push, or a run that
       produced none): publish through explicit confirmation instead
    2  usage error
    3  GitHub could not be read
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

FORMAT = "noru-reviewed-plan/1"
ARTIFACT_PREFIX = "noru-reviewed-plan-"
DEFAULT_API = "https://api.github.com"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_PAGES = 30
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024

# The plan fields that say what it does. Everything else — when it was generated and expires, the
# repository root, branch and commit it was computed in, provenance — differs between the pull
# request and the merge by construction.
REVIEWABLE_FIELDS = (
    "piece",
    "piece_version",
    "manifest",
    "manifest_sha256",
    "target",
    "required_scopes",
    "operations",
)

SEVERITY = {"info": 0, "warning": 1, "medium": 2, "high": 3}


class Refusal(Exception):
    """A condition of confirmation by a reviewed plan does not hold."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def reviewable(plan):
    missing = [f for f in REVIEWABLE_FIELDS if f not in plan]
    if missing:
        raise Refusal(f"the plan has no {', '.join(missing)}")
    return {field: plan[field] for field in REVIEWABLE_FIELDS}


def digest(plan):
    """`sha256:<hex>` over the canonical JSON of the plan's reviewable fields."""
    return "sha256:" + hashlib.sha256(canonical(reviewable(plan)).encode("utf-8")).hexdigest()


def document(plan):
    """The reviewed-plan document a pull request's workflow stores as an artifact."""
    return {
        "format": FORMAT,
        "piece": plan.get("piece"),
        "digest": digest(plan),
        "plan": reviewable(plan),
        "summary": plan.get("summary"),
    }


def render_markdown(doc):
    """What a reviewer reads in the job summary: the operations, and the digest that binds them."""
    plan = doc["plan"]
    target = plan.get("target") or {}
    lines = [
        f"### Reviewed plan for `{doc['piece']}`",
        "",
        f"Organization `{target.get('organization_name')}` ({target.get('organization_id')}), "
        f"manifest `{plan.get('manifest')}`.",
        "",
        "| effect | operation | subject |",
        "| --- | --- | --- |",
    ]
    for op in plan.get("operations") or []:
        subject = str(op.get("subject", "")).replace("|", "\\|")
        lines.append(f"| {op.get('effect')} | {op.get('operation')} | {subject} |")
    lines += [
        "",
        f"Digest `{doc['digest']}`. When this pull request merges, the publish job writes exactly "
        "this plan, or nothing: if Noru's state has changed by then, the digests differ and it "
        "refuses.",
        "",
    ]
    return "\n".join(lines)


# --- the publish job's checks -----------------------------------------------------------------

def check_acc(acc_manifest, merge_commit):
    """Condition 3: acc evaluated this merge and found it independently approved and clean."""
    events = acc_manifest.get("events") or {}
    changes = [c for c in events.get("changes") or [] if c.get("merge_commit_sha") == merge_commit]
    if len(changes) != 1:
        raise Refusal(f"the acc manifest has no change merged as {merge_commit[:12]}")
    change = changes[0]
    if not (events.get("window") or {}).get("complete") or not change.get("reviews_complete"):
        raise Refusal("acc's collection for the change is incomplete")
    approved = [
        a for a in acc_manifest.get("assessments") or []
        if a.get("change_id") == change["id"] and a.get("rule_id") == "ACC003"
    ]
    if not approved or approved[0].get("status") != "pass":
        raise Refusal(
            "acc does not record an approval independent of the change's effective author (ACC003)"
        )
    threshold = SEVERITY.get((acc_manifest.get("policy") or {}).get("fail_on", "medium"), 2)
    open_findings = [
        f["rule_id"] for f in acc_manifest.get("findings") or []
        if f.get("change_id") == change["id"]
        and SEVERITY.get(f.get("severity"), 3) >= threshold
        and (f.get("disposition") or {}).get("status", "open") == "open"
    ]
    if open_findings:
        raise Refusal(f"acc reports open findings for the change: {', '.join(sorted(set(open_findings)))}")
    return change


def check_provenance(reviewed, change, workflow, piece):
    """Conditions 1, 2 and 4, from what `fetch` recorded about where the plan came from."""
    if reviewed.get("format") != FORMAT:
        raise Refusal("the reviewed plan is not a noru-reviewed-plan/1 document")
    if reviewed.get("piece") != piece:
        raise Refusal(f"the reviewed plan is for {reviewed.get('piece')}, not {piece}")
    source = reviewed.get("provenance") or {}
    if source.get("same_repository") is not True:
        raise Refusal("the reviewed plan did not come from a pull request in this repository")
    if source.get("head_sha") != change.get("head_sha"):
        raise Refusal("the reviewed plan was computed on another commit than the approved head")
    if source.get("workflow") != workflow:
        raise Refusal(f"the reviewed plan was produced by {source.get('workflow')}, not {workflow}")
    if source.get("changed_workflow") is not False:
        raise Refusal(
            "the pull request changed the workflow that produced its own reviewed plan; it cannot "
            "confirm its own publication"
        )
    if source.get("changed_manifest") is not True:
        raise Refusal(
            "the pull request did not change the manifest, so no reviewed manifest is being "
            "published; a plan driven only by a change in Noru needs explicit confirmation"
        )


def verify(reviewed, rebuilt_plan, acc_manifest, merge_commit, workflow, piece):
    """Every condition, in order. Returns the confirmed digest or raises Refusal."""
    change = check_acc(acc_manifest, merge_commit)
    check_provenance(reviewed, change, workflow, piece)
    rebuilt = digest(rebuilt_plan)
    if reviewed.get("digest") != digest_of_document(reviewed):
        raise Refusal("the reviewed plan's digest does not match its own contents")
    if rebuilt != reviewed["digest"]:
        raise Refusal(
            f"the plan rebuilt after the merge ({rebuilt[:19]}) is not the reviewed plan "
            f"({reviewed['digest'][:19]}): Noru's state changed since the review. Nothing was "
            "written; review the new plan and confirm it explicitly"
        )
    return rebuilt


def digest_of_document(reviewed):
    return "sha256:" + hashlib.sha256(canonical(reviewed.get("plan") or {}).encode("utf-8")).hexdigest()


# --- fetch: the reviewed plan of the pull request a merge came from ---------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_base(env=os.environ):
    base = env.get("GITHUB_API_URL", DEFAULT_API).rstrip("/")
    url = urllib.parse.urlsplit(base)
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in LOOPBACK_HOSTS):
        raise ValueError("GITHUB_API_URL must use https")
    return base


class GitHub:
    def __init__(self, base, token):
        self.base = base
        self.token = token
        self.opener = urllib.request.build_opener(_NoRedirect)

    def request(self, path, authorized=True, raw=False):
        url = path if path.startswith(("http://", "https://")) else f"{self.base}{path}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if authorized and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers), timeout=60) as response:
                body = response.read(MAX_ARTIFACT_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                return {"redirect": error.headers.get("Location")}
            if error.code == 404:
                return None
            raise OSError(f"GitHub answered {error.code} for {urllib.parse.urlsplit(url).path}") from None
        if len(body) > MAX_ARTIFACT_BYTES:
            raise OSError("GitHub response exceeds the size limit")
        return body if raw else json.loads(body)

    def pages(self, path, key=None):
        items = []
        sep = "&" if "?" in path else "?"
        for page in range(1, MAX_PAGES + 1):
            value = self.request(f"{path}{sep}per_page=100&page={page}")
            batch = (value or {}).get(key, []) if key else (value or [])
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise OSError(f"{path} has more than {MAX_PAGES} pages")

    def download(self, path):
        """An artifact archive: GitHub redirects to a signed URL that must not get the token."""
        first = self.request(path)
        if not isinstance(first, dict) or not first.get("redirect"):
            raise OSError("GitHub did not redirect the artifact download")
        location = first["redirect"]
        scheme = urllib.parse.urlsplit(location)
        if scheme.scheme != "https" and scheme.hostname not in LOOPBACK_HOSTS:
            raise OSError("the artifact download is not https")
        return self.request(location, authorized=False, raw=True)


def fetch(gh, repository, commit, piece, workflow):
    """The reviewed plan, with where it came from, or None when this merge has none."""
    pulls = [
        p for p in gh.request(f"/repos/{repository}/commits/{commit}/pulls") or []
        if p.get("merged_at") and p.get("merge_commit_sha") == commit
    ]
    if len(pulls) != 1:
        return None, "the commit is not the merge of exactly one pull request"
    pull = pulls[0]
    head = pull["head"]["sha"]
    same_repository = (pull["head"].get("repo") or {}).get("full_name") == repository
    if not same_repository:
        return None, "the pull request came from a fork, which has no key to compute a plan with"
    runs = [
        r for r in gh.pages(
            f"/repos/{repository}/actions/runs?head_sha={head}&event=pull_request", "workflow_runs"
        )
        if r.get("path") == workflow and r.get("head_sha") == head and r.get("conclusion") == "success"
    ]
    if not runs:
        return None, f"no successful run of {workflow} on the pull request's head"
    run = max(runs, key=lambda r: (r.get("run_attempt", 1), r.get("id", 0)))
    artifacts = [
        a for a in gh.pages(f"/repos/{repository}/actions/runs/{run['id']}/artifacts", "artifacts")
        if a.get("name") == ARTIFACT_PREFIX + piece and not a.get("expired")
    ]
    if len(artifacts) != 1:
        return None, f"the run produced no {ARTIFACT_PREFIX}{piece} artifact"
    archive = gh.download(f"/repos/{repository}/actions/artifacts/{artifacts[0]['id']}/zip")
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        names = [n for n in z.namelist() if n.endswith("reviewed-plan.json")]
        if len(names) != 1:
            return None, "the artifact holds no reviewed-plan.json"
        reviewed = json.loads(z.read(names[0]))
    files = [f.get("filename") for f in gh.pages(f"/repos/{repository}/pulls/{pull['number']}/files")]
    reviewed["provenance"] = {
        "repository": repository,
        "pull_request": pull["number"],
        "head_sha": head,
        "same_repository": True,
        "workflow": workflow,
        "run_id": run["id"],
        "changed_workflow": workflow in files,
        "changed_manifest": f".noru/{piece}.yml" in files,
    }
    return reviewed, None


def main(argv):
    if not argv or argv[0] != "fetch":
        sys.stderr.write(__doc__.split("Usage:")[1])
        return 2
    opts = {}
    for arg in argv[1:]:
        key, _, value = arg.partition("=")
        opts[key.lstrip("-")] = value
    required = ("repository", "commit", "piece", "workflow", "out")
    if any(not opts.get(k) for k in required):
        sys.stderr.write(f"error: {', '.join('--' + k for k in required)} are required\n")
        return 2
    try:
        gh = GitHub(api_base(), os.environ.get("GITHUB_TOKEN"))
        reviewed, why = fetch(gh, opts["repository"], opts["commit"], opts["piece"], opts["workflow"])
    except ValueError as error:
        sys.stderr.write(f"error: {error}\n")
        return 2
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, KeyError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 3
    if reviewed is None:
        sys.stderr.write(f"no reviewed plan: {why}\n")
        return 1
    with open(opts["out"], "w", encoding="utf-8") as handle:
        handle.write(json.dumps(reviewed, indent=2, sort_keys=True) + "\n")
    if opts.get("output") == "json":
        sys.stdout.write(json.dumps({"ok": True, "out": opts["out"], "digest": reviewed.get("digest")}) + "\n")
    return 0


def git_head(repo):
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
