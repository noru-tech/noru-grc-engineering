#!/usr/bin/env python3
"""End-to-end test of the property that matters most: a second push must be a no-op.

Standard library only, and no network. Every piece declared under plugins/ is driven through its
real scan -> validate -> diff -> push scripts against a fixture repository and a fixture snapshot
of a Noru organization. Pieces are enumerated from disk, and a piece with no test registered in
IDEMPOTENCY_TESTS is a FAILURE rather than a silent pass: a gate that reports "every piece" while
covering half of them launders an untested piece as tested, which is worse than no gate at all. The snapshot for the second round is derived from what the first round said it
would write, so this asserts the real thing: **if the writes we planned actually happened, does the
next run correctly decide there is nothing to do?**

It also asserts a property one machine cannot notice on its own: **the plan must not change because
a different YAML loader parsed the same manifest.** The validators use PyYAML where it is importable
and a bundled fallback otherwise, so a piece that renders manifest prose into a content marker could
have an identity that depends on the machine it ran on, and file a duplicate on the second push from
somewhere else. Both loaders are run here and their plans compared, which needs PyYAML to be
importable to have two of them; the comparison reports itself skipped rather than passing when it is
not, and the CI matrix runs a leg with it installed.

This cannot replace running against a live organization — see the Maturity section of
docs/verification.md. What it does replace is the class of idempotency bug that survives review
because nobody re-ran the command twice.

Usage:
    python3 scripts/test_idempotency.py [--output=json] [--quiet]
Exit codes: 0 = pass, 1 = a piece is not idempotent, 2 = usage / setup error.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
FIXTURE_REPO = ROOT / "tests" / "fixture-repo"


class Results:
    def __init__(self):
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append({"test": name, "ok": bool(ok), "detail": str(detail)[:400]})
        return ok

    def skip(self, name, reason):
        """Record a check that could not run here, said out loud rather than counted as a pass."""
        self.rows.append(
            {"test": name, "ok": True, "skipped": True, "detail": str(reason)[:400]}
        )

    @property
    def failures(self):
        return [r for r in self.rows if not r["ok"]]


def run(cmd, env=None):
    merged = dict(os.environ)
    merged.pop("NORU_API_KEY", None)
    if env:
        merged.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False, env=merged)


def prepare(tmp, piece_name):
    """A throwaway repo with the piece's valid fixture installed as the manifest."""
    repo = pathlib.Path(tmp) / f"{piece_name}-repo"
    shutil.copytree(FIXTURE_REPO, repo)
    (repo / ".noru" / ".cache").mkdir(parents=True, exist_ok=True)
    piece = PLUGINS / piece_name
    decl = json.loads((piece / "piece.json").read_text(encoding="utf-8"))
    fixture = piece / decl["validator"]["fixtures"]["valid"][0]
    manifest = repo / decl["artifact"]
    manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture, manifest)
    return repo, piece, decl, manifest


def validate_and_parse(piece, manifest, repo, results, label):
    validator = piece / "scripts" / "validate_manifest.py"
    parsed = repo / ".noru" / ".cache" / f"{piece.name}.parsed.json"
    result = run(
        ["python3", str(validator), str(manifest), f"--emit-parsed={parsed}", "--quiet"]
    )
    results.check(f"[{label}] manifest validates", result.returncode == 0, result.stdout)
    return parsed


def diff(piece, repo):
    return run(
        ["node", str(piece / "scripts" / "diff.mjs"), f"--repo={repo}", "--output=json", "--quiet"]
    )


def write_state(repo, payload):
    path = repo / ".noru" / ".cache" / "noru-state.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def state_after_ai_inventory(operations):
    """Build the org snapshot that would exist if every planned write had succeeded."""
    assets, vendors, evidence = [], [], []
    for i, op in enumerate(operations):
        if op["effect"] == "skip":
            continue
        args = op["arguments"]
        if op["operation"] == "createAsset":
            assets.append(
                {
                    "id": f"NORU-ASSET-{i}",
                    "source": args["source"],
                    "externalId": args["externalId"],
                    "name": args["name"],
                    "description": args.get("description"),
                    # Deliberately reversed key order: nothing guarantees a JSON object comes back
                    # with its keys in the order they were sent, so the diff must not care.
                    "metadata": dict(reversed(list(args["metadata"].items()))),
                }
            )
        elif op["operation"] == "createVendor":
            vendors.append({"id": f"NORU-VENDOR-{i}", "name": args["name"]})
        elif op["operation"] == "createEvidence":
            evidence.append(
                {
                    "id": f"NORU-EVD-{i}",
                    "title": args["title"],
                    "description": args["description"],
                }
            )
    return assets, vendors, evidence


