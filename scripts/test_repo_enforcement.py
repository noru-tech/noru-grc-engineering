#!/usr/bin/env python3
"""Regression tests for offline ratchets, repository setup, and GitHub plan/apply safety."""

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "repo-enforcement"
ENFORCE = PLUGIN / "scripts" / "enforce.py"
CONFIGURE = PLUGIN / "scripts" / "configure.mjs"
GITHUB_PLAN = PLUGIN / "scripts" / "github-plan.mjs"
GITHUB_APPLY = PLUGIN / "scripts" / "github-apply.mjs"
GITHUB_VERIFY = PLUGIN / "scripts" / "github-verify.mjs"
REGISTRY = ROOT / "actions" / "enforce" / "registry.json"
ACTION = ROOT / "actions" / "enforce" / "dist" / "enforce.js"


class Results:
    def __init__(self):
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append({"test": name, "ok": bool(ok), "detail": str(detail)[:1000]})

    def finish(self, output_json, quiet):
        ok = all(row["ok"] for row in self.rows)
        if output_json:
            print(json.dumps({"ok": ok, "total": len(self.rows), "results": self.rows}))
        elif not quiet:
            for row in self.rows:
                print(f"  {'ok' if row['ok'] else 'FAIL':4}  {row['test']}")
            print(f"\n{'OK' if ok else 'FAILED'}: {len(self.rows)} repository enforcement tests.")
        return 0 if ok else 1


def run(args, cwd=None):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False, timeout=180)


def init_repo(path):
    path.mkdir()
    run(["git", "init", "-q", "-b", "main"], path)
    run(["git", "config", "user.name", "Fixture User"], path)
    run(["git", "config", "user.email", "fixture@example.com"], path)
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    run(["git", "add", "README.md"], path)
    run(["git", "commit", "-q", "-m", "fixture"], path)
    return run(["git", "rev-parse", "HEAD"], path).stdout.strip()


def configure(repo):
    return run(
        [
            "node", str(CONFIGURE), "plan", f"--repo={repo}",
            "--action-sha=" + "a" * 40,
            "--grc-reviewers=@example/grc-reviewers",
            "--privacy-reviewers=@example/privacy-reviewers",
            "--security-reviewers=@example/security-reviewers",
            "--break-glass=@example/security-admins",
            "--mode=ratchet", "--output=json", "--quiet",
        ]
    )


