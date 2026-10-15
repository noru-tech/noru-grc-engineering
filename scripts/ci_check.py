#!/usr/bin/env python3
"""Run one piece headless: scan -> validate -> expiry -> policy -> (state -> diff -> push).

Standard library only, and by default no network and no credential — that is the whole design
constraint. A fork pull request has no secrets, so the checks that matter most on a pull request
have to be computable from the repository alone. Two of them are:

  1. **Manifest drift.** Re-run the collector and compare its derived facts against the digest
     recorded in the committed `.noru/<piece>.yml`. If they differ, someone changed the code without
     updating the record. Pure local computation: the collector opens no socket, and the digest it
     compares against is a field in the committed file.
  2. **Expired interpretation.** A claim whose expiry has passed, or that is outside the review
     cadence declared for this path. Local computation plus a calendar. See scripts/check_expiry.py.
  3. **Personal data nobody agreed to.** A data category, purpose or subject in the manifest that
     the committed privacy baseline does not permit. The baseline is a floor pinned from Noru so
     this stays offline; Noru is the truth. See scripts/check_policy.py.

The `state`, `diff` and publication half genuinely needs Noru, so it is opt-in, and when the inputs
it needs are absent it is reported as skipped rather than failed. With NORU_API_KEY present,
`state` builds the snapshot from the read tools the piece declares (scripts/mcp_exec.mjs state),
and `push` runs the piece's own confirmed :push and then, for an MCP-backed piece, executes exactly
the calls it emitted (scripts/mcp_exec.mjs exec). The push is reported as written only when the
executor's receipt says every call succeeded. A gate that breaks on every fork pull request gets
deleted in a week.

This file is driven entirely by `plugins/<piece>/piece.json` — entrypoints, artifact path, exit
codes. A piece scaffolded tomorrow works here with no change to this file, and there is a test that
says so.

Credentials: the job reaches Noru with NORU_API_KEY, or, when NORU_TRUST_ID names a CI trust record,
with a 15-minute token from Noru's CI token exchange (scripts/ci_credential.py), which then wins over
the key. The exchange runs once, the first time the state or push step needs Noru, and asks only for
the piece's read scopes, plus its write scopes when this run may push. Whether a credential is
available decides whether those steps run; a job with neither (a fork pull request) skips them. The
credential is passed through the process environment, as NORU_API_KEY, to the one child that sends
it (the REST piece's own push entrypoint, or scripts/mcp_exec.mjs), which reads it at the point of
use, and is never printed. The piece's MCP :push, which only writes a call list, never gets it.
Every step that does not need it is run with it removed from the child environment, and everything
captured from a child process is passed through redact() before it can reach stdout or the report.
The CI system's own credentials — a GitLab job token, the merge request note token, and whatever
would let a process obtain an OIDC ID token — are removed from every child: no step needs one.

Nothing here reads which CI system it is running on. `--code-quality` and `--security-report` write
the two report files a GitLab merge request reads (scripts/gitlab_reports.py); they are files, and a
pipeline on any system may write them.

Usage:
    python3 scripts/ci_check.py --piece=<name> [--repo=<path>] [--plugins=<dir>]
        [--mode=gate|warn] [--steps=scan,validate,expiry,policy,state,diff,push|all]
        [--as-of=YYYY-MM-DD] [--warn-within-days=N] [--max-age-days=N]
        [--fail-on=<kinds>|none] [--baseline=<path>] [--base-ref=<ref>] [--gate-on-new]
        [--state=<path>] [--write-reviewed-plan=<path>]
        [--reviewed-plan=<path> --acc-manifest=<path> --publish-workflow=<path>]
        [--on-missing-prerequisite=skip|fail] [--report=<path.json>]
        [--code-quality=<path.json>] [--security-report=<path.json>]
        [--security-findings=include|omit] [--output=json|text] [--quiet]

Exit codes — see docs/ci-mode.md for the table and the reasoning:
    0  every requested check passed, or --mode=warn
    1  more than one distinct failure condition; read the report
    2  usage error (you called this wrong)
    3  manifest drift
    4  an interpretation has expired or is outside the declared cadence
    5  the manifest failed validation
    6  a check could not run at all (missing runtime, unreadable declaration, child crash, or a
       collector that parsed no schema in a repository that visibly has one)
    7  the manifest processes personal data the privacy baseline does not permit
"""
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from check_expiry import (  # noqa: E402
    DEFAULT_WARN_WITHIN_DAYS,
    KINDS as EXPIRY_KINDS,
    evaluate as evaluate_expiry,
    parse_date,
)
from check_policy import (  # noqa: E402
    DEFAULT_FAIL_ON as POLICY_DEFAULT_FAIL_ON,
    KINDS as POLICY_KINDS,
    evaluate as evaluate_policy,
    finding_identity,
    load_document as load_baseline,
    load_special_categories,
    parse_document as parse_manifest_text,
)
from finding_docs import see as see_finding_docs  # noqa: E402
import ci_credential  # noqa: E402
import reviewed_plan  # noqa: E402
import gitlab_reports  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
MCP_EXEC = pathlib.Path(__file__).resolve().parent / "mcp_exec.mjs"

EXIT_OK = 0
EXIT_MIXED = 1
EXIT_USAGE = 2
EXIT_DRIFT = 3
EXIT_EXPIRY = 4
EXIT_INVALID = 5
EXIT_TOOLING = 6
EXIT_POLICY = 7

ALL_STEPS = ("scan", "validate", "expiry", "policy", "state", "diff", "push")
DEFAULT_STEPS = ("scan", "validate", "expiry", "policy")

FINDING_KINDS = ("drift", "invalid", "dangling_ref", "coverage") + EXPIRY_KINDS + POLICY_KINDS
DEFAULT_FAIL_ON = (
    "drift", "invalid", "expired", "cadence", "unparsable",
) + POLICY_DEFAULT_FAIL_ON

KIND_EXIT = {
    "drift": EXIT_DRIFT,
    "invalid": EXIT_INVALID,
    "dangling_ref": EXIT_DRIFT,
    # A partial map is reported, not gated, by default. Where it IS gated, it is a broken gate
    # and not a compliance finding, so it shares the tooling exit rather than getting its own.
    "coverage": EXIT_TOOLING,
    "expired": EXIT_EXPIRY,
    "cadence": EXIT_EXPIRY,
    "expiring": EXIT_EXPIRY,
    "unbounded": EXIT_EXPIRY,
    "unparsable": EXIT_EXPIRY,
    **{kind: EXIT_POLICY for kind in POLICY_KINDS},
}

# The validator runs on the interpreter this script is running on, never on whatever `python3`
# happens to resolve to. The validators pick PyYAML when it is importable and a bundled fallback
# otherwise, so two interpreters means two loaders, and a manifest that passes here and fails
# three lines later. One interpreter is the only way this file can promise what it checked.
PYTHON = sys.executable or "python3"

# Credentials a CI system puts in a job's environment that no step of this script has any use for.
# NORU_API_KEY is handled separately, because one step does need it.
FOREIGN_CREDENTIALS = ("CI_JOB_TOKEN", "NORU_GITLAB_NOTE_TOKEN")

MAX_EXPLAINED_ITEMS = 40
MAX_REF_FILE_BYTES = 4_000_000
REF_RE = re.compile(r"^([^:\s][^:]*):([0-9]+)$")
IDENTITY_FIELDS = ("key", "file", "id", "name")