def test_ai_inventory(results, tmp):
    repo, piece, decl, manifest = prepare(tmp, "ai-inventory")
    label = "ai-inventory"

    validate_and_parse(piece, manifest, repo, results, label)
    write_state(
        repo,
        {
            "fetched_at": "2026-08-27T09:14:00Z",
            "assets": [],
            "vendors": [],
            "evidence": [],
            "ai_framework_ids": ["zz_framework"],
            "ai_controls": [{"id": "zz-01", "controlId": "ZZ-01", "name": "Example control"}],
        },
    )

    first = diff(piece, repo)
    if not results.check(f"[{label}] first diff succeeds", first.returncode == 0, first.stderr):
        return
    plan = json.loads(first.stdout)
    results.check(
        f"[{label}] first diff plans real writes",
        plan["summary"]["create"] > 0,
        json.dumps(plan["summary"]),
    )

    push = piece / "scripts" / "push.mjs"
    refused = run(["node", str(push), f"--repo={repo}"])
    results.check(
        f"[{label}] push without --confirm is refused with exit 2",
        refused.returncode == 2,
        refused.stderr,
    )

    first_push = run(["node", str(push), f"--repo={repo}", "--confirm", "--output=json", "--quiet"])
    if not results.check(
        f"[{label}] first push emits the confirmed calls", first_push.returncode == 0,
        first_push.stderr,
    ):
        return
    calls = json.loads(first_push.stdout)["calls"]
    results.check(f"[{label}] first push has calls to make", len(calls) > 0, len(calls))

    # Now pretend every planned write landed, and ask again.
    assets, vendors, evidence = state_after_ai_inventory(plan["operations"])
    write_state(
        repo,
        {
            "fetched_at": "2026-08-27T10:00:00Z",
            "assets": assets,
            "vendors": vendors,
            "evidence": evidence,
            "ai_framework_ids": ["zz_framework"],
            "ai_controls": [{"id": "zz-01", "controlId": "ZZ-01", "name": "Example control"}],
        },
    )

    second = diff(piece, repo)
    if not results.check(f"[{label}] second diff succeeds", second.returncode == 0, second.stderr):
        return
    plan2 = json.loads(second.stdout)
    non_skip = [op for op in plan2["operations"] if op["effect"] != "skip"]
    results.check(
        f"[{label}] SECOND DIFF IS A NO-OP (every operation skipped)",
        len(non_skip) == 0,
        "; ".join(f"{op['operation']} {op['effect']} {op['subject']}" for op in non_skip),
    )

    second_push = run(["node", str(push), f"--repo={repo}", "--confirm", "--output=json", "--quiet"])
    results.check(
        f"[{label}] SECOND PUSH MAKES NO CALLS",
        second_push.returncode == 0 and len(json.loads(second_push.stdout)["calls"]) == 0,
        second_push.stdout[:200],
    )

    # Editing the manifest must invalidate the reviewed plan.
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n# edited after the plan\n",
                        encoding="utf-8")
    stale = run(["node", str(push), f"--repo={repo}", "--confirm"])
    results.check(
        f"[{label}] editing the manifest invalidates the plan",
        stale.returncode == 1 and "manifest changed" in stale.stderr,
        stale.stderr,
    )


def test_evidence_push(results, tmp):
    repo, piece, decl, manifest = prepare(tmp, "evidence-push")
    label = "evidence-push"

    # The fixture manifest points at artifacts that must exist for the plan to propose an upload.
    for rel in ("q2-access-review.pdf", "2026-pentest-report.pdf"):
        target = repo / ".noru" / "artifacts" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.4 fixture\n")

    validate_and_parse(piece, manifest, repo, results, label)
    write_state(repo, {"fetched_at": "2026-08-27T09:14:00Z", "evidence": []})

    first = diff(piece, repo)
    if not results.check(f"[{label}] first diff succeeds", first.returncode == 0, first.stderr):
        return
    plan = json.loads(first.stdout)
    results.check(
        f"[{label}] first diff plans real uploads",
        plan["summary"]["create"] > 0,
        json.dumps(plan["summary"]),
    )

    push = piece / "scripts" / "push.mjs"
    refused = run(["node", str(push), f"--repo={repo}"])
    results.check(
        f"[{label}] push without --confirm is refused with exit 2",
        refused.returncode == 2,
        refused.stderr,
    )

    # --dry-run proves the request shape without a credential and without touching the network.
    dry = run(
        ["node", str(push), f"--repo={repo}", "--confirm", "--dry-run", "--output=json", "--quiet"]
    )
    if results.check(f"[{label}] dry run succeeds with no credential", dry.returncode == 0, dry.stderr):
        payload = json.loads(dry.stdout)
        results.check(
            f"[{label}] dry run sends controlMappings, never the legacy controlIds",
            all("control_mappings" in u for u in payload["would_upload"]),
            json.dumps(payload["would_upload"])[:200],
        )
        results.check(
            f"[{label}] dry run makes no request and reveals no credential",
            "NORU_API_KEY" not in dry.stdout and payload.get("dry_run") is True,
            dry.stdout[:200],
        )

    # A real push with no credential must stop, not guess.
    no_key = run(["node", str(push), f"--repo={repo}", "--confirm"])
    results.check(
        f"[{label}] a real push with no NORU_API_KEY exits 1 without making a request",
        no_key.returncode == 1 and "NORU_API_KEY is not set" in no_key.stderr,
        no_key.stderr[:200],
    )

    # Now pretend the uploads landed: the evidence descriptions carry the content markers.
    evidence = [
        {
            "id": f"NORU-EVD-{i}",
            "title": op["arguments"]["form"]["title"],
            "description": op["arguments"]["form"]["description"],
        }
        for i, op in enumerate(plan["operations"])
        if op["effect"] == "create"
    ]
    write_state(repo, {"fetched_at": "2026-08-27T10:00:00Z", "evidence": evidence})

    second = diff(piece, repo)
    if not results.check(f"[{label}] second diff succeeds", second.returncode == 0, second.stderr):
        return
    plan2 = json.loads(second.stdout)
    non_skip = [op for op in plan2["operations"] if op["effect"] != "skip"]
    results.check(
        f"[{label}] SECOND DIFF IS A NO-OP (every operation skipped)",
        len(non_skip) == 0,
        "; ".join(f"{op['operation']} {op['effect']} {op['subject']}" for op in non_skip),
    )

    second_push = run(
        ["node", str(push), f"--repo={repo}", "--confirm", "--output=json", "--quiet"]
    )
    results.check(
        f"[{label}] SECOND PUSH UPLOADS NOTHING",
        second_push.returncode == 0 and json.loads(second_push.stdout)["uploaded"] == 0,
        second_push.stdout[:200],
    )