def policy_validation_tests(results, repo):
    first = run([
        sys.executable, str(ENFORCE), "validate", f"--repo={repo}", f"--suite-root={ROOT}",
        f"--registry={REGISTRY}", "--as-of=2026-09-04", "--output=json", "--quiet",
    ])
    payload = json.loads(first.stdout)
    results.check("whole-repository validation reports current debt", first.returncode == 1 and payload["new_violations"], first.stderr or payload)
    fingerprints = [row["fingerprint"] for row in payload["new_violations"]]
    results.check("violation fingerprints are unique", len(fingerprints) == len(set(fingerprints)), fingerprints)

    proposal = run([
        sys.executable, str(ENFORCE), "baseline", "propose", f"--repo={repo}",
        f"--suite-root={ROOT}", f"--registry={REGISTRY}", "--as-of=2026-09-04",
        "--output=json", "--quiet",
    ])
    proposed = json.loads(proposal.stdout)
    results.check("baseline propose is explicitly non-authoritative", proposal.returncode == 0 and proposed["status"] == "proposal_only" and all(not row["owner"] for row in proposed["violations"]), proposed)

    accepted = {
        "version": 1,
        "policy_digest": payload["policy_digest"],
        "violations": [
            {
                "piece": row["piece"], "rule": row["rule"], "subject": row["subject"],
                "fingerprint": row["fingerprint"], "owner": "Dana Okafor",
                "decided_at": "2026-09-01", "expires_at": "2026-09-30",
                "rationale": "Temporary acceptance while the existing repository debt is resolved.",
            }
            for row in payload["new_violations"] if row["baselineable"]
        ],
    }
    baseline = repo / ".noru" / "enforcement-baseline.json"
    baseline.write_text(json.dumps(accepted, indent=2) + "\n", encoding="utf-8")
    second = run([
        sys.executable, str(ENFORCE), "baseline", "check", f"--repo={repo}",
        f"--suite-root={ROOT}", f"--registry={REGISTRY}", "--as-of=2026-09-04",
        "--output=json", "--quiet",
    ])
    second_payload = json.loads(second.stdout)
    results.check("exact live debt is accepted by the ratchet", second.returncode == 0 and not second_payload["new_violations"] and len(second_payload["baselined_violations"]) == len(accepted["violations"]), second_payload)

    accepted["violations"][0]["fingerprint"] = "sha256:" + "f" * 64
    baseline.write_text(json.dumps(accepted, indent=2) + "\n", encoding="utf-8")
    mutated = run([
        sys.executable, str(ENFORCE), "baseline", "check", f"--repo={repo}",
        f"--suite-root={ROOT}", f"--registry={REGISTRY}", "--as-of=2026-09-04",
        "--output=json", "--quiet",
    ])
    mutated_payload = json.loads(mutated.stdout)
    results.check("mutated debt creates both new and stale failures", mutated.returncode == 1 and mutated_payload["new_violations"] and mutated_payload["stale_baseline_entries"], mutated_payload)

    accepted["violations"][0]["fingerprint"] = fingerprints[0]
    accepted["violations"][0]["decided_at"] = "2026-08-01"
    accepted["violations"][0]["expires_at"] = "2026-09-03"
    baseline.write_text(json.dumps(accepted, indent=2) + "\n", encoding="utf-8")
    expired = run([
        sys.executable, str(ENFORCE), "baseline", "check", f"--repo={repo}",
        f"--suite-root={ROOT}", f"--registry={REGISTRY}", "--as-of=2026-09-04",
        "--output=json", "--quiet",
    ])
    expired_payload = json.loads(expired.stdout)
    results.check("expired baseline acceptance fails", expired.returncode == 1 and expired_payload["expired_exceptions"], expired_payload)

    accepted["violations"][0]["decided_at"] = "2026-09-01"
    accepted["violations"][0]["expires_at"] = "2026-09-30"
    accepted["policy_digest"] = "0" * 64
    baseline.write_text(json.dumps(accepted, indent=2) + "\n", encoding="utf-8")
    changed_policy = run([
        sys.executable, str(ENFORCE), "baseline", "check", f"--repo={repo}",
        f"--suite-root={ROOT}", f"--registry={REGISTRY}", "--as-of=2026-09-04",
        "--output=json", "--quiet",
    ])
    changed_payload = json.loads(changed_policy.stdout)
    results.check(
        "a baseline bound to another policy is invalid",
        changed_policy.returncode == 1
        and any(row["rule"] == "invalid_baseline" and not row["baselineable"] for row in changed_payload["new_violations"]),
        changed_payload,
    )

    spec = importlib.util.spec_from_file_location("repo_enforcement", ENFORCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    one = module.violation_from_finding("privacy-datamap", {"kind": "invalid", "path": "dataset[0].field.needs_review", "message": "line 10", "refs": ["db.sql:10"]})
    two = module.violation_from_finding("privacy-datamap", {"kind": "invalid", "path": "dataset[0].field.needs_review", "message": "line 40", "refs": ["db.sql:40"]})
    results.check("line movement does not change a violation fingerprint", one["fingerprint"] == two["fingerprint"], (one, two))
    never = module.violation_from_finding("repo-enforcement", {"kind": "ruleset_drift", "path": "github", "message": "weakened"})
    results.check("ruleset drift can never be baselined", never["baselineable"] is False, never)


def github_tests(results, repo, commit):
    state = json.loads((PLUGIN / "fixtures" / "github-no-ruleset.json").read_text(encoding="utf-8"))
    state["repository_commit"] = commit
    state_path = repo / "github-state.json"
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    stale_plan = run(["node", str(GITHUB_PLAN), f"--repo={repo}", f"--state={state_path}", "--now=2000-01-01T00:00:00Z", "--output=json", "--quiet"])
    stale_apply = run(["node", str(GITHUB_APPLY), f"--repo={repo}", f"--state={state_path}", "--confirm", "--output=json", "--quiet"])
    results.check("expired GitHub plans cannot be applied", stale_plan.returncode == 0 and stale_apply.returncode == 1 and "expired" in stale_apply.stderr, stale_apply.stderr)

    planned = run(["node", str(GITHUB_PLAN), f"--repo={repo}", f"--state={state_path}", "--now=2099-01-01T00:00:00Z", "--output=json", "--quiet"])
    planned_payload = json.loads(planned.stdout)
    results.check("GitHub plan creates only a dedicated ruleset", planned.returncode == 0 and planned_payload["counts"] == {"create": 1, "update": 0, "skip": 0}, planned.stderr or planned_payload)
    refused = run(["node", str(GITHUB_APPLY), f"--repo={repo}", f"--state={state_path}", "--output=json", "--quiet"])
    results.check("GitHub apply without confirmation is refused", refused.returncode == 2 and "--confirm" in refused.stderr, refused.stderr)

    rebound = json.loads(json.dumps(state))
    rebound["check_runs"][0]["app_id"] = 99999
    rebound_path = repo / "github-rebound.json"
    rebound_path.write_text(json.dumps(rebound, indent=2) + "\n", encoding="utf-8")
    rebound_apply = run(["node", str(GITHUB_APPLY), f"--repo={repo}", f"--state={rebound_path}", "--confirm", "--output=json", "--quiet"])
    results.check("a check-source change invalidates the GitHub plan", rebound_apply.returncode == 1 and "check_integration_id changed" in rebound_apply.stderr, rebound_apply.stderr)

    no_admin = json.loads(json.dumps(state))
    no_admin["permissions"]["admin"] = False
    no_admin_path = repo / "github-no-admin.json"
    no_admin_path.write_text(json.dumps(no_admin, indent=2) + "\n", encoding="utf-8")
    no_admin_apply = run(["node", str(GITHUB_APPLY), f"--repo={repo}", f"--state={no_admin_path}", "--confirm", "--output=json", "--quiet"])
    results.check("apply stops when Administration write is unavailable", no_admin_apply.returncode == 1 and "Administration: write" in no_admin_apply.stderr, no_admin_apply.stderr)

    changed = json.loads(json.dumps(state))
    changed["rulesets"] = [{"id": 55, "name": "Noru GRC — governed development", "source_type": "Repository", "target": "branch", "enforcement": "disabled", "updated_at": "2099-01-01T00:00:00Z", "rules": []}]
    changed_path = repo / "github-concurrent.json"
    changed_path.write_text(json.dumps(changed, indent=2) + "\n", encoding="utf-8")
    changed_apply = run(["node", str(GITHUB_APPLY), f"--repo={repo}", f"--state={changed_path}", "--confirm", "--output=json", "--quiet"])
    results.check("a concurrent ruleset change invalidates the plan", changed_apply.returncode == 1 and "ruleset_id changed" in changed_apply.stderr, changed_apply.stderr)

    applied_state = repo / "github-applied.json"
    applied = run(["node", str(GITHUB_APPLY), f"--repo={repo}", f"--state={state_path}", f"--write-state={applied_state}", "--confirm", "--output=json", "--quiet"])
    results.check("confirmed fixture apply verifies effective rules", applied.returncode == 0 and json.loads(applied.stdout)["verified"], applied.stderr or applied.stdout)
    second = run(["node", str(GITHUB_PLAN), f"--repo={repo}", f"--state={applied_state}", "--now=2099-01-01T00:00:00Z", "--output=json", "--quiet"])
    results.check("a second unchanged GitHub plan is a no-op", second.returncode == 0 and json.loads(second.stdout)["counts"]["skip"] == 1, second.stderr or second.stdout)
    second_apply = run(["node", str(GITHUB_APPLY), f"--repo={repo}", f"--state={applied_state}", "--confirm", "--output=json", "--quiet"])
    results.check("a second unchanged GitHub apply reports only a skip", second_apply.returncode == 0 and json.loads(second_apply.stdout)["counts"] == {"create": 0, "update": 0, "skip": 1}, second_apply.stderr or second_apply.stdout)

    duplicate = json.loads(applied_state.read_text(encoding="utf-8"))
    duplicate["rulesets"].append({**duplicate["rulesets"][0], "id": 9002})
    duplicate_path = repo / "github-duplicate.json"
    duplicate_path.write_text(json.dumps(duplicate, indent=2) + "\n", encoding="utf-8")
    duplicate_plan = run(["node", str(GITHUB_PLAN), f"--repo={repo}", f"--state={duplicate_path}", "--output=json", "--quiet"])
    results.check("duplicate managed rulesets require explicit migration", duplicate_plan.returncode == 1 and "explicit migration" in duplicate_plan.stderr, duplicate_plan.stderr)

    weakened = json.loads(applied_state.read_text(encoding="utf-8"))
    weakened["rulesets"][0]["enforcement"] = "disabled"
    weakened["rulesets"][0]["bypass_actors"] = [{"actor_id": 9, "actor_type": "Team", "bypass_mode": "always"}]
    weakened_path = repo / "github-weakened.json"
    weakened_path.write_text(json.dumps(weakened, indent=2) + "\n", encoding="utf-8")
    verified = run(["node", str(GITHUB_VERIFY), f"--repo={repo}", f"--state={weakened_path}", "--output=json", "--quiet"])
    kinds = {row["kind"] for row in json.loads(verified.stdout)["findings"]}
    results.check("verification detects disabled rulesets and new bypass actors", verified.returncode == 1 and {"ruleset_disabled", "bypass_added"} <= kinds, kinds)

    missing_team = dict(state)
    missing_team["teams"] = state["teams"][:-1]
    missing_path = repo / "github-missing-team.json"
    missing_path.write_text(json.dumps(missing_team, indent=2) + "\n", encoding="utf-8")
    team_plan = run(["node", str(GITHUB_PLAN), f"--repo={repo}", f"--state={missing_path}", "--output=json", "--quiet"])
    results.check("planning fails when a configured team cannot be resolved", team_plan.returncode == 1 and "do not resolve" in team_plan.stderr, team_plan.stderr)

    inherited = json.loads(applied_state.read_text(encoding="utf-8"))
    inherited["rulesets"][0]["source_type"] = "Organization"
    inherited_path = repo / "github-inherited.json"
    inherited_path.write_text(json.dumps(inherited, indent=2) + "\n", encoding="utf-8")
    policy_path = repo / ".noru" / "enforcement.yml"
    original_policy = policy_path.read_text(encoding="utf-8")
    policy_path.write_text(original_policy.replace("scope: repository", "scope: organization"), encoding="utf-8")
    inherited_verify = run(["node", str(GITHUB_VERIFY), f"--repo={repo}", f"--state={inherited_path}", "--output=json", "--quiet"])
    results.check("verification evaluates inherited organization rules", inherited_verify.returncode == 0, inherited_verify.stderr or inherited_verify.stdout)
    policy_path.write_text(original_policy, encoding="utf-8")


def main(argv):
    output_json = "--output=json" in argv
    quiet = "--quiet" in argv
    unknown = [arg for arg in argv if arg not in {"--output=json", "--output=text", "--quiet"}]
    if unknown:
        print(f"error: unknown option '{unknown[0]}'", file=sys.stderr)
        return 2
    results = Results()
    with tempfile.TemporaryDirectory(prefix="noru-repo-enforcement-") as tmp:
        repo = pathlib.Path(tmp) / "repo"
        commit = init_repo(repo)
        (repo / ".github").mkdir()
        (repo / ".github" / "CODEOWNERS").write_text("/docs/ @example/docs\n", encoding="utf-8")
        plan = configure(repo)
        results.check("setup produces a reviewable local file plan", plan.returncode == 0 and json.loads(plan.stdout)["counts"]["create"] >= 3, plan.stderr or plan.stdout)
        refused = run(["node", str(CONFIGURE), "apply", f"--repo={repo}", "--output=json", "--quiet"])
        results.check("local setup apply requires confirmation", refused.returncode == 2, refused.stderr)
        plan_path = repo / ".noru" / ".cache" / "repo-enforcement-files.json"
        tampered = json.loads(plan_path.read_text(encoding="utf-8"))
        tampered["operations"][0]["content"] += "# injected\n"
        plan_path.write_text(json.dumps(tampered, indent=2) + "\n", encoding="utf-8")
        tampered_apply = run(["node", str(CONFIGURE), "apply", f"--repo={repo}", "--confirm", "--output=json", "--quiet"])
        results.check("a modified local file plan is rejected", tampered_apply.returncode == 1 and "digest is invalid" in tampered_apply.stderr, tampered_apply.stderr)
        configure(repo)
        applied = run(["node", str(CONFIGURE), "apply", f"--repo={repo}", "--confirm", "--output=json", "--quiet"])
        codeowners = (repo / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        results.check("confirmed setup preserves existing CODEOWNERS and protects itself", applied.returncode == 0 and "/docs/ @example/docs" in codeowners and "/.github/CODEOWNERS @example/grc-reviewers" in codeowners, applied.stderr or codeowners)
        policy_validation_tests(results, repo)
        action_env = {
            **dict(os.environ), "GITHUB_WORKSPACE": str(repo),
            "INPUT_AS-OF": "2026-09-04", "INPUT_REPO": ".",
            "INPUT_POLICY": ".noru/enforcement.yml", "RUNNER_TEMP": str(repo / ".noru" / ".cache"),
        }
        action = subprocess.run(["node", str(ACTION)], cwd=repo, env=action_env, text=True, capture_output=True, check=False, timeout=180)
        action_report = repo / ".noru" / ".cache" / "noru-grc-enforcement.json"
        results.check("the bundled action fails closed and writes its JSON report", action.returncode == 1 and action_report.is_file() and not json.loads(action_report.read_text())["ok"], action.stderr or action.stdout)
        # GitHub planning reads policy only; baseline state is irrelevant from here.
        github_tests(results, repo, commit)
    return results.finish(output_json, quiet)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