USAGE = (
    "usage: ci_check.py --piece=<name> [--repo=<path>] [--plugins=<dir>] [--mode=gate|warn]\n"
    "                   [--steps=scan,validate,expiry,policy,state,diff,push|all]\n"
    "                   [--as-of=YYYY-MM-DD]\n"
    "                   [--warn-within-days=N] [--max-age-days=N] [--fail-on=<kinds>|none]\n"
    "                   [--baseline=<path>] [--base-ref=<ref>] [--gate-on-new]\n"
    "                   [--state=<path>] [--write-reviewed-plan=<path>]\n"
    "                   [--reviewed-plan=<path> --acc-manifest=<path> --publish-workflow=<path>]\n"
    "                   [--on-missing-prerequisite=skip|fail]\n"
    "                   [--report=<path.json>]\n"
    "                   [--code-quality=<path.json>] [--security-report=<path.json>]\n"
    "                   [--security-findings=include|omit]\n"
    "                   [--output=json|text] [--quiet]\n"
)

# Mirrors redact() in plugins/noru/scripts/lib/plan.mjs. A piece never handles a secret, but an
# HTTP error body can echo a header back, and this script prints child output.
_REDACTIONS = (
    (re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"\bnoru_[A-Za-z0-9_-]{6,}"), "<redacted>"),
    # An OIDC ID token, or any other JWT: three base64url segments, the first a JSON header.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "<redacted>"),
    (
        re.compile(
            r"(\"?(?:api[_-]?key|authorization|token|secret|password)\"?\s*[:=]\s*\"?)([^\"\s,}]{6,})",
            re.IGNORECASE,
        ),
        r"\1<redacted>",
    ),
)


def redact(value):
    text = value if isinstance(value, str) else json.dumps(value)
    for secret in ci_credential.SECRETS:
        text = text.replace(secret, "<redacted>")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


# The job's credential, resolved once, the first time a step needs Noru. GitLab mints one ID token
# per job and each exchanges once, so a second exchange in the same job could not succeed anyway.
_CREDENTIAL = {"resolved": False, "credential": None, "unavailable": None, "refused": None}
_CREDENTIAL_SCOPES = []


def credential(report):
    """("ok", None), ("absent", why) or ("refused", why). Records the source in the report."""
    if not _CREDENTIAL["resolved"]:
        _CREDENTIAL["resolved"] = True
        try:
            _CREDENTIAL["credential"] = ci_credential.resolve(_CREDENTIAL_SCOPES)
            report.payload["credential"] = _CREDENTIAL["credential"].describe()
        except ci_credential.Unavailable as exc:
            # Names variables, never a value, and redact() would read `id-token: write` as one.
            _CREDENTIAL["unavailable"] = str(exc)
        except ci_credential.Refused as exc:
            _CREDENTIAL["refused"] = redact(str(exc))
    if _CREDENTIAL["credential"] is not None:
        return "ok", None
    if _CREDENTIAL["refused"]:
        return "refused", _CREDENTIAL["refused"]
    return "absent", _CREDENTIAL["unavailable"]


class Report:
    def __init__(self, piece, repo, mode, policy):
        self.payload = {
            "check": "ci",
            "piece": piece,
            "repo": str(repo),
            "mode": mode,
            "policy": policy,
            "status": "pass",
            "steps": [],
            "findings": [],
            "counts": {},
        }

    def step(self, name, status, **detail):
        row = {"step": name, "status": status}
        row.update(detail)
        self.payload["steps"].append(row)
        return row

    def find(self, kind, message, **detail):
        finding = {"kind": kind, "message": message}
        finding.update(detail)
        self.payload["findings"].append(finding)
        return finding

    @property
    def findings(self):
        return self.payload["findings"]

    @property
    def steps(self):
        return self.payload["steps"]


def run(cmd, cwd=None, with_api_key=False):
    """Run a child process. NORU_API_KEY is removed unless the step is the one that needs it.

    The one that needs it gets the job's credential under that name: the API key, or the token
    the CI token exchange issued. No child gets what it would take to obtain an ID token.
    """
    env = dict(os.environ)
    env.pop("NORU_API_KEY", None)
    for name in ci_credential.ID_TOKEN_SOURCES:
        env.pop(name, None)
    if with_api_key and _CREDENTIAL["credential"] is not None:
        env["NORU_API_KEY"] = _CREDENTIAL["credential"].token
    for name in FOREIGN_CREDENTIALS:
        env.pop(name, None)
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, check=False, cwd=cwd, env=env
        )
    except FileNotFoundError as exc:
        return None, f"cannot execute {cmd[0]}: {exc}"
    except subprocess.TimeoutExpired:
        return None, f"{cmd[0]} timed out after 600s"
    return completed, None