def state_after_governance_records(operations):
    """Build the org snapshot that would exist if every planned write had succeeded."""
    evidence = []
    created_at_index = {}
    for i, op in enumerate(operations):
        if op["effect"] == "skip":
            continue
        args = op["arguments"]
        if op["operation"] == "createEvidence":
            record = {
                "id": f"NORU-EVD-{i}",
                "title": args["title"],
                "description": args["description"],
                # Deliberately reversed: nothing guarantees the API echoes control links back in the
                # order they were sent, so the diff must not care.
                "linkedControls": [
                    {"id": m["controlId"]} for m in reversed(args.get("controlMappings", []))
                ],
            }
            evidence.append(record)
            created_at_index[i] = record
        elif op["operation"] == "linkEvidenceToControl":
            target = next((e for e in evidence if e["id"] == args["evidenceId"]), None)
            if target is not None:
                target.setdefault("linkedControls", []).append({"id": args["controlId"]})
    return evidence


def test_governance_records(results, tmp):
    repo, piece, decl, manifest = prepare(tmp, "governance-records")
    label = "governance-records"

    validate_and_parse(piece, manifest, repo, results, label)
    write_state(repo, {"fetched_at": "2026-08-27T09:14:00Z", "evidence": []})

    first = diff(piece, repo)
    if not results.check(f"[{label}] first diff succeeds", first.returncode == 0, first.stderr):
        return
    plan = json.loads(first.stdout)
    results.check(
        f"[{label}] first diff plans real writes",
        plan["summary"]["create"] > 0,
        json.dumps(plan["summary"]),
    )

    push = piece / "scripts" / "push.mjs"
    refused = run(["node", str(push), f"--repo={repo}"])
    results.check(
        f"[{label}] push without --confirm is refused with exit 2",
        refused.returncode == 2,
        refused.stderr,
    )

    first_push = run(["node", str(push), f"--repo={repo}", "--confirm", "--output=json", "--quiet"])
    if not results.check(
        f"[{label}] first push emits the confirmed calls", first_push.returncode == 0,
        first_push.stderr,
    ):
        return
    calls = json.loads(first_push.stdout)["calls"]
    results.check(f"[{label}] first push has calls to make", len(calls) > 0, len(calls))

    # Now pretend every planned write landed, and ask again.
    evidence = state_after_governance_records(plan["operations"])
    write_state(repo, {"fetched_at": "2026-08-27T10:00:00Z", "evidence": evidence})

    second = diff(piece, repo)
    if not results.check(f"[{label}] second diff succeeds", second.returncode == 0, second.stderr):
        return
    plan2 = json.loads(second.stdout)
    non_skip = [op for op in plan2["operations"] if op["effect"] != "skip"]
    results.check(
        f"[{label}] SECOND DIFF IS A NO-OP (every operation skipped)",
        len(non_skip) == 0,
        "; ".join(f"{op['operation']} {op['effect']} {op['subject']}" for op in non_skip),
    )

    second_push = run(["node", str(push), f"--repo={repo}", "--confirm", "--output=json", "--quiet"])
    results.check(
        f"[{label}] SECOND PUSH MAKES NO CALLS",
        second_push.returncode == 0 and len(json.loads(second_push.stdout)["calls"]) == 0,
        second_push.stdout[:200],
    )

    # The link operation only fires when the record exists but a mapping does not. Without this the
    # second half of the push would be dead code that no test ever reaches.
    unlinked = [dict(record, linkedControls=[]) for record in evidence]
    write_state(repo, {"fetched_at": "2026-08-27T11:00:00Z", "evidence": unlinked})
    relink = diff(piece, repo)
    plan3 = json.loads(relink.stdout) if relink.returncode == 0 else {"operations": []}
    links = [
        op for op in plan3["operations"]
        if op["operation"] == "linkEvidenceToControl" and op["effect"] == "create"
    ]
    results.check(
        f"[{label}] a mapping added after the record was filed plans a link, not a second record",
        len(links) > 0
        and all(
            op["effect"] == "skip"
            for op in plan3["operations"]
            if op["operation"] == "createEvidence"
        ),
        json.dumps([f"{op['operation']} {op['effect']}" for op in plan3["operations"]]),
    )
    write_state(repo, {"fetched_at": "2026-08-27T10:00:00Z", "evidence": evidence})

    # Editing the manifest must invalidate the reviewed plan.
    diff(piece, repo)
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n# edited after the plan\n",
                        encoding="utf-8")
    stale = run(["node", str(push), f"--repo={repo}", "--confirm"])
    results.check(
        f"[{label}] editing the manifest invalidates the plan",
        stale.returncode == 1 and "manifest changed" in stale.stderr,
        stale.stderr,
    )


def state_after_review_signoff(operations):
    """As above, and it also has to thread the evidence id the update depends on."""
    evidence = []
    created_at_index = {}
    for i, op in enumerate(operations):
        if op["effect"] == "skip":
            continue
        args = op["arguments"]
        if op["operation"] == "createEvidence":
            record = {
                "id": f"NORU-EVD-{i}",
                "title": args["title"],
                "description": args["description"],
                "expiresAt": None,
            }
            evidence.append(record)
            created_at_index[i] = record
        elif op["operation"] == "updateEvidence":
            target = None
            if args.get("evidenceId"):
                target = next((e for e in evidence if e["id"] == args["evidenceId"]), None)
            elif op.get("depends_on"):
                target = created_at_index.get(op["depends_on"]["operation_index"])
            if target is not None:
                target["expiresAt"] = args["expiresAt"]
    return evidence


