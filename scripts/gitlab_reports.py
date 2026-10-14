#!/usr/bin/env python3
"""Turn a CI-mode report into the two report files a GitLab merge request reads.

Imported by scripts/ci_check.py (`--code-quality`, `--security-report`) and scripts/ci_review.py
(`--code-quality`). Nothing here reads the environment, opens a socket or knows which CI system it
runs on: the formats are GitLab's, the inputs are the report and the manifest this toolkit already
wrote, and the output is a file. A GitHub job can write them too; nothing would read them.

**Code Quality** carries every CI-mode finding — drift, an expired interpretation, a policy finding.
GitLab shows it in the merge request on every tier. Format, from
https://docs.gitlab.com/ci/testing/code_quality/ ("Code Quality report format"): one JSON array of
objects with `description`, `check_name`, `fingerprint`, `severity` (`info`, `minor`, `major`,
`critical` or `blocker`) and `location.path` plus `location.lines.begin`, the path relative to the
repository and without a leading `./`. Findings with the same fingerprint display as one, so the
fingerprint is the finding's identity and never its line: a manifest that gains a line above a
finding must not turn it into a new one.

**The security report** carries the security findings a piece's committed manifest records — the
reviewed record, not the collector's latest guess — for a piece whose `piece.json` declares the
`createSecurityFinding` write. GitLab shows it in the merge request only on Ultimate
(https://docs.gitlab.com/user/application_security/sast/, "New findings in merge request reports"),
and ingests it only when the job that produced it succeeded
(https://docs.gitlab.com/ci/yaml/artifacts_reports/). It targets the SAST report schema
SAST_SCHEMA_VERSION below, from
https://gitlab.com/gitlab-org/security-products/security-report-schemas.

A manifest that declares an input does not reach a forge alert store gets no findings in it. A
merge request security report *is* a forge alert store; filling one from such a manifest would make
the declaration false and file every result a second time wherever the store is imported.
"""
import datetime
import hashlib
import json
import pathlib
import re
import subprocess
import uuid

SAST_SCHEMA_VERSION = "15.2.0"
SAST_SCHEMA_URL = (
    "https://gitlab.com/gitlab-org/security-products/security-report-schemas/-/blob/"
    f"v{SAST_SCHEMA_VERSION}/dist/sast-report-format.json"
)
SECURITY_WRITE = "createSecurityFinding"

CODE_QUALITY_BLOCKING = "major"
CODE_QUALITY_ADVISORY = "minor"

# A finding that is still a live observation. One a person accepted, called a false positive or
# saw resolved is a decision already recorded in the manifest and in Noru; reporting it to GitLab
# as a vulnerability would present the decision as an open problem.
OPEN_STATUSES = ("open", "in_progress")
SAST_SEVERITY = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low", "info": "Info"}

REF_RE = re.compile(r"^([^:\s][^:]*):([0-9]+)$")
# The vulnerability id is a UUID derived from what the finding is, so the same finding has the same
# id in every run. The namespace is fixed for the same reason.
ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/noru-tech/noru-grc-engineering")


def project_prefix(repo):
    """`repo` relative to the top of its git checkout, so paths are relative to the project.

    `--repo` may be a subdirectory of a monorepo; GitLab matches a location against the project's
    own tree, so a path relative to the subdirectory would point at a file that is not there.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-prefix"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _join(prefix, rel):
    rel = rel[2:] if rel.startswith("./") else rel
    return f"{prefix}{rel}" if prefix else rel


def _manifest_line(manifest_text, needle):
    """Best effort: the first manifest line naming the finding's subject, else the first line.

    Only ever a display hint. It is not part of the fingerprint, so a wrong guess moves an
    annotation and never creates or hides a finding.
    """
    if isinstance(needle, str) and len(needle.strip()) >= 3:
        for number, line in enumerate(manifest_text.splitlines(), start=1):
            if needle.strip() in line:
                return number
    return 1


def _location(finding, repo, artifact, manifest_text):
    """Where a finding points: a resolvable `file:line` the finding carries, or the manifest."""
    rows = (finding.get("explanation") or {}).get("new_in_repository") or []
    for row in rows:
        match = REF_RE.match(row.get("ref") or "")
        if match and (repo / match.group(1)).is_file():
            return match.group(1), int(match.group(2))
    return artifact, _manifest_line(manifest_text, finding.get("subject"))


def code_quality(payload, repo, artifact=None):
    """One Code Quality entry per finding in a ci_check report, deterministic in order and identity."""
    repo = pathlib.Path(repo)
    piece = payload.get("piece") or "unknown"
    artifact = artifact or payload.get("artifact") or f".noru/{piece}.yml"
    try:
        manifest_text = (repo / artifact).read_text(encoding="utf-8")
    except OSError:
        manifest_text = ""
    fail_on = set((payload.get("policy") or {}).get("fail_on") or [])
    prefix = project_prefix(repo)
    entries = []
    seen = {}
    for finding in payload.get("findings") or []:
        kind = finding.get("kind") or "finding"
        path, line = _location(finding, repo, artifact, manifest_text)
        identity = [
            piece, kind, path, finding.get("path"), finding.get("field"),
            finding.get("subject"), finding.get("value"),
        ]
        # A validation error is identified by what it says about a path; two errors at one path are
        # two findings. Everything else is identified without its prose, which carries counts.
        if kind == "invalid":
            identity.append(finding.get("message"))
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        # Two findings with one identity would display as one. The occurrence number keeps both, and
        # stays stable because findings are reported in a deterministic order.
        occurrence = seen.get(fingerprint, 0)
        seen[fingerprint] = occurrence + 1
        if occurrence:
            fingerprint = hashlib.sha256(f"{fingerprint}#{occurrence}".encode("utf-8")).hexdigest()
        message = str(finding.get("message") or "")
        where = finding.get("path")
        entries.append(
            {
                "description": f"[{piece}] {kind}: {message}" + (f" ({where})" if where else ""),
                "check_name": f"noru/{piece}/{kind}",
                "fingerprint": fingerprint,
                "severity": CODE_QUALITY_BLOCKING if kind in fail_on else CODE_QUALITY_ADVISORY,
                "location": {"path": _join(prefix, path), "lines": {"begin": max(1, int(line))}},
            }
        )
    return entries


def code_quality_for_review(review_payload, repo):
    """The Code Quality entries of every piece a ci_review run checked, in one array."""
    entries = []
    for result in review_payload.get("results") or []:
        if isinstance(result, dict) and result.get("findings") is not None:
            entries.extend(code_quality(result, repo))
    return entries


def declares_security_findings(decl):
    return any(
        operation.get("name") == SECURITY_WRITE
        for operation in (decl.get("push") or {}).get("operations") or []
    )


def forge_alert_store_refusal(document):
    """Why this manifest's findings may not go into a forge alert store, or None."""
    inputs = document.get("inputs") if isinstance(document, dict) else None
    if not isinstance(inputs, list):
        return None
    refused = [
        str(entry.get("id") or entry.get("path") or "?")
        for entry in inputs
        if isinstance(entry, dict) and entry.get("reaches_forge_alert_store") is False
    ]
    if not refused:
        return None
    return (
        f"the manifest declares that input(s) {', '.join(sorted(refused))} reach no forge alert "
        "store; a merge request security report is one, so its findings are not reported there"
    )


