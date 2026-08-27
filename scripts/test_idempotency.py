#!/usr/bin/env python3
"""End-to-end test of the property that matters most: a second push must be a no-op.

Standard library only, and no network. Every piece declared under plugins/ is driven through its
real scan -> validate -> diff -> push scripts against a fixture repository and a fixture snapshot
of a Noru organization. Pieces are enumerated from disk, and a piece with no test registered in
IDEMPOTENCY_TESTS is a FAILURE rather than a silent pass: a gate that reports "every piece" while
covering half of them launders an untested piece as tested, which is worse than no gate at all. The snapshot for the second round is derived from what the first round said it
would write, so this asserts the real thing: **if the writes we planned actually happened, does the
next run correctly decide there is nothing to do?**

This cannot replace running against a live organization — see the Maturity section of
docs/verification.md. What it does replace is the class of idempotency bug that survives review
because nobody re-ran the command twice.

Usage:
    python3 scripts/test_idempotency.py [--output=json] [--quiet]
Exit codes: 0 = pass, 1 = a piece is not idempotent, 2 = usage / setup error.
"""
import json
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

    @property
    def failures(self):
        return [r for r in self.rows if not r["ok"]]


def run(cmd, env=None):
    import os

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


# Every piece, and the test that proves re-running it is a no-op. A piece missing from this table is
# reported as a failure by main() — see the note in the module docstring about gates that overclaim.
IDEMPOTENCY_TESTS = {
    "ai-inventory": test_ai_inventory,
    "evidence-push": test_evidence_push,
    "governance-records": test_governance_records,
    "review-signoff": test_review_signoff,
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