def test_review_signoff(results, tmp):
    repo, piece, decl, manifest = prepare(tmp, "review-signoff")
    label = "review-signoff"

    validate_and_parse(piece, manifest, repo, results, label)
    write_state(repo, {"fetched_at": "2026-08-27T09:14:00Z", "evidence": []})

    first = diff(piece, repo)
    if not results.check(f"[{label}] first diff succeeds", first.returncode == 0, first.stderr):
        return
    plan = json.loads(first.stdout)
    results.check(
        f"[{label}] first diff plans real writes",
        plan["summary"]["create"] > 0 and plan["summary"]["update"] > 0,
        json.dumps(plan["summary"]),
    )
    results.check(
        f"[{label}] the expiry reaches the record, not only its text",
        all(
            op["arguments"]["expiresAt"].startswith("20")
            for op in plan["operations"]
            if op["operation"] == "updateEvidence"
        ),
        json.dumps(
            [op["arguments"] for op in plan["operations"] if op["operation"] == "updateEvidence"]
        )[:200],
    )

    push = piece / "scripts" / "push.mjs"
    refused = run(["node", str(push), f"--repo={repo}"])
    results.check(
        f"[{label}] push without --confirm is refused with exit 2",
        refused.returncode == 2,
        refused.stderr,
    )

    first_push = run(["node", str(push), f"--repo={repo}", "--confirm", "--output=json", "--quiet"])
    if not results.check(
        f"[{label}] first push emits the confirmed calls", first_push.returncode == 0,
        first_push.stderr,
    ):
        return
    calls = json.loads(first_push.stdout)["calls"]
    results.check(f"[{label}] first push has calls to make", len(calls) > 0, len(calls))

    # The dependent call has to point at a call that is actually in this push, by its position
    # after skipped operations were dropped — an off-by-one here would send a null evidence id.
    dependent = [c for c in calls if "depends_on" in c]
    results.check(
        f"[{label}] every dependent call names an earlier call in this push",
        len(dependent) > 0
        and all(
            1 <= c["depends_on"]["order"] < c["order"]
            and calls[c["depends_on"]["order"] - 1]["tool"] == "createEvidence"
            for c in dependent
        ),
        json.dumps([{"order": c["order"], "depends_on": c.get("depends_on")} for c in calls]),
    )
    results.check(
        f"[{label}] no call carries an unresolved dependency",
        not any("error" in c for c in calls),
        json.dumps([c.get("error") for c in calls if "error" in c]),
    )

    # Now pretend every planned write landed, and ask again.
    evidence = state_after_review_signoff(plan["operations"])
    results.check(
        f"[{label}] the simulated writes left every record with an expiry",
        len(evidence) > 0 and all(e["expiresAt"] for e in evidence),
        json.dumps(evidence)[:300],
    )
    write_state(repo, {"fetched_at": "2026-08-27T10:00:00Z", "evidence": evidence})

    second = diff(piece, repo)
    if not results.check(f"[{label}] second diff succeeds", second.returncode == 0, second.stderr):
        return
    plan2 = json.loads(second.stdout)
    non_skip = [op for op in plan2["operations"] if op["effect"] != "skip"]
    results.check(
        f"[{label}] SECOND DIFF IS A NO-OP (every operation skipped)",
        len(non_skip) == 0,
        "; ".join(f"{op['operation']} {op['effect']} {op['subject']}" for op in non_skip),
    )

    second_push = run(["node", str(push), f"--repo={repo}", "--confirm", "--output=json", "--quiet"])
    results.check(
        f"[{label}] SECOND PUSH MAKES NO CALLS",
        second_push.returncode == 0 and len(json.loads(second_push.stdout)["calls"]) == 0,
        second_push.stdout[:200],
    )

    # An expiry that drifted in Noru is an update, not a duplicate record.
    drifted = [dict(record, expiresAt="2030-01-01T23:59:59Z") for record in evidence]
    write_state(repo, {"fetched_at": "2026-08-27T11:00:00Z", "evidence": drifted})
    third = diff(piece, repo)
    plan3 = json.loads(third.stdout) if third.returncode == 0 else {"operations": []}
    results.check(
        f"[{label}] a changed expiry plans an update, not a second sign-off",
        any(
            op["operation"] == "updateEvidence" and op["effect"] == "update"
            for op in plan3["operations"]
        )
        and all(
            op["effect"] == "skip"
            for op in plan3["operations"]
            if op["operation"] == "createEvidence"
        ),
        json.dumps([f"{op['operation']} {op['effect']}" for op in plan3["operations"]]),
    )
    write_state(repo, {"fetched_at": "2026-08-27T10:00:00Z", "evidence": evidence})

    # Editing the manifest must invalidate the reviewed plan.
    diff(piece, repo)
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n# edited after the plan\n",
                        encoding="utf-8")
    stale = run(["node", str(push), f"--repo={repo}", "--confirm"])
    results.check(
        f"[{label}] editing the manifest invalidates the plan",
        stale.returncode == 1 and "manifest changed" in stale.stderr,
        stale.stderr,
    )


def state_after_iac_scan(operations, existing=()):
    """Build the org snapshot that would exist if every planned upsert had succeeded.

    Modelled on the documented behaviour and nothing more: the key is (source, externalId), a write
    on an existing key updates in place, and a field the call does not send is left alone.
    """
    findings = {row["externalId"]: dict(row) for row in existing}
    for i, op in enumerate(operations):
        if op["effect"] == "skip":
            continue
        args = op["arguments"]
        record = findings.get(args["externalId"], {"id": f"NORU-FND-{i}"})
        for field in (
            "source", "externalId", "title", "severity", "status", "category", "checkName",
            "description", "observedAt", "assetId", "riskId", "ownerEmail",
        ):
            if field in args:
                record[field] = args[field]
        findings[args["externalId"]] = record
    # Deliberately reversed: nothing guarantees a list endpoint returns records in the order they
    # were written, so the diff must not care.
    return list(reversed(list(findings.values())))