def _timestamp(moment):
    # The schema's pattern is YYYY-MM-DDTHH:MM:SS with no zone; the value is UTC.
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _first_line(finding):
    file = finding.get("file")
    for ref in finding.get("refs") or []:
        match = REF_RE.match(str(ref))
        if match and (file is None or match.group(1) == file):
            return match.group(1), int(match.group(2))
    return file, None


def vulnerabilities(decl, document, repo):
    piece = decl["piece"]
    prefix = project_prefix(repo)
    identifier_type = f"noru_{piece.replace('-', '_')}_rule"
    rows = []
    for finding in document.get("findings") or []:
        if not isinstance(finding, dict) or finding.get("status") not in OPEN_STATUSES:
            continue
        rule = finding.get("check") or finding.get("rule")
        file, line = _first_line(finding)
        if not rule or not file:
            continue
        title = str(finding.get("title") or rule)
        location = {"file": _join(prefix, file)}
        # GitLab's integration guide asks for a start line; a finding about a whole file has none,
        # and is pinned to the first line rather than given an invented one.
        location["start_line"] = line or 1
        rows.append(
            {
                "id": str(uuid.uuid5(ID_NAMESPACE, f"{piece}:{finding.get('key') or rule}")),
                "name": title,
                "description": (
                    f"{title}. Recorded in {decl['artifact']} as `{finding.get('key')}`, status "
                    f"{finding.get('status')}; the interpretation there is the reviewed decision."
                ),
                "severity": SAST_SEVERITY.get(str(finding.get("severity")).lower(), "Unknown"),
                "identifiers": [
                    {"type": identifier_type, "name": f"{piece} {rule}", "value": str(rule)}
                ],
                "location": location,
            }
        )
    return rows


def security_report(decl, document, repo, version, started, finished, include=True):
    """(report, reason). `report` is always a valid SAST document; `reason` says why it is empty.

    Always a document, never a missing file: the GitLab template declares the report path whatever
    the piece, and what GitLab does with a declared report that is absent is not documented. An
    empty report from a scanner that found nothing to report is accurate.
    """
    reason = None
    rows = []
    status = "success"
    if not include:
        reason = "security findings were turned off for this job"
    elif not declares_security_findings(decl):
        reason = f"{decl['piece']} does not record security findings"
    elif document is None:
        reason = "the manifest did not validate in this run, so it has no findings to report"
        status = "failure"
    else:
        reason = forge_alert_store_refusal(document)
        if reason is None:
            rows = vulnerabilities(decl, document, pathlib.Path(repo))
    tool = {"name": "Noru"}
    report = {
        "version": SAST_SCHEMA_VERSION,
        "scan": {
            "analyzer": {"id": "noru-grc-engineering", "name": "noru-grc-engineering", "version": version, "vendor": tool},
            "scanner": {"id": f"noru-{decl['piece']}", "name": decl["piece"], "version": version, "vendor": tool},
            "type": "sast",
            "start_time": _timestamp(started),
            "end_time": _timestamp(finished),
            "status": status,
        },
        "vulnerabilities": rows,
    }
    return report, reason


def write_json(path, data):
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target