def parse_child_json(completed):
    """Child stdout as JSON, or None. Steps print JSON on success and prose on a hard failure."""
    text = (completed.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def first_error_line(completed):
    for stream in (completed.stderr or "", completed.stdout or ""):
        for line in stream.splitlines():
            if line.strip():
                return redact(line.strip())[:400]
    return ""


# --- drift, explained ---------------------------------------------------------------------------
def _identity_of(node):
    for field in IDENTITY_FIELDS:
        value = node.get(field)
        if isinstance(value, str) and value.strip():
            return field, value.strip()
    return None, None


def _first_ref(node):
    ref = node.get("ref")
    if isinstance(ref, str) and REF_RE.match(ref):
        return ref
    refs = node.get("refs")
    if isinstance(refs, list):
        for candidate in refs:
            if isinstance(candidate, str) and REF_RE.match(candidate):
                return candidate
    return None


def derived_identities(node, path="", out=None):
    """Every named thing the collector found, with the group it belongs to and where it was seen.

    Generic on purpose: it keys off shape (`key`/`file`/`id`/`name` plus `refs`), not off any one
    piece's derived-facts layout, so it explains a drift for a piece written after this file.
    """
    if out is None:
        out = []
    if isinstance(node, dict):
        field, identity = _identity_of(node)
        if identity is not None:
            out.append(
                {
                    "group": path or "<root>",
                    "field": field,
                    "identity": identity,
                    "ref": _first_ref(node),
                }
            )
        for key, child in node.items():
            derived_identities(child, f"{path}.{key}" if path else key, out)
    elif isinstance(node, list):
        for index, child in enumerate(node):
            if isinstance(child, str):
                # A bare string in a list is an identity too (a prompt file, a path), but a
                # file:line citation is line-level noise and would swamp the explanation.
                if len(child) > 3 and not REF_RE.match(child):
                    out.append(
                        {"group": path or "<root>", "field": None, "identity": child, "ref": None}
                    )
                continue
            derived_identities(child, f"{path}[{index}]", out)
    return out


def dangling_refs(manifest_document, repo):
    """Manifest citations that no longer resolve: the file is gone, or the line is past its end."""
    problems = []
    seen = set()

    def visit(node, path=""):
        if isinstance(node, dict):
            for key, child in node.items():
                visit(child, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{path}[{index}]")
        elif isinstance(node, str):
            match = REF_RE.match(node)
            if not match or node in seen:
                return
            seen.add(node)
            rel, line_no = match.group(1), int(match.group(2))
            target = repo / rel
            if not target.is_file():
                problems.append(
                    {"path": path, "ref": node, "reason": f"{rel} no longer exists in the repository"}
                )
                return
            try:
                if target.stat().st_size > MAX_REF_FILE_BYTES:
                    return
                lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return
            if line_no > len(lines):
                problems.append(
                    {
                        "path": path,
                        "ref": node,
                        "reason": f"{rel} now has {len(lines)} line(s), so line {line_no} is past its end",
                    }
                )

    visit(manifest_document)
    return problems


def explain_drift(repo, manifest_path, derived_path):
    """A readable account of *what* changed, next to the digest that says *that* it changed."""
    explanation = {"new_in_repository": [], "note": None}
    manifest_exists = manifest_path.is_file()
    try:
        derived = json.loads(derived_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        explanation["note"] = "the collector's derived facts could not be read, so only the digest is reported"
        return explanation
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        manifest_text = ""

    seen = set()
    for row in derived_identities(derived):
        marker = (row["group"], row["identity"])
        if marker in seen:
            continue
        seen.add(marker)
        if row["identity"] in manifest_text:
            continue
        explanation["new_in_repository"].append(row)

    total = len(explanation["new_in_repository"])
    explanation["new_in_repository"].sort(key=lambda r: (r["group"], r["identity"]))
    if total > MAX_EXPLAINED_ITEMS:
        explanation["new_in_repository"] = explanation["new_in_repository"][:MAX_EXPLAINED_ITEMS]
        explanation["note"] = f"{total} items found in the repository and not named in the manifest; showing the first {MAX_EXPLAINED_ITEMS}"
    elif total == 0 and manifest_exists:
        # An honest "I cannot itemise this" beats an empty list that reads like "nothing changed".
        explanation["note"] = (
            "the derived facts changed but nothing named appeared or disappeared — the difference is "
            "in line positions or counts, so re-run :scan and read the manifest diff"
        )
    return explanation


def drift_message(manifest_path, decl):
    """Three different situations produce the same collector exit code. Say which one this is."""
    if not manifest_path.is_file():
        return f"no committed manifest at {decl['artifact']} — run the piece's :scan and commit it"
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if "derived_digest" not in text:
        return (
            f"{decl['artifact']} records no `source.derived_digest`, so there is nothing to compare "
            "it against — re-run the piece's :scan so the collector stamps one"
        )
    return "the committed manifest no longer matches the repository"


def check_coverage(report, repo, summary, decl, reconciliation=None):
    """Did the collector actually read anything, and did it miss something it could see was there?

    An empty manifest and a repository with no personal data in it are the same file, and only one
    of them is good news. Every check downstream — drift, expiry, policy — passes cleanly on an
    empty map, so a collector that parsed nothing is not a clean result, it is a **broken gate**.
    That is exit `6`, which `--mode=warn` deliberately does not suppress, and it is the same rule
    docs/ci-mode.md already states: a check that could not run is not a check that passed.

    Piece-agnostic in shape, like everything else here: any piece whose derived facts carry a
    `coverage` block gets this check, and one that does not is unaffected.
    """
    derived_rel = (summary or {}).get("derived_facts")
    if not derived_rel:
        return "ok"
    try:
        derived = json.loads((repo / derived_rel).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "ok"
    coverage = derived.get("coverage")
    if not isinstance(coverage, dict):
        return "ok"

    candidates = [
        *(coverage.get("unparsed_candidates") or []),
        *(coverage.get("migration_gaps") or []),
        *(coverage.get("schema_conflicts") or []),
    ]
    partition = (reconciliation or {}).get("extraction_diagnostics")
    if isinstance(partition, dict) and partition.get("version") == 1 and isinstance(partition.get("active_blockers"), list):
        # Canonical semantic/write investigations still block the validator. Only collector
        # coverage failures establish that extraction produced no safe schema here.
        candidates = [gap for gap in partition["active_blockers"] if gap.get("coverage_key") in {"unparsed_candidates", "migration_gaps", "schema_conflicts"}]
    parsed = coverage.get("files_parsed", 0)
    safe_datasets = (derived.get("counts") or {}).get("datasets", 0)
    if not candidates:
        return "ok"

    formats = sorted({c.get("format", c.get("kind", "extraction")) for c in candidates if isinstance(c, dict)})
    shown = [c.get("ref") for c in candidates[:MAX_EXPLAINED_ITEMS] if isinstance(c, dict)]

    if safe_datasets == 0:
        report.step("scan", "error", detail=f"no safe dataset normalized; {len(candidates)} gap(s) found")
        report.find(
            "coverage",
            f"the collector produced no safe schema, but found {len(candidates)} structural "
            f"coverage gap(s) ({', '.join(formats)}). An empty data map is "
            "not the same as a repository with no personal data in it, and every check after this "
            "one would have passed on the empty set",
            manifest=decl["artifact"],
            formats=formats,
            refs=shown,
        )
        return "tooling"

    report.find(
        "coverage",
        f"{len(candidates)} structural observation(s) could not be normalized safely "
        f"({', '.join(formats)}), so the data map does not describe them. {parsed} file(s) were "
        "parsed, so the map is partial rather than empty",
        manifest=decl["artifact"],
        formats=formats,
        refs=shown,
    )
    return "ok"


# --- the steps ----------------------------------------------------------------------------------
def step_scan(report, piece_dir, decl, repo, manifest_path, on_missing_prereq):
    entry = piece_dir / decl["collector"]["entrypoint"]
    if not entry.is_file():
        report.step("scan", "error", detail=f"collector entrypoint {entry} does not exist")
        return "tooling"

    completed, failure = run(["node", str(entry), f"--repo={repo}", "--check", "--output=json", "--quiet"])
    if failure:
        report.step("scan", "error", detail=redact(failure))
        return "tooling"

    summary = parse_child_json(completed)
    # A piece may declare a second, finer deterministic comparison. The collector still owns the
    # global freshness gate; a reconciler explains drift per entity so CI and an agent see the same
    # bounded queue. It is deliberately a local process, never a model call.
    reconciliation = None
    reconcile_decl = decl.get("reconciler")
    reconciler = piece_dir / reconcile_decl["entrypoint"] if reconcile_decl else None
    if reconciler is not None:
        if not reconciler.is_file():
            report.step(
                "scan", "error", detail=f"reconciler entrypoint {reconciler} does not exist"
            )
            return "tooling"
        executable = "node" if reconcile_decl.get("runtime") == "node" else PYTHON
        reconciled, reconcile_failure = run(
            [executable, str(reconciler), f"--repo={repo}", "--output=json", "--quiet"]
        )
        if reconcile_failure:
            report.step("scan", "error", detail=redact(reconcile_failure))
            return "tooling"
        if reconciled.returncode != 0:
            report.step(
                "scan",
                "error",
                detail=first_error_line(reconciled) or f"reconciler exit {reconciled.returncode}",
            )
            return "tooling"
        reconciliation = parse_child_json(reconciled)
        if not isinstance(reconciliation, dict):
            report.step("scan", "error", detail="reconciler did not return a JSON object")
            return "tooling"

    coverage_outcome = check_coverage(report, repo, summary, decl, reconciliation)
    if coverage_outcome == "tooling":
        return "tooling"

    if reconciliation is not None:
        investigations = reconciliation.get("investigation_required") or []
        controls = reconciliation.get("control_changes") or []
        if investigations or controls:
            report.find("invalid", "decision evidence or baseline metadata needs review; this is not a confirmed privacy change",
                        manifest=decl["artifact"], explanation={"investigations": investigations, "control_changes": controls})
            report.step("scan", "fail", derived_digest=(summary or {}).get("derived_digest"))
            return "finding"

    if (
        completed.returncode == 0
        and reconciliation is not None
        and reconciliation.get("drift") is True
    ):
        explanation = {
            "reconciliation": {
                "mode": reconciliation.get("mode"),
                "counts": reconciliation.get("counts"),
                "proposal_required": reconciliation.get("proposal_required"),
                "identity_ambiguities": reconciliation.get("identity_ambiguities"),
                "collection_review_required": reconciliation.get(
                    "collection_review_required"
                ),
            }
        }
        report.step("scan", "fail", derived_digest=(summary or {}).get("derived_digest"))
        report.find(
            "drift",
            "the accepted observation lock no longer matches the repository or manifest",
            manifest=decl["artifact"],
            derived_digest=(summary or {}).get("derived_digest"),
            explanation=explanation,
        )
        return "finding"

    if completed.returncode == 0:
        report.step("scan", "pass", derived_digest=(summary or {}).get("derived_digest"))
        return "ok"

    if completed.returncode == 1 and summary is not None and summary.get("drift") is True:
        derived_rel = summary.get("derived_facts") or f".noru/.cache/{decl['piece']}.derived.json"
        explanation = explain_drift(repo, manifest_path, repo / derived_rel)
        if reconciliation is not None:
            explanation["reconciliation"] = {
                "mode": reconciliation.get("mode"),
                "counts": reconciliation.get("counts"),
                "proposal_required": reconciliation.get("proposal_required"),
                "identity_ambiguities": reconciliation.get("identity_ambiguities"),
                "collection_review_required": reconciliation.get(
                    "collection_review_required"
                ),
            }
        report.step("scan", "fail", derived_digest=summary.get("derived_digest"))
        report.find("drift", drift_message(manifest_path, decl), manifest=decl["artifact"],
                    derived_digest=summary.get("derived_digest"), explanation=explanation)
        return "finding"

    if completed.returncode in (1, 2):
        # A prerequisite the collector needs is missing (for a piece whose queue comes from Noru,
        # that is the normal state on a fork pull request). Not drift, and not a broken build.
        detail = first_error_line(completed)
        report.step("scan", "blocked", detail=detail)
        return "blocked" if on_missing_prereq == "skip" else "tooling"

    report.step("scan", "error", detail=first_error_line(completed) or f"exit {completed.returncode}")
    return "tooling"


def renewal_base(repo, artifact, base_ref):
    """The commit an agent's renewal is judged against, or None when it cannot be read.

    With --base-ref, the merge base, as the policy step uses. Without it, the commit the manifest
    was changed against: HEAD when the working tree changes it, otherwise HEAD's first parent, which
    on a push to a protected branch is the branch before the merge.
    """
    if git_read(repo, "rev-parse", "--git-dir") is None:
        return None
    if base_ref:
        if git_read(repo, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}") is None:
            return None
        merge_base = git_read(repo, "merge-base", "HEAD", base_ref)
        return merge_base.strip() if merge_base else None
    if git_read(repo, "diff", "--quiet", "HEAD", "--", artifact) is None:
        head = git_read(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        return head.strip() if head else None
    parent = git_read(repo, "rev-parse", "--verify", "--quiet", "HEAD^1^{commit}")
    return parent.strip() if parent else None


def renewal_args(piece_dir, decl, repo, base_ref):
    """--base and --interpretation-policy, both as they stood at the base revision.

    Only for a piece that lets an agent own a claim. Reading the policy from the base means a pull
    request that widens it is judged by the policy it would replace. A file absent there is passed
    as an empty one; a base that cannot be read at all is passed as an empty flag, and the
    validator then refuses every agent-owned claim rather than taking it on trust.
    """
    owners = decl.get("interpretation", {}).get("agent_owners")
    if not owners:
        return [], None
    commit = renewal_base(repo, decl["artifact"], base_ref)
    if commit is None:
        return ["--base=", "--interpretation-policy="], "unavailable"
    cache = repo / ".noru" / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    args = []
    for flag, rel, suffix in (
        ("--base", decl["artifact"], "base-manifest.yml"),
        ("--interpretation-policy", owners["policy"], "base-interpretation-policy.yml"),
    ):
        target = cache / f"{piece_dir.name}.{suffix}"
        target.write_text(git_read(repo, "show", f"{commit}:{rel}") or "", encoding="utf-8")
        args.append(f"{flag}={target}")
    return args, commit[:12]


def step_validate(report, piece_dir, decl, repo, manifest_path, parsed_path, base_ref=None):
    entry = piece_dir / decl["validator"]["entrypoint"]
    if not entry.is_file():
        report.step("validate", "error", detail=f"validator entrypoint {entry} does not exist")
        return "tooling"
    if not manifest_path.is_file():
        report.step("validate", "blocked", detail=f"no manifest at {manifest_path}")
        return "blocked"
    renewal, base = renewal_args(piece_dir, decl, repo, base_ref)
    judged = {"renewal_base": base} if base else {}

    parsed_path.parent.mkdir(parents=True, exist_ok=True)
    # No --as-of is passed, even to a validator that accepts one. Time is checked once, in the
    # expiry step, so a single stale claim cannot come back as two different exit codes from two
    # different steps. Validation stays a question about the file, not about the day.
    completed, failure = run(
        [
            PYTHON,
            str(entry),
            str(manifest_path),
            f"--emit-parsed={parsed_path}",
            "--output=json",
            "--quiet",
            *renewal,
        ]
    )
    if failure:
        report.step("validate", "error", detail=redact(failure))
        return "tooling"

    result = parse_child_json(completed)
    if completed.returncode == 0:
        report.step("validate", "pass", counts=(result or {}).get("counts"), **judged)
        return "ok"
    if completed.returncode == 1 and result is not None:
        errors = result.get("errors") or []
        report.step("validate", "fail", errors=len(errors), **judged)
        for error in errors[:MAX_EXPLAINED_ITEMS]:
            report.find(
                "invalid",
                redact(error.get("message", "")),
                path=error.get("path"),
                manifest=decl["artifact"],
            )
        if len(errors) > MAX_EXPLAINED_ITEMS:
            report.find(
                "invalid",
                f"{len(errors) - MAX_EXPLAINED_ITEMS} further validation error(s) not listed",
                path="<truncated>",
                manifest=decl["artifact"],
            )
        return "finding"

    report.step("validate", "error", detail=first_error_line(completed) or f"exit {completed.returncode}")
    return "tooling"


def step_expiry(report, repo, parsed_path, policy):
    if not parsed_path.is_file():
        report.step("expiry", "blocked", detail="the manifest did not validate, so there is nothing to age")
        return "blocked"
    try:
        document = json.loads(parsed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.step("expiry", "error", detail=f"cannot read {parsed_path} ({exc})")
        return "tooling"

    findings, counts = evaluate_expiry(
        document, policy["as_of_date"], policy["warn_within_days"], policy["max_age_days"]
    )
    for problem in dangling_refs(document, repo):
        findings.append(
            {
                "kind": "dangling_ref",
                "path": problem["path"],
                "field": None,
                "value": problem["ref"],
                "owner": None,
                "subject": problem["ref"],
                "message": f"citation no longer resolves: {problem['reason']}",
            }
        )
        counts["dangling_ref"] = counts.get("dangling_ref", 0) + 1

    for finding in findings:
        report.find(
            finding["kind"],
            finding["message"],
            path=finding.get("path"),
            field=finding.get("field"),
            value=finding.get("value"),
            owner=finding.get("owner"),
            subject=finding.get("subject"),
        )
    failing = [f for f in findings if f["kind"] in policy["fail_on"]]
    report.step("expiry", "fail" if failing else "pass", claims=counts.get("claims", 0))
    return "finding" if failing else "ok"


def git_read(repo, *args):
    """Run a read-only git command in `repo`. Returns stdout, or None if git could not answer.

    Every call here reads history that is already on disk: no fetch, no remote, no credential. A
    shallow clone that does not contain the merge base is the ordinary failure and is reported as a
    skip, because a delta nobody can compute is not a delta that failed.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def base_manifest(repo, base_ref, artifact):
    """The committed manifest as it stood at the merge base with `base_ref`.

    This is what makes a policy finding answer "did this pull request introduce it?" rather than
    only "is it there?". Comparing manifests rather than re-running the collector at the base is
    deliberate: the manifest is the reviewed record, it is what the policy step evaluates at HEAD,
    and reading it costs one `git show` instead of a second checkout and a second collector run.

    Returns (document, None) or (None, reason-it-could-not-be-done).
    """
    if git_read(repo, "rev-parse", "--git-dir") is None:
        return None, f"{repo} is not a git repository, so there is no base to compare against"
    if git_read(repo, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}") is None:
        return None, (
            f"--base-ref={base_ref} does not resolve in this checkout. A CI job that clones with "
            "depth 1 does not have it; fetch the base branch, or drop the flag"
        )
    merge_base = git_read(repo, "merge-base", "HEAD", base_ref)
    if not merge_base:
        return None, f"HEAD and {base_ref} share no history, so there is no merge base"
    merge_base = merge_base.strip()

    blob = git_read(repo, "show", f"{merge_base}:{artifact}")
    if blob is None:
        # A manifest that did not exist at the base is not an error: every finding in it is new,
        # which is exactly what an empty base document expresses.
        return {}, None
    try:
        return parse_manifest_text(blob), None
    except Exception as exc:  # noqa: BLE001
        return None, f"the manifest at {merge_base[:12]} could not be parsed ({exc})"


def mark_first_seen(findings, base_document, baseline, special_roots):
    """Stamp every finding with whether this branch introduced it.

    The base manifest is evaluated against *today's* baseline, not the baseline as it stood then.
    The question being answered is "which of these findings is this pull request responsible for",
    and widening or narrowing the policy is a change to the policy, judged in its own diff.
    """
    if base_document is None:
        return
    before, _counts = evaluate_policy(base_document, baseline, special_roots)
    seen = {finding_identity(f) for f in before}
    for finding in findings:
        finding["first_seen"] = "pre_existing" if finding_identity(finding) in seen else "this_pr"


def step_policy(report, repo, parsed_path, baseline_source, delta=None):
    """Is the personal data in this manifest data the organization agreed to process?

    Skips rather than fails when there is no baseline. A repository that has not agreed a taxonomy
    yet has nothing for this step to check, and inventing a default policy would be exactly the
    "ship an opinion" failure the contract's requirement 9 exists to prevent. What it must never do
    is report that as a pass, so it says `skipped` and names the file it looked for.
    """
    if baseline_source is not None:
        baseline_path = pathlib.Path(baseline_source)
        if not baseline_path.is_file():
            report.step("policy", "error", detail=f"--baseline={baseline_source} does not exist")
            return "tooling"
    else:
        baseline_path = repo / ".noru" / "privacy-baseline.yml"
        if not baseline_path.is_file():
            report.step(
                "policy",
                "skipped",
                detail=(
                    f"no privacy baseline at {baseline_path.relative_to(repo)} — agree one and "
                    "commit it, or pass --baseline. This step has no default policy of its own"
                ),
            )
            return "skipped"

    if not parsed_path.is_file():
        report.step(
            "policy", "blocked", detail="the manifest did not validate, so there is nothing to check"
        )
        return "blocked"

    try:
        document = json.loads(parsed_path.read_text(encoding="utf-8"))
        baseline = load_baseline(baseline_path)
    except Exception as exc:  # noqa: BLE001 - an unreadable baseline is a broken gate, not a finding
        report.step("policy", "error", detail=f"cannot read {baseline_path} ({exc})")
        return "tooling"

    if not isinstance(baseline, dict) or baseline.get("kind") != "privacy-baseline":
        report.step(
            "policy",
            "error",
            detail=(
                f"{baseline_path} is not a privacy baseline (kind is "
                f"{baseline.get('kind') if isinstance(baseline, dict) else type(baseline).__name__!r})"
            ),
        )
        return "tooling"

    special_roots = load_special_categories()
    findings, counts = evaluate_policy(document, baseline, special_roots)

    delta_note = None
    if delta and delta.get("base_ref"):
        base_document, reason = base_manifest(repo, delta["base_ref"], delta["artifact"])
        if base_document is None:
            # A delta that cannot be computed must not silently become "everything is new", which
            # would gate the whole backlog, nor "everything is old", which would gate nothing.
            delta_note = f"no base comparison: {reason}"
        else:
            mark_first_seen(findings, base_document, baseline, special_roots)
            new = sum(1 for f in findings if f.get("first_seen") == "this_pr")
            counts["first_seen_this_pr"] = new
            counts["first_seen_pre_existing"] = len(findings) - new
            delta_note = f"{new} of {len(findings)} finding(s) new since {delta['base_ref']}"

    for finding in findings:
        report.find(
            finding["kind"],
            finding["message"],
            path=finding.get("path"),
            field=None,
            value=finding.get("value"),
            owner=None,
            subject=finding.get("subject"),
            first_seen=finding.get("first_seen"),
        )
    failing = [
        f for f in findings
        if f["kind"] in report.payload["policy"]["fail_on"]
        and not (delta and delta.get("gate_on_new") and f.get("first_seen") == "pre_existing")
    ]
    detail = f"against {baseline_path}"
    if delta_note:
        detail = f"{detail} — {delta_note}"
    report.step(
        "policy",
        "fail" if failing else "pass",
        claims=counts.get("carriers", 0),
        detail=detail,
    )
    return "finding" if failing else "ok"


def step_state(report, decl, repo, plugins, state_source):
    """Build .noru/.cache/noru-state.json from the read tools the piece declares."""
    if state_source is not None:
        report.step("state", "skipped", detail=f"a snapshot was passed with --state={state_source}; it is used as given")
        return "skipped"
    declared = decl.get("state") or {}
    if declared.get("headless") is not True:
        report.step(
            "state",
            "skipped",
            detail=(
                "this piece declares no headless snapshot: "
                + (declared.get("reason") or "piece.json has no state declaration")
                + " Build it in an authenticated MCP host and pass --state=<path>."
            ),
        )
        return "skipped"
    # The value is handed to the executor, which reads it at the point of use.
    available, why = credential(report)
    if available == "refused":
        report.step("state", "error", detail=why)
        return "tooling"
    if available == "absent":
        report.step(
            "state",
            "skipped",
            detail=f"{why}, so this job cannot read the organization. This is the expected state on a fork pull request.",
        )
        return "skipped"
    completed, failure = run(
        ["node", str(MCP_EXEC), "state", f"--piece={decl['piece']}", f"--repo={repo}",
         f"--plugins={plugins}", "--output=json", "--quiet"],
        with_api_key=True,
    )
    if failure:
        report.step("state", "error", detail=redact(failure))
        return "tooling"
    if completed.returncode != 0:
        report.step("state", "error", detail=first_error_line(completed) or f"exit {completed.returncode}")
        return "tooling"
    result = parse_child_json(completed) or {}
    report.step("state", "pass", reads=result.get("reads"))
    return "ok"


def step_diff(report, piece_dir, decl, repo, state_source, write_reviewed=None):
    state_path = repo / ".noru" / ".cache" / "noru-state.json"
    if state_source is not None:
        source = pathlib.Path(state_source)
        if not source.is_file():
            report.step("diff", "error", detail=f"--state={state_source} does not exist")
            return "tooling"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != state_path.resolve():
            shutil.copyfile(source, state_path)
    if not state_path.is_file():
        report.step(
            "diff",
            "skipped",
            detail=(
                "no Noru state snapshot at .noru/.cache/noru-state.json — :diff compares against the "
                "organization, which needs a read-scoped connection this job does not have. Pass "
                "--state=<path> in a job that can produce one."
            ),
        )
        return "skipped"

    entry = piece_dir / decl["push"]["entrypoint"]
    diff_entry = entry.parent / "diff.mjs"
    if not diff_entry.is_file():
        report.step("diff", "error", detail=f"no diff entrypoint at {diff_entry}")
        return "tooling"

    completed, failure = run(["node", str(diff_entry), f"--repo={repo}", "--output=json", "--quiet"])
    if failure:
        report.step("diff", "error", detail=redact(failure))
        return "tooling"
    if completed.returncode != 0:
        report.step("diff", "error", detail=first_error_line(completed) or f"exit {completed.returncode}")
        return "tooling"

    plan = parse_child_json(completed) or {}
    row = report.step("diff", "pass", summary=plan.get("summary"))
    if write_reviewed:
        # The plan a reviewer reads on the pull request, and the digest a publish job will require
        # of the plan it rebuilds after the merge (docs/ci-mode.md, "Confirmation by a reviewed
        # plan").
        try:
            written = json.loads(plan_path(repo, decl).read_text(encoding="utf-8"))
            doc = reviewed_plan.document(written)
            out = pathlib.Path(write_reviewed)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            out.with_suffix(".md").write_text(reviewed_plan.render_markdown(doc), encoding="utf-8")
        except (OSError, json.JSONDecodeError, reviewed_plan.Refusal) as exc:
            report.step("diff", "error", detail=f"could not write the reviewed plan: {redact(str(exc))}")
            return "tooling"
        row["reviewed_plan"] = {"path": str(out), "digest": doc["digest"]}
    return "ok"


def plan_path(repo, decl):
    return repo / ".noru" / ".cache" / f"{decl['piece']}.plan.json"


def confirm_reviewed(report, decl, repo, confirm):
    """Confirmation by a reviewed plan: `ok`, `skipped` (no reviewed plan for this merge) or `refused`."""
    source = pathlib.Path(confirm["reviewed_plan"])
    if not source.is_file():
        report.step(
            "push",
            "skipped",
            detail=(
                f"no reviewed plan at {source}: this merge came from a fork, from a direct push, or "
                "from a pull request whose workflow produced none, so nothing confirms its plan. "
                "Publish it through the piece's :diff and an explicit confirmation."
            ),
        )
        return "skipped"
    head = git_read(repo, "rev-parse", "HEAD")
    try:
        reviewed = json.loads(source.read_text(encoding="utf-8"))
        acc = json.loads(pathlib.Path(confirm["acc_manifest"]).read_text(encoding="utf-8"))
        rebuilt = json.loads(plan_path(repo, decl).read_text(encoding="utf-8"))
        if not any(op.get("effect") != "skip" for op in rebuilt.get("operations") or []):
            # Nothing to write needs no confirmation: a re-run after the publication, or a merge
            # whose writes are already in Noru.
            report.payload["confirmed_by"] = {"reviewed_plan": None, "reason": "nothing to write"}
            return "ok"
        if not head:
            raise reviewed_plan.Refusal("the merge commit cannot be read from the checkout")
        confirmed = reviewed_plan.verify(
            reviewed, rebuilt, acc, head.strip(), confirm["publish_workflow"], decl["piece"]
        )
    except (OSError, json.JSONDecodeError) as exc:
        report.step("push", "error", detail=f"cannot read the confirmation inputs: {redact(str(exc))}")
        return "refused"
    except reviewed_plan.Refusal as refusal:
        report.step("push", "error", detail=f"not confirmed by a reviewed plan: {refusal}")
        return "refused"
    report.payload["confirmed_by"] = {"reviewed_plan": confirmed, "merge_commit": head.strip()}
    return "ok"


def step_push(report, piece_dir, decl, repo, diff_outcome, confirm=None):
    if diff_outcome != "ok":
        report.step("push", "skipped", detail="the diff step did not produce a plan, so there is nothing to push")
        return "skipped"
    if confirm:
        confirmation = confirm_reviewed(report, decl, repo, confirm)
        if confirmation == "skipped":
            return "skipped"
        if confirmation == "refused":
            return "tooling"
    transports = {
        operation.get("transport") for operation in decl.get("push", {}).get("operations", [])
    }
    if transports not in ({"rest"}, {"mcp"}):
        report.step("push", "error", detail=f"unsupported push transport set: {sorted(transports)}")
        return "tooling"

    # Whichever child performs the writes (the REST piece's own push entrypoint, or
    # scripts/mcp_exec.mjs) gets the credential and reads it at the point of use.
    available, why = credential(report)
    if available == "refused":
        report.step("push", "error", detail=why)
        return "tooling"
    if available == "absent":
        report.step(
            "push",
            "skipped",
            detail=f"{why}, so this job cannot write to Noru. This is the expected state on a fork pull request; the checks above still ran.",
        )
        return "skipped"

    entry = piece_dir / decl["push"]["entrypoint"]
    if not entry.is_file():
        report.step("push", "error", detail=f"push entrypoint {entry} does not exist")
        return "tooling"

    # The piece's own :push makes the confirmed, plan-bound call list. For an MCP piece it writes a
    # file and nothing else, so it never sees the key; for the REST piece it is the writer.
    completed, failure = run(
        ["node", str(entry), f"--repo={repo}", "--confirm", "--output=json", "--quiet"],
        with_api_key=transports == {"rest"},
    )
    if failure:
        report.step("push", "error", detail=redact(failure))
        return "tooling"
    if completed.returncode != 0:
        report.step("push", "error", detail=first_error_line(completed) or f"exit {completed.returncode}")
        return "tooling"
    result = parse_child_json(completed) or {}
    if transports == {"rest"}:
        report.step(
            "push",
            "pass",
            uploaded=result.get("uploaded"),
            calls=len(result.get("calls", [])) if isinstance(result.get("calls"), list) else None,
        )
        return "ok"

    completed, failure = run(
        ["node", str(MCP_EXEC), "exec", f"--piece={decl['piece']}", f"--repo={repo}",
         f"--plugins={piece_dir.parent}", "--output=json", "--quiet"],
        with_api_key=True,
    )
    if failure:
        report.step("push", "error", detail=redact(failure))
        return "tooling"
    executed = parse_child_json(completed) or {}
    receipt = executed.get("receipt") or str(repo / ".noru" / ".cache" / f"{decl['piece']}.receipt.json")
    if completed.returncode != 0 or executed.get("ok") is not True:
        report.step(
            "push",
            "error",
            detail=first_error_line(completed) or f"exit {completed.returncode}",
            written=executed.get("written"),
            failed=executed.get("failed"),
            not_run=executed.get("not_run"),
            receipt=receipt,
        )
        return "tooling"
    report.step("push", "pass", written=executed.get("written"), receipt=receipt)
    return "ok"


# --- wiring -------------------------------------------------------------------------------------
def decide_exit(report, mode, fail_on, tooling, gate_on_new=False):
    if tooling:
        report.payload["status"] = "error"
        return EXIT_TOOLING
    # `first_seen` is only ever set on a finding the base comparison could judge, so a drift or an
    # expiry finding is never excluded here however this flag is set.
    failing = [
        f for f in report.findings
        if f["kind"] in fail_on
        and not (gate_on_new and f.get("first_seen") == "pre_existing")
    ]
    if not failing:
        ran = any(step["status"] == "pass" for step in report.steps)
        # "Nothing ran" must never read as "everything passed". It exits 0 because a fork pull
        # request legitimately cannot fetch a queue from Noru, but it says so in plain words.
        report.payload["status"] = (
            "pass" if ran and not report.findings else "pass-with-warnings" if ran else "skipped"
        )
        return EXIT_OK
    if mode == "warn":
        report.payload["status"] = "warn"
        return EXIT_OK
    report.payload["status"] = "fail"
    codes = {KIND_EXIT[f["kind"]] for f in failing}
    return codes.pop() if len(codes) == 1 else EXIT_MIXED


def render_text(payload, quiet):
    lines = []
    if not quiet:
        lines.append(f"{payload['piece']} in {payload['repo']} ({payload['mode']} mode)")
        for step in payload["steps"]:
            marker = {
                "pass": "ok     ",
                "fail": "FAIL   ",
                "error": "ERROR  ",
                "blocked": "blocked",
                "skipped": "skipped",
            }.get(step["status"], step["status"])
            detail = step.get("detail")
            lines.append(f"  {marker} {step['step']}" + (f": {detail}" if detail else ""))
        lines.append("")

    fail_on = set(payload["policy"]["fail_on"])
    # In warn mode nothing fails, so nothing may be labelled as if it had.
    gating_label = "FAIL" if payload["mode"] == "gate" else "would-fail"
    # ...except a broken gate, which fails in either mode. Labelling the finding that stopped the
    # run as `warn` next to an ERROR line saying a check could not run is the kind of contradiction
    # that teaches people to stop reading the output.
    broken = payload["status"] == "error"
    for finding in payload["findings"]:
        failing = finding["kind"] in fail_on or (broken and KIND_EXIT.get(finding["kind"]) == EXIT_TOOLING)
        if quiet and not failing:
            continue
        label = ("BLOCKING" if broken and finding["kind"] not in fail_on else gating_label) if failing else "warn"
        where = finding.get("path") or finding.get("manifest") or ""
        lines.append(
            f"  {label} [{finding['kind']}] {where}: {finding['message']}"
            + see_finding_docs(finding["kind"])
        )
        explanation = finding.get("explanation")
        if explanation:
            rows = explanation.get("new_in_repository", [])
            if rows:
                lines.append(
                    "         present in the repository, named nowhere in the manifest:"
                )
            for row in rows:
                where_seen = f"  (first seen at {row['ref']})" if row.get("ref") else ""
                lines.append(f"           + {row['group']}: {row['identity']}{where_seen}")
            if explanation.get("note"):
                lines.append(f"         {explanation['note']}")

    lines.append("")
    status = payload["status"]
    if status == "fail":
        lines.append(f"FAILED ({payload['exit_code']}): see docs/ci-mode.md for what this exit code means.")
    elif status == "warn":
        lines.append("WARN-ONLY: findings above would fail this build in gate mode. Exiting 0.")
    elif status == "error":
        # --quiet suppresses the step table, so the reason has to travel with the verdict or a
        # tooling failure reads as one unexplained line in a log nobody can act on.
        culprit = next(
            (s for s in reversed(payload["steps"]) if s["status"] in ("error", "blocked")), None
        )
        detail = f" — {culprit['step']}: {culprit.get('detail', '')}" if culprit else ""
        lines.append(
            f"ERROR: a check could not run{detail}\n"
            "This is a tooling failure, not a compliance finding."
        )
    elif status == "skipped":
        blocked = [s for s in payload["steps"] if s["status"] in ("blocked", "skipped")]
        lines.append(
            f"SKIPPED: nothing was checked — {len(blocked)} step(s) had no input to work on. "
            "Exiting 0; pass --on-missing-prerequisite=fail if this job should insist."
        )
    elif not quiet:
        lines.append("OK: every requested check passed.")
    return "\n".join(line for line in lines if line is not None).rstrip()


def piece_version(piece_dir):
    try:
        manifest = json.loads((piece_dir / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    return str(manifest.get("version") or "unknown")


def write_gitlab_reports(report, opts, decl, piece_dir, repo, parsed_path, started):
    """The Code Quality and security report files, when asked for, recorded in the JSON report.

    Neither changes the exit code: they are another view of findings this run already decided on.
    The security report reads the manifest this run validated, and only when it did validate — the
    parsed copy in the cache could be left over from an earlier run.
    """
    written = {}
    if opts["code_quality"]:
        entries = gitlab_reports.code_quality(report.payload, repo, decl["artifact"])
        gitlab_reports.write_json(opts["code_quality"], entries)
        written["code_quality"] = {"path": opts["code_quality"], "entries": len(entries)}
    if opts["security_report"]:
        validated = any(s["step"] == "validate" and s["status"] == "pass" for s in report.steps)
        document = None
        if validated:
            try:
                document = json.loads(parsed_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                document = None
        sast, reason = gitlab_reports.security_report(
            decl, document, repo, piece_version(piece_dir), started,
            datetime.datetime.now(datetime.timezone.utc),
            include=opts["security_findings"] == "include",
        )
        gitlab_reports.write_json(opts["security_report"], sast)
        written["security_report"] = {
            "path": opts["security_report"],
            "schema": gitlab_reports.SAST_SCHEMA_VERSION,
            "vulnerabilities": len(sast["vulnerabilities"]),
            "note": reason,
        }
    if written:
        report.payload["gitlab"] = written


def parse_args(argv):
    opts = {
        "piece": None,
        "repo": pathlib.Path.cwd(),
        "plugins": ROOT / "plugins",
        "mode": "gate",
        "steps": list(DEFAULT_STEPS),
        "as_of": None,
        "warn_within_days": DEFAULT_WARN_WITHIN_DAYS,
        "max_age_days": 0,
        "fail_on": set(DEFAULT_FAIL_ON),
        "baseline": None,
        "base_ref": None,
        "gate_on_new": False,
        "state": None,
        "write_reviewed_plan": None,
        "reviewed_plan": None,
        "acc_manifest": None,
        "publish_workflow": None,
        "on_missing_prerequisite": "skip",
        "report": None,
        "code_quality": None,
        "security_report": None,
        "security_findings": "include",
        "json": False,
        "quiet": False,
        "help": False,
    }
    for arg in argv:
        if arg.startswith("--piece="):
            opts["piece"] = arg.split("=", 1)[1]
        elif arg.startswith("--repo="):
            opts["repo"] = pathlib.Path(arg.split("=", 1)[1])
        elif arg.startswith("--plugins="):
            opts["plugins"] = pathlib.Path(arg.split("=", 1)[1])
        elif arg.startswith("--mode="):
            value = arg.split("=", 1)[1]
            if value not in ("gate", "warn"):
                return None, f"--mode must be 'gate' or 'warn', got '{value}'"
            opts["mode"] = value
        elif arg.startswith("--steps="):
            value = arg.split("=", 1)[1]
            if value == "all":
                opts["steps"] = list(ALL_STEPS)
            else:
                requested = [s.strip() for s in value.split(",") if s.strip()]
                unknown = [s for s in requested if s not in ALL_STEPS]
                if unknown:
                    return None, f"unknown step(s) {unknown}; known steps are {list(ALL_STEPS)} or 'all'"
                opts["steps"] = [s for s in ALL_STEPS if s in requested]
        elif arg.startswith("--as-of="):
            opts["as_of"] = arg.split("=", 1)[1]
        elif arg.startswith("--warn-within-days=") or arg.startswith("--max-age-days="):
            flag, _, raw = arg.partition("=")
            try:
                number = int(raw)
            except ValueError:
                return None, f"{flag} needs a whole number of days, got '{raw}'"
            if number < 0:
                return None, f"{flag} cannot be negative"
            opts["warn_within_days" if flag == "--warn-within-days" else "max_age_days"] = number
        elif arg.startswith("--fail-on="):
            value = arg.split("=", 1)[1]
            if value.strip().lower() in ("none", ""):
                opts["fail_on"] = set()
            else:
                kinds = {k.strip() for k in value.split(",") if k.strip()}
                unknown = kinds - set(FINDING_KINDS)
                if unknown:
                    return None, (
                        f"unknown --fail-on kind(s) {sorted(unknown)}; known kinds are "
                        f"{list(FINDING_KINDS)} or 'none'"
                    )
                opts["fail_on"] = kinds
        elif arg.startswith("--baseline="):
            opts["baseline"] = arg.split("=", 1)[1]
        elif arg.startswith("--base-ref="):
            opts["base_ref"] = arg.split("=", 1)[1]
        elif arg == "--gate-on-new":
            opts["gate_on_new"] = True
        elif arg.startswith("--state="):
            opts["state"] = arg.split("=", 1)[1]
        elif arg.startswith("--write-reviewed-plan="):
            opts["write_reviewed_plan"] = arg.split("=", 1)[1]
        elif arg.startswith("--reviewed-plan="):
            opts["reviewed_plan"] = arg.split("=", 1)[1]
        elif arg.startswith("--acc-manifest="):
            opts["acc_manifest"] = arg.split("=", 1)[1]
        elif arg.startswith("--publish-workflow="):
            opts["publish_workflow"] = arg.split("=", 1)[1]
        elif arg.startswith("--report="):
            opts["report"] = arg.split("=", 1)[1]
        elif arg.startswith("--code-quality="):
            opts["code_quality"] = arg.split("=", 1)[1]
        elif arg.startswith("--security-report="):
            opts["security_report"] = arg.split("=", 1)[1]
        elif arg.startswith("--security-findings="):
            value = arg.split("=", 1)[1]
            if value not in ("include", "omit"):
                return None, f"--security-findings must be 'include' or 'omit', got '{value}'"
            opts["security_findings"] = value
        elif arg.startswith("--on-missing-prerequisite="):
            value = arg.split("=", 1)[1]
            if value not in ("skip", "fail"):
                return None, f"--on-missing-prerequisite must be 'skip' or 'fail', got '{value}'"
            opts["on_missing_prerequisite"] = value
        elif arg == "--output=json":
            opts["json"] = True
        elif arg == "--output=text":
            opts["json"] = False
        elif arg == "--quiet":
            opts["quiet"] = True
        elif arg in ("-h", "--help"):
            opts["help"] = True
        else:
            return None, f"unknown option '{arg}'"
    confirm = ("reviewed_plan", "acc_manifest", "publish_workflow")
    given = [k for k in confirm if opts[k]]
    if given and len(given) != len(confirm):
        return None, "--reviewed-plan, --acc-manifest and --publish-workflow go together"
    return opts, None


def main(argv):
    opts, error = parse_args(argv)
    if error:
        sys.stderr.write(f"error: {error}\n" + USAGE)
        return EXIT_USAGE
    if opts["help"]:
        sys.stdout.write(USAGE)
        return EXIT_OK
    if not opts["piece"]:
        sys.stderr.write("error: --piece=<name> is required\n" + USAGE)
        return EXIT_USAGE

    repo = opts["repo"]
    if not repo.is_dir():
        sys.stderr.write(f"error: --repo={repo} is not a directory\n")
        return EXIT_USAGE
    repo = repo.resolve()
    started = datetime.datetime.now(datetime.timezone.utc)

    piece_dir = opts["plugins"] / opts["piece"]
    declaration = piece_dir / "piece.json"
    if not declaration.is_file():
        available = sorted(p.parent.name for p in opts["plugins"].glob("*/piece.json"))
        sys.stderr.write(
            f"error: no piece declaration at {declaration}\n"
            f"  pieces available under {opts['plugins']}: {', '.join(available) or '(none)'}\n"
        )
        return EXIT_USAGE
    try:
        decl = json.loads(declaration.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"error: {declaration} is not readable JSON ({exc})\n")
        return EXIT_TOOLING

    as_of = parse_date(opts["as_of"]) if opts["as_of"] else None
    if opts["as_of"] and as_of is None:
        sys.stderr.write(f"error: --as-of='{opts['as_of']}' is not a date (expected YYYY-MM-DD)\n")
        return EXIT_USAGE
    if as_of is None:
        as_of = datetime.datetime.now(datetime.timezone.utc).date()

    policy = {
        "steps": opts["steps"],
        "fail_on": sorted(opts["fail_on"]),
        "as_of": as_of.isoformat(),
        "warn_within_days": opts["warn_within_days"],
        "max_age_days": opts["max_age_days"],
        "on_missing_prerequisite": opts["on_missing_prerequisite"],
        "base_ref": opts["base_ref"],
        "gate_on_new": opts["gate_on_new"],
    }
    report = Report(opts["piece"], repo, opts["mode"], policy)
    report.payload["artifact"] = decl["artifact"]

    manifest_path = repo / decl["artifact"]
    parsed_path = repo / ".noru" / ".cache" / f"{opts['piece']}.parsed.json"
    expiry_policy = {
        "as_of_date": as_of,
        "warn_within_days": opts["warn_within_days"],
        "max_age_days": opts["max_age_days"],
        "fail_on": opts["fail_on"],
    }

    delta = {
        "base_ref": opts["base_ref"],
        "gate_on_new": opts["gate_on_new"],
        "artifact": decl["artifact"],
    }

    # A token from the CI token exchange carries only what this run needs: the piece's read scopes,
    # and its write scopes only when this run may push.
    declared_scopes = decl.get("scopes") or {}
    _CREDENTIAL_SCOPES[:] = sorted(
        set(declared_scopes.get("read") or [])
        | (set(declared_scopes.get("write") or []) if "push" in opts["steps"] else set())
    )

    tooling = False
    diff_outcome = "skipped"
    for step in opts["steps"]:
        if step == "scan":
            outcome = step_scan(
                report, piece_dir, decl, repo, manifest_path, opts["on_missing_prerequisite"]
            )
        elif step == "validate":
            outcome = step_validate(
                report, piece_dir, decl, repo, manifest_path, parsed_path, opts["base_ref"]
            )
        elif step == "expiry":
            outcome = step_expiry(report, repo, parsed_path, expiry_policy)
        elif step == "policy":
            outcome = step_policy(report, repo, parsed_path, opts["baseline"], delta)
        elif step == "state":
            outcome = step_state(report, decl, repo, opts["plugins"].resolve(), opts["state"])
        elif step == "diff":
            outcome = step_diff(report, piece_dir, decl, repo, opts["state"], opts["write_reviewed_plan"])
            diff_outcome = outcome
        else:
            confirm = (
                {k: opts[k] for k in ("reviewed_plan", "acc_manifest", "publish_workflow")}
                if opts["reviewed_plan"]
                else None
            )
            outcome = step_push(report, piece_dir, decl, repo, diff_outcome, confirm)
        if outcome == "tooling":
            tooling = True
            break

    report.payload["counts"] = {
        kind: sum(1 for f in report.findings if f["kind"] == kind) for kind in FINDING_KINDS
    }
    code = decide_exit(report, opts["mode"], opts["fail_on"], tooling, opts["gate_on_new"])
    report.payload["exit_code"] = code

    try:
        write_gitlab_reports(report, opts, decl, piece_dir, repo, parsed_path, started)
    except OSError as exc:
        sys.stderr.write(f"error: could not write a GitLab report ({exc})\n")
        return EXIT_TOOLING

    if opts["report"]:
        # The machine-readable report is written whatever --output says, so a caller can print a
        # readable log and still drive a step summary or a downstream job from the same run.
        try:
            report_path = pathlib.Path(opts["report"])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report.payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            sys.stderr.write(f"error: could not write {opts['report']} ({exc})\n")
            return EXIT_TOOLING

    if opts["json"]:
        sys.stdout.write(
            json.dumps(report.payload, indent=None if opts["quiet"] else 2, sort_keys=True) + "\n"
        )
    else:
        rendered = render_text(report.payload, opts["quiet"])
        if rendered:
            sys.stdout.write(rendered + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