def test_iac_scan(results, tmp):
    repo, piece, decl, manifest = prepare(tmp, "iac-scan")
    label = "iac-scan"

    validate_and_parse(piece, manifest, repo, results, label)

    # Seed Noru with a finding this piece filed last time whose rule no longer fires. Closing it is
    # half of what re-running is for, and it has to be exercised on the first diff, not assumed.
    stale = {
        "id": "NORU-FND-STALE",
        "source": "iac-scan",
        "externalId": "example-org/example-app:kubernetes-container-runs-privileged.f0e1d2c3b4a5",
        "title": "Container requests privileged mode",
        "checkName": "kubernetes-container-runs-privileged",
        "description": "filed by an earlier scan",
        "severity": "critical",
        "status": "open",
        "category": "configuration",
        "observedAt": "2026-06-01T00:00:00Z",
        "assetId": None,
        "riskId": None,
        "ownerEmail": None,
    }
    # A finding under ANOTHER repository's slug, pushed to the same source. Closing it would be a
    # bug with real consequences, so the fixture contains one on purpose.
    other_repo = dict(
        stale,
        id="NORU-FND-OTHER",
        externalId="example-org/other-app:kubernetes-container-runs-privileged.aabbccddeeff",
    )
    write_state(
        repo,
        {"fetched_at": "2026-08-27T09:14:00Z", "security_findings": [stale, other_repo]},
    )

    first = diff(piece, repo)
    if not results.check(f"[{label}] first diff succeeds", first.returncode == 0, first.stderr):
        return
    plan = json.loads(first.stdout)
    results.check(
        f"[{label}] first diff plans real writes",
        plan["summary"]["create"] > 0,
        json.dumps(plan["summary"]),
    )
    results.check(
        f"[{label}] every operation declares a server-side upsert key, not a client probe",
        all(op["idempotency"]["kind"] == "server_upsert" for op in plan["operations"]),
        json.dumps([op["idempotency"] for op in plan["operations"]]),
    )

    closes = [
        op for op in plan["operations"]
        if op["effect"] != "skip" and op["arguments"].get("status") == "resolved"
    ]
    results.check(
        f"[{label}] a finding whose rule no longer fires is planned for closure",
        len(closes) == 1 and closes[0]["arguments"]["externalId"] == stale["externalId"],
        json.dumps([op["arguments"].get("externalId") for op in closes]),
    )
    results.check(
        f"[{label}] ANOTHER repository's finding under the same source is left alone",
        not any(
            op["arguments"].get("externalId") == other_repo["externalId"]
            for op in plan["operations"]
        ),
        json.dumps([op["arguments"].get("externalId") for op in plan["operations"]]),
    )
    results.check(
        f"[{label}] every write carries slug, commit and branch provenance",
        all(
            {"slug", "commit_sha", "branch"} <= set(op["arguments"].get("raw") or {})
            for op in plan["operations"]
            if op["effect"] != "skip"
        ),
        json.dumps([op["arguments"].get("raw") for op in plan["operations"]])[:300],
    )

    push = piece / "scripts" / "push.mjs"
    refused = run(["node", str(push), f"--repo={repo}"])
    results.check(
        f"[{label}] push without --confirm is refused with exit 2",
        refused.returncode == 2,
        refused.stderr,
    )

    first_push = run(["node", str(push), f"--repo={repo}", "--confirm", "--output=json", "--quiet"])
    if not results.check(
        f"[{label}] first push emits the confirmed calls", first_push.returncode == 0,
        first_push.stderr,
    ):
        return
    calls = json.loads(first_push.stdout)["calls"]
    results.check(f"[{label}] first push has calls to make", len(calls) > 0, len(calls))
    results.check(
        f"[{label}] no call carries a matched line — a finding is a citation, never a copy",
        "example-placeholder-not-a-credential" not in first_push.stdout,
        first_push.stdout[:200],
    )

    # Now pretend every planned write landed, and ask again.
    findings = state_after_iac_scan(plan["operations"], [stale, other_repo])
    write_state(repo, {"fetched_at": "2026-08-27T10:00:00Z", "security_findings": findings})

    second = diff(piece, repo)
    if not results.check(f"[{label}] second diff succeeds", second.returncode == 0, second.stderr):
        return
    plan2 = json.loads(second.stdout)
    non_skip = [op for op in plan2["operations"] if op["effect"] != "skip"]
    results.check(
        f"[{label}] SECOND DIFF IS A NO-OP (every operation skipped)",
        len(non_skip) == 0,
        "; ".join(f"{op['operation']} {op['effect']} {op['subject']}" for op in non_skip),
    )

    second_push = run(["node", str(push), f"--repo={repo}", "--confirm", "--output=json", "--quiet"])
    results.check(
        f"[{label}] SECOND PUSH MAKES NO CALLS",
        second_push.returncode == 0 and len(json.loads(second_push.stdout)["calls"]) == 0,
        second_push.stdout[:200],
    )

    # A severity changed by hand in Noru is an update on the same key, never a second record.
    drifted = [
        dict(row, severity="low") if row["externalId"].endswith("a1b2c3d4e5f6") else row
        for row in findings
    ]
    write_state(repo, {"fetched_at": "2026-08-27T11:00:00Z", "security_findings": drifted})
    third = diff(piece, repo)
    plan3 = json.loads(third.stdout) if third.returncode == 0 else {"operations": []}
    updates = [op for op in plan3["operations"] if op["effect"] == "update"]
    results.check(
        f"[{label}] a field changed in Noru plans an update on the same key, not a new finding",
        len(updates) == 1
        and "severity" in updates[0]["reason"]
        and not any(op["effect"] == "create" for op in plan3["operations"]),
        json.dumps([f"{op['effect']} {op['reason']}" for op in plan3["operations"]])[:300],
    )
    write_state(repo, {"fetched_at": "2026-08-27T10:00:00Z", "security_findings": findings})

    # Editing the manifest must invalidate the reviewed plan.
    diff(piece, repo)
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n# edited after the plan\n",
                        encoding="utf-8")
    stale_plan = run(["node", str(push), f"--repo={repo}", "--confirm"])
    results.check(
        f"[{label}] editing the manifest invalidates the plan",
        stale_plan.returncode == 1 and "manifest changed" in stale_plan.stderr,
        stale_plan.stderr,
    )


def state_after_audit_pack(operations):
    """Build the org snapshot that would exist if every planned write had succeeded."""
    evidence = []
    for i, op in enumerate(operations):
        if op["effect"] == "skip":
            continue
        args = op["arguments"]
        if op["operation"] == "createEvidence":
            evidence.append(
                {
                    "id": f"NORU-EVD-{i}",
                    "title": args["title"],
                    "description": args["description"],
                    # Deliberately reversed: nothing guarantees the API echoes control links back in
                    # the order they were sent, so the diff must not care.
                    "linkedControls": [
                        {"id": m["controlId"]} for m in reversed(args.get("controlMappings", []))
                    ],
                }
            )
        elif op["operation"] == "linkEvidenceToControl":
            target = next((e for e in evidence if e["id"] == args["evidenceId"]), None)
            if target is not None:
                target.setdefault("linkedControls", []).append({"id": args["controlId"]})
    return evidence


def test_audit_pack(results, tmp):
    repo, piece, decl, manifest = prepare(tmp, "audit-pack")
    label = "audit-pack"

    validate_and_parse(piece, manifest, repo, results, label)
    write_state(repo, {"fetched_at": "2026-08-27T09:14:00Z", "evidence": []})

    first = diff(piece, repo)
    if not results.check(f"[{label}] first diff succeeds", first.returncode == 0, first.stderr):
        return
    plan = json.loads(first.stdout)
    results.check(
        f"[{label}] first diff plans real writes",
        plan["summary"]["create"] > 0,
        json.dumps(plan["summary"]),
    )

    creates = [op for op in plan["operations"] if op["operation"] == "createEvidence"]
    results.check(
        f"[{label}] one workpaper becomes one record mapped to exactly one control",
        len(creates) > 0
        and all(len(op["arguments"]["controlMappings"]) == 1 for op in creates),
        json.dumps([op["arguments"]["controlMappings"] for op in creates]),
    )
    # The pack is a local deliverable. If the index ever ends up inside a call, the piece has quietly
    # become a second register, which is the non-goal this whole design turns on.
    results.check(
        f"[{label}] the rendered pack is NOT pushed — only the tested conclusions are",
        "## Controls in scope" not in first.stdout and "Assembled by:" not in first.stdout,
        first.stdout[:200],
    )
    results.check(
        f"[{label}] every conclusion carries the window it is about and when it stops standing",
        all(
            " to 2026-06-30" in op["arguments"]["content"]
            and "Stands until:" in op["arguments"]["content"]
            for op in creates
        ),
        json.dumps([op["arguments"]["content"][:120] for op in creates]),
    )

    push = piece / "scripts" / "push.mjs"
    refused = run(["node", str(push), f"--repo={repo}"])
    results.check(
        f"[{label}] push without --confirm is refused with exit 2",
        refused.returncode == 2,
        refused.stderr,
    )

    first_push = run(["node", str(push), f"--repo={repo}", "--confirm", "--output=json", "--quiet"])
    if not results.check(
        f"[{label}] first push emits the confirmed calls", first_push.returncode == 0,
        first_push.stderr,
    ):
        return
    calls = json.loads(first_push.stdout)["calls"]
    results.check(f"[{label}] first push has calls to make", len(calls) > 0, len(calls))

    # Now pretend every planned write landed, and ask again.
    evidence = state_after_audit_pack(plan["operations"])
    write_state(repo, {"fetched_at": "2026-08-27T10:00:00Z", "evidence": evidence})

    second = diff(piece, repo)
    if not results.check(f"[{label}] second diff succeeds", second.returncode == 0, second.stderr):
        return
    plan2 = json.loads(second.stdout)
    non_skip = [op for op in plan2["operations"] if op["effect"] != "skip"]
    results.check(
        f"[{label}] SECOND DIFF IS A NO-OP (every operation skipped)",
        len(non_skip) == 0,
        "; ".join(f"{op['operation']} {op['effect']} {op['subject']}" for op in non_skip),
    )

    second_push = run(["node", str(push), f"--repo={repo}", "--confirm", "--output=json", "--quiet"])
    results.check(
        f"[{label}] SECOND PUSH MAKES NO CALLS",
        second_push.returncode == 0 and len(json.loads(second_push.stdout)["calls"]) == 0,
        second_push.stdout[:200],
    )

    # A record filed with its control link missing plans a link, not a second workpaper. Without this
    # the second half of the push would be dead code no test ever reaches.
    unlinked = [dict(record, linkedControls=[]) for record in evidence]
    write_state(repo, {"fetched_at": "2026-08-27T11:00:00Z", "evidence": unlinked})
    relink = diff(piece, repo)
    plan3 = json.loads(relink.stdout) if relink.returncode == 0 else {"operations": []}
    links = [
        op for op in plan3["operations"]
        if op["operation"] == "linkEvidenceToControl" and op["effect"] == "create"
    ]
    results.check(
        f"[{label}] a missing control link plans a link, not a second workpaper",
        len(links) > 0
        and all(
            op["effect"] == "skip"
            for op in plan3["operations"]
            if op["operation"] == "createEvidence"
        ),
        json.dumps([f"{op['operation']} {op['effect']}" for op in plan3["operations"]]),
    )
    write_state(repo, {"fetched_at": "2026-08-27T10:00:00Z", "evidence": evidence})

    # Editing the manifest must invalidate the reviewed plan.
    diff(piece, repo)
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n# edited after the plan\n",
                        encoding="utf-8")
    stale = run(["node", str(push), f"--repo={repo}", "--confirm"])
    results.check(
        f"[{label}] editing the manifest invalidates the plan",
        stale.returncode == 1 and "manifest changed" in stale.stderr,
        stale.stderr,
    )


def _fallback_only_env(tmp):
    """An environment in which `import yaml` fails, so load_yaml takes the bundled fallback.

    Forcing the loader inside this interpreter beats asking for two of them: the comparison then
    runs wherever the suite runs, and needs nothing installed that is not already here. The stub is
    a module named `yaml` that raises on import, which is the exact condition load_yaml branches on.
    """
    stub = pathlib.Path(tmp) / "no-pyyaml"
    stub.mkdir(exist_ok=True)
    (stub / "yaml.py").write_text(
        'raise ImportError("PyYAML hidden so the bundled fallback loader runs")\n', encoding="utf-8"
    )
    existing = os.environ.get("PYTHONPATH")
    return {"PYTHONPATH": f"{stub}{os.pathsep}{existing}" if existing else str(stub)}


def _differences(left, right, path="", found=None):
    """The JSON paths at which two plans disagree, for a failure message worth reading.

    A plan can move in its arguments rather than in its marker — a description, a title, an asset
    payload that is digested to decide whether anything changed — so reporting only the marker would
    have printed two identical-looking values next to a failing assertion.
    """
    found = [] if found is None else found
    if len(found) >= 4:
        return found
    if type(left) is not type(right):
        found.append((path, repr(left)[:80], repr(right)[:80]))
    elif isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            _differences(left.get(key), right.get(key), f"{path}.{key}", found)
    elif isinstance(left, list):
        if len(left) != len(right):
            found.append((path, f"{len(left)} operation(s)", f"{len(right)} operation(s)"))
        for i, (a, b) in enumerate(zip(left, right)):
            _differences(a, b, f"{path}[{i}]", found)
    elif left != right:
        found.append((path, repr(left)[:80], repr(right)[:80]))
    return found


def _org_after(operations):
    """An org snapshot in which every planned write has landed, derived from the plan itself.

    Deliberately generic — keyed on the operation names in the plan rather than on a list of pieces —
    because this is not trying to be a faithful snapshot the way each piece's own idempotency test
    is. It only has to be rich enough to drive the diff down its "this already exists" branches, and
    identical for both of the runs being compared. Without it the loader comparison only ever sees an
    empty organization, and every existence check, skip reason and marker match — which is the code
    the fix is actually about — goes unexercised.
    """
    evidence, assets, vendors, findings = [], [], [], []
    for i, op in enumerate(operations):
        args = op.get("arguments") or {}
        marker = (op.get("idempotency") or {}).get("marker")
        if marker:
            form = args.get("form") or {}
            evidence.append(
                {
                    "id": f"NORU-EVD-{i}",
                    "description": form.get("description", args.get("description", marker)),
                    "expiresAt": args.get("expiresAt"),
                    "linkedControls": [
                        {"id": m.get("controlId")} for m in (args.get("controlMappings") or [])
                    ],
                }
            )
        if op["operation"] == "createAsset":
            assets.append({"id": f"NORU-ASSET-{i}", **args})
        elif op["operation"] == "createVendor":
            vendors.append({"id": f"NORU-VENDOR-{i}", "name": args.get("name")})
        elif op["operation"] == "createSecurityFinding":
            findings.append({"id": f"NORU-SF-{i}", **args})
    return {
        "fetched_at": "2026-08-27T09:14:00Z",
        "evidence": evidence,
        "assets": assets,
        "vendors": vendors,
        "ai_controls": [],
        "security_findings": findings,
    }


def test_loader_independence(results, tmp):
    """The plan must not change because a different YAML loader parsed the same manifest.

    A piece that renders manifest prose into a content marker has an identity that depends on which
    loader ran: push from a laptop without PyYAML, push again from CI with it, and the second push
    files a duplicate instead of skipping. No amount of re-running on one machine finds that, which
    is why it is asserted here rather than left to the reviewer.

    Both loaders run here, in this interpreter — PyYAML as the validator would pick it up, and the
    bundled fallback forced by hiding PyYAML behind a stub that raises on import. This used to
    simulate the difference instead, by re-spacing prose the way the other loader would have written
    it, because the pieces defended themselves by normalising whitespace out of everything they
    digested. They no longer do: the loaders agree, so the plan is compared under both rather than
    against a guess at what one of them would have produced.

    Having two loaders to compare means having PyYAML importable, so this reports itself skipped
    where it is not, rather than passing while checking nothing. The CI matrix runs a leg with it
    installed, and `scripts/test_validators.py` pins the fallback loader's output to PyYAML's on
    every machine including that one.

    Every declared piece is checked, not a list maintained here, for the reason given in the module
    docstring. Each one is checked twice — writing into an empty organization, and into one the same
    plan has already been pushed into — because only the second reaches the marker matching and the
    skip reasons, which is the code a loader-dependent identity actually breaks.
    """
    if importlib.util.find_spec("yaml") is None:
        results.skip(
            "the plan is the same whichever YAML loader parsed the manifest",
            "PyYAML is not importable here, so there is only one loader and nothing to compare. "
            "The CI matrix runs a leg with it installed; test_validators.py checks the fallback "
            "loader against pinned PyYAML output on every machine, including this one.",
        )
        return

    fallback_env = _fallback_only_env(tmp)
    for name in declared_pieces():
        repo = pathlib.Path(tmp) / f"{name}-loader-repo"
        shutil.copytree(FIXTURE_REPO, repo)
        (repo / ".noru" / ".cache").mkdir(parents=True, exist_ok=True)
        piece = PLUGINS / name
        decl = json.loads((piece / "piece.json").read_text(encoding="utf-8"))
        manifest = repo / decl["artifact"]
        manifest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(piece / decl["validator"]["fixtures"]["valid"][0], manifest)
        empty_org = {"fetched_at": "2026-08-27T09:14:00Z", "evidence": [], "security_findings": []}
        write_state(repo, empty_org)

        parsed = repo / ".noru" / ".cache" / f"{name}.parsed.json"
        documents = {}
        for loader, env in (("PyYAML", None), ("the bundled fallback", fallback_env)):
            validated = run(
                [
                    "python3",
                    str(piece / "scripts" / "validate_manifest.py"),
                    str(manifest),
                    f"--emit-parsed={parsed}",
                    "--quiet",
                ],
                env=env,
            )
            if not results.check(
                f"[{name}] manifest validates under {loader}",
                validated.returncode == 0,
                validated.stdout[:300],
            ):
                break
            documents[loader] = json.loads(parsed.read_text(encoding="utf-8"))
        if len(documents) != 2:
            continue

        # Not the point of this test, but a free and much sharper failure message when it does go
        # wrong: a plan can only move because the documents did.
        results.check(
            f"[{name}] both loaders parse the manifest to the same document",
            documents["PyYAML"] == documents["the bundled fallback"],
            "; ".join(
                f"{path}: {left} != {right}"
                for path, left, right in _differences(
                    documents["PyYAML"], documents["the bundled fallback"]
                )
            ),
        )

        parsed.write_text(json.dumps(documents["PyYAML"], indent=2), encoding="utf-8")
        first = diff(piece, repo)
        if not results.check(f"[{name}] diff succeeds as parsed", first.returncode == 0, first.stderr):
            continue
        as_parsed = json.loads(first.stdout)["operations"]

        # Against an empty organization every operation is a create, so that comparison alone never
        # reaches an existence check, a marker match or a skip reason — which is the half of the code
        # this is really about. So the plan is compared twice: once writing into an empty
        # organization, and once writing into one the same plan has already been pushed into.
        rounds = [("into an empty organization", empty_org)]
        pushed_org = _org_after(as_parsed)
        write_state(repo, pushed_org)
        if results.check(
            f"[{name}] diff succeeds against an organization already holding the plan",
            diff(piece, repo).returncode == 0,
            "",
        ):
            rounds.append(("into an organization already holding it", pushed_org))

        for label, org in rounds:
            # The plan as PyYAML parsed the manifest, then as the fallback loader parsed the same
            # bytes. Same organization both times.
            write_state(repo, org)
            parsed.write_text(json.dumps(documents["PyYAML"], indent=2), encoding="utf-8")
            expected = json.loads(diff(piece, repo).stdout)["operations"]

            parsed.write_text(
                json.dumps(documents["the bundled fallback"], indent=2), encoding="utf-8"
            )
            other = diff(piece, repo)
            if not results.check(
                f"[{name}] diff succeeds as the fallback loader parsed it, {label}",
                other.returncode == 0,
                other.stderr,
            ):
                continue

            as_fallback = json.loads(other.stdout)["operations"]
            results.check(
                f"[{name}] THE PLAN IS THE SAME WHICHEVER YAML LOADER PARSED THE MANIFEST ({label})",
                expected == as_fallback,
                "; ".join(
                    f"operations{path}: {left} != {right}"
                    for path, left, right in _differences(expected, as_fallback)
                ),
            )


# Every piece, and the test that proves re-running it is a no-op. A piece missing from this table is
# reported as a failure by main() — see the note in the module docstring about gates that overclaim.
IDEMPOTENCY_TESTS = {
    "ai-inventory": test_ai_inventory,
    "evidence-push": test_evidence_push,
    "governance-records": test_governance_records,
    "review-signoff": test_review_signoff,
    "iac-scan": test_iac_scan,
    "audit-pack": test_audit_pack,
}


def declared_pieces():
    """Read the pieces off disk rather than from a list maintained by hand in this file."""
    plugins = ROOT / "plugins"
    if not plugins.is_dir():
        return []
    return sorted(p.name for p in plugins.iterdir() if (p / "piece.json").is_file())


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
            sys.stdout.write("usage: test_idempotency.py [--output=json] [--quiet]\n")
            return 0
        else:
            sys.stderr.write(f"error: unknown option '{arg}'\n")
            return 2

    if not FIXTURE_REPO.is_dir():
        sys.stderr.write(f"error: fixture repository missing at {FIXTURE_REPO}\n")
        return 2

    pieces = declared_pieces()
    if not pieces:
        sys.stderr.write("error: no pieces found (a piece is a plugin directory with a piece.json)\n")
        return 2

    results = Results()
    covered = []
    with tempfile.TemporaryDirectory(prefix="noru-idempotency-") as tmp:
        for name in pieces:
            test = IDEMPOTENCY_TESTS.get(name)
            if test is None:
                results.check(
                    f"[{name}] has an idempotency test",
                    False,
                    "no entry in IDEMPOTENCY_TESTS. A second push being a no-op is the property "
                    "this file exists to prove, so an unregistered piece is an untested piece and "
                    "this gate must not pass while claiming to cover every piece",
                )
                continue
            results.check(f"[{name}] has an idempotency test", True)
            covered.append(name)
            test(results, tmp)
        test_loader_independence(results, tmp)

    ok = not results.failures
    if output_json:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": ok,
                    "total": len(results.rows),
                    "pieces": pieces,
                    "covered": covered,
                    "results": results.rows,
                },
                indent=None if quiet else 2,
            )
            + "\n"
        )
        return 0 if ok else 1

    for row in results.rows:
        if not row["ok"]:
            print(f"  FAIL  {row['test']}")
            if row["detail"]:
                print(f"        {row['detail']}")
        elif row.get("skipped"):
            # Printed even under --quiet. A check that did not run is the one thing a reader
            # skimming a passing run still needs to see.
            print(f"  SKIP  {row['test']}")
            print(f"        {row['detail']}")
        elif not quiet:
            print(f"  ok    {row['test']}")
    if ok:
        # Say what was actually exercised. main() fails above on any piece it did not cover, so this
        # list and `pieces` are the same set whenever this line is reached — but print the list, not
        # the claim, so a future gap shows up in the output instead of hiding behind the wording.
        print(
            f"\nOK: {len(results.rows)} test(s) passed. A second push is a no-op for each of the "
            f"{len(covered)} piece(s) exercised: {', '.join(covered)}."
        )
        return 0
    print(f"\nFAILED: {len(results.failures)} of {len(results.rows)} test(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
