import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPTS = dirname(dirname(fileURLToPath(import.meta.url)));
const PROFILE = join(dirname(SCRIPTS), "references", "github-ruleset-profile.json");
const PLAN_REL = ".noru/.cache/repo-enforcement-github-plan.json";
const VERSION = "0.6.0";
const CHECK = "Noru GRC / validate";
const TOKEN = /\b(?:gh[pousr]_[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,})/gi;

export function redact(value) { return String(value ?? "").replace(TOKEN, "<redacted>"); }
function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, child]) => [key, stable(child)]));
  return value;
}
function digest(value) { return createHash("sha256").update(JSON.stringify(stable(value))).digest("hex"); }
function run(command, args, input = null) {
  return execFileSync(command, args, { encoding: "utf8", input, stdio: [input === null ? "ignore" : "pipe", "pipe", "pipe"], env: process.env }).trim();
}
function git(repo, args) { return run("git", ["-C", repo, ...args]); }
function repoSlug(repo) {
  const remote = git(repo, ["remote", "get-url", "origin"]);
  const match = remote.match(/[:/]([^/:]+\/[^/]+?)(?:\.git)?$/);
  if (!match) throw new Error("origin does not identify a GitHub owner/repository");
  return match[1];
}
function ghJson(args, input = null) {
  try { const text = run("gh", args, input); return text ? JSON.parse(text) : null; }
  catch (error) { throw new Error(`GitHub request failed: ${redact(error.stderr || error.message)}`); }
}
function api(path, method = "GET", body = null) {
  const args = ["api", path, "--method", method, "--header", "Accept: application/vnd.github+json"];
  if (body !== null) args.push("--input", "-");
  return ghJson(args, body === null ? null : JSON.stringify(body));
}

export function normalizeRuleset(ruleset) {
  if (!ruleset) return null;
  const rules = (ruleset.rules ?? []).map((rule) => {
    const row = { type: rule.type };
    if (rule.parameters) row.parameters = stable(rule.parameters);
    return row;
  }).sort((a, b) => a.type.localeCompare(b.type));
  return stable({
    name: ruleset.name,
    target: ruleset.target,
    enforcement: ruleset.enforcement,
    conditions: ruleset.conditions ?? {},
    bypass_actors: ruleset.bypass_actors ?? [],
    rules,
  });
}

export function inspectFixture(path) {
  const state = JSON.parse(readFileSync(path, "utf8"));
  state.rulesets = state.rulesets ?? [];
  state.teams = state.teams ?? [];
  state.check_runs = state.check_runs ?? [];
  return state;
}

export function inspectLive(repo) {
  const slug = repoSlug(repo);
  const repository = api(`repos/${slug}`);
  const listed = api(`repos/${slug}/rulesets?includes_parents=true`) ?? [];
  const rulesets = listed.map((row) => api(`repos/${slug}/rulesets/${row.id}?includes_parents=true`));
  const checks = api(`repos/${slug}/commits/${repository.default_branch}/check-runs`)?.check_runs ?? [];
  const workflowPath = join(repo, ".github", "workflows", "noru-grc.yml");
  const workflowText = existsSync(workflowPath) ? readFileSync(workflowPath, "utf8") : "";
  const codeownersText = existsSync(join(repo, ".github", "CODEOWNERS")) ? readFileSync(join(repo, ".github", "CODEOWNERS"), "utf8") : "";
  const actionUse = workflowText.match(/^\s*uses:\s*noru-tech\/noru-grc-engineering\/actions\/enforce@([^\s#]+)\s*$/m)?.[1] ?? null;
  return {
    host: "github.com",
    organization: { login: repository.owner.login, id: repository.owner.id },
    repository: { full_name: repository.full_name, id: repository.id, default_branch: repository.default_branch },
    repository_commit: git(repo, ["rev-parse", "HEAD"]),
    rulesets,
    check_runs: checks.map((row) => ({ name: row.name, app_id: row.app?.id ?? null, conclusion: row.conclusion })),
    workflow: {
      present: Boolean(workflowText),
      immutable_pin: Boolean(actionUse && /^[0-9a-f]{40}$/i.test(actionUse)),
      action_ref: actionUse,
    },
    codeowners: {
      present: Boolean(codeownersText),
      protects_self: /^\/\.github\/CODEOWNERS\s+@[^\s/]+\/[^\s]+\s*$/m.test(codeownersText),
    },
    permissions: repository.permissions ?? {},
    teams: [],
  };
}

function policy(repo) {
  const text = run("python3", [join(SCRIPTS, "enforce.py"), "policy", `--repo=${repo}`, "--output=json", "--quiet"]);
  return JSON.parse(text);
}
function currentManaged(state, name) {
  const rows = state.rulesets.filter((row) => row.name === name && (row.source_type ?? "Repository") === "Repository");
  if (rows.length > 1) throw new Error(`found ${rows.length} repository rulesets named '${name}'; explicit migration is required`);
  return rows[0] ?? null;
}
function effectiveManaged(state, name, scope) {
  if (scope === "repository") return currentManaged(state, name);
  const rows = state.rulesets.filter((row) => row.name === name && row.source_type === "Organization");
  if (rows.length > 1) throw new Error(`found ${rows.length} inherited organization rulesets named '${name}'`);
  return rows[0] ?? null;
}
function checkSource(state, name) {
  const rows = state.check_runs.filter((row) => row.name === name && row.app_id && row.conclusion === "success");
  const ids = [...new Set(rows.map((row) => row.app_id))];
  if (ids.length !== 1) throw new Error(`expected one source integration for '${name}' after a real successful run; found ${ids.length}`);
  return ids[0];
}
function ensureTeams(state, ownership, fixtureMode) {
  const names = [...new Set(Object.values(ownership))];
  if (fixtureMode) {
    const available = new Set(state.teams.map((row) => `@${row.organization}/${row.slug}`));
    const missing = names.filter((name) => !available.has(name));
    if (missing.length) throw new Error(`configured GitHub team(s) do not resolve: ${missing.join(", ")}`);
    return;
  }
  for (const name of names) {
    const [, org, slug] = name.match(/^@([^/]+)\/(.+)$/) ?? [];
    if (!org || !slug) throw new Error(`invalid team '${name}'`);
    api(`orgs/${org}/teams/${slug}`);
  }
}
function desired(policyDoc, integrationId) {
  const profile = JSON.parse(readFileSync(PROFILE, "utf8"));
  profile.name = policyDoc.github.ruleset_name;
  profile.enforcement = policyDoc.github.enforcement ?? "active";
  const pull = profile.rules.find((row) => row.type === "pull_request").parameters;
  pull.required_approving_review_count = policyDoc.reviews.minimum_approvals;
  pull.dismiss_stale_reviews_on_push = policyDoc.reviews.dismiss_stale_approvals;
  pull.require_code_owner_review = policyDoc.reviews.require_code_owner_review;
  pull.require_last_push_approval = policyDoc.reviews.require_last_push_approval;
  pull.required_review_thread_resolution = policyDoc.reviews.require_thread_resolution;
  const status = profile.rules.find((row) => row.type === "required_status_checks");
  status.parameters.required_status_checks = [{ context: policyDoc.github.required_check, integration_id: integrationId }];
  return normalizeRuleset(profile);
}
function plannedOperation(state, policyDoc, integrationId, current) {
  const wanted = desired(policyDoc, integrationId);
  const same = current && digest(normalizeRuleset(current)) === digest(wanted);
  return same ? { action: "skip" } : current
    ? { action: "update", method: "PUT", endpoint: `repos/${state.repository.full_name}/rulesets/${current.id}`, body: wanted }
    : { action: "create", method: "POST", endpoint: `repos/${state.repository.full_name}/rulesets`, body: wanted };
}
function bindings(state, policyDigest, current, integrationId = null) {
  return {
    api_host: state.host,
    organization_id: state.organization.id,
    repository_id: state.repository.id,
    repository_name: state.repository.full_name,
    default_branch: state.repository.default_branch,
    repository_commit: state.repository_commit,
    policy_digest: policyDigest,
    check_integration_id: integrationId,
    ruleset_id: current?.id ?? null,
    ruleset_updated_at: current?.updated_at ?? null,
    ruleset_digest: current ? digest(normalizeRuleset(current)) : null,
  };
}

export function plan(repo, state, policyPayload, nowValue, fixtureMode) {
  const policyDoc = policyPayload.policy;
  if (policyDoc.github.scope !== "repository") throw new Error("organization scope is inspect/verify only in this release; repository apply is the supported pilot path");
  ensureTeams(state, policyDoc.ownership, fixtureMode);
  const integrationId = checkSource(state, policyDoc.github.required_check);
  const current = currentManaged(state, policyDoc.github.ruleset_name);
  const operation = plannedOperation(state, policyDoc, integrationId, current);
  const now = nowValue ? new Date(nowValue) : new Date();
  if (Number.isNaN(now.valueOf())) throw new Error("--now must be an ISO timestamp");
  const document = {
    version: 1,
    plugin_version: VERSION,
    created_at: now.toISOString(),
    expires_at: new Date(now.valueOf() + 60 * 60 * 1000).toISOString(),
    bindings: bindings(state, policyPayload.policy_digest, current, integrationId),
    required_permissions: ["administration:write"],
    operation,
  };
  document.plan_digest = digest(document);
  const path = join(repo, PLAN_REL);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `${JSON.stringify(document, null, 2)}\n`, "utf8");
  return { ok: true, plan: PLAN_REL, counts: { create: operation.action === "create" ? 1 : 0, update: operation.action === "update" ? 1 : 0, skip: operation.action === "skip" ? 1 : 0 }, bindings: document.bindings, operation: { ...operation, body: operation.body ? "<bound in plan>" : undefined } };
}

function verifyBindings(repo, state, policyPayload, document) {
  const { plan_digest: recordedDigest, ...unsignedPlan } = document;
  if (recordedDigest !== digest(unsignedPlan)) throw new Error("GitHub plan digest is invalid; re-plan");
  if (document.plugin_version !== VERSION) throw new Error("GitHub plan was created by another plugin version; re-plan");
  if (new Date(document.expires_at) <= new Date()) throw new Error("GitHub plan expired; re-plan");
  const current = currentManaged(state, policyPayload.policy.github.ruleset_name);
  const integrationId = checkSource(state, policyPayload.policy.github.required_check);
  const actual = bindings(state, policyPayload.policy_digest, current, integrationId);
  for (const key of Object.keys(document.bindings)) {
    if (document.bindings[key] !== actual[key]) throw new Error(`${key} changed after planning; re-plan`);
  }
  const expectedOperation = plannedOperation(state, policyPayload.policy, integrationId, current);
  if (digest(document.operation) !== digest(expectedOperation)) throw new Error("GitHub plan operation no longer matches trusted desired state; re-plan");
  if (document.bindings.repository_commit !== git(repo, ["rev-parse", "HEAD"])) throw new Error("repository commit changed after planning; re-plan");
}

export function apply(repo, state, policyPayload, confirm, fixturePath, writeState) {
  if (!confirm) throw Object.assign(new Error("refusing to mutate GitHub without --confirm"), { usage: true });
  if (state.permissions?.admin !== true) throw new Error("GitHub repository Administration: write permission is required for apply");
  const planPath = join(repo, PLAN_REL);
  if (!existsSync(planPath)) throw new Error(`no GitHub plan at ${PLAN_REL}; run github plan`);
  const document = JSON.parse(readFileSync(planPath, "utf8"));
  verifyBindings(repo, state, policyPayload, document);
  const operation = document.operation;
  let resulting = state;
  if (operation.action !== "skip") {
    if (fixturePath) {
      resulting = JSON.parse(JSON.stringify(state));
      const simulated = { ...operation.body, id: document.bindings.ruleset_id ?? 9001, source_type: "Repository", updated_at: document.created_at };
      resulting.rulesets = resulting.rulesets.filter((row) => row.id !== simulated.id && !(row.name === simulated.name && (row.source_type ?? "Repository") === "Repository"));
      resulting.rulesets.push(simulated);
      if (writeState) writeFileSync(writeState, `${JSON.stringify(resulting, null, 2)}\n`, "utf8");
    } else {
      api(operation.endpoint, operation.method, operation.body);
      resulting = inspectLive(repo);
    }
  }
  const verification = verify(resulting, policyPayload.policy, false);
  if (!verification.ok) throw new Error(`GitHub apply completed but effective verification failed: ${verification.findings.map((row) => row.kind).join(", ")}`);
  return { ok: true, verified: true, counts: { create: operation.action === "create" ? 1 : 0, update: operation.action === "update" ? 1 : 0, skip: operation.action === "skip" ? 1 : 0 }, state: resulting };
}

function rule(ruleset, type) { return (ruleset?.rules ?? []).find((row) => row.type === type); }
export function verify(state, policyDoc, requireWorkflow = true) {
  const findings = [];
  const managed = effectiveManaged(state, policyDoc.github.ruleset_name, policyDoc.github.scope);
  if (!managed) findings.push({ kind: "ruleset_missing", message: `managed ${policyDoc.github.scope} ruleset is absent` });
  else {
    if (managed.enforcement !== "active") findings.push({ kind: "ruleset_disabled", message: `managed ruleset enforcement is ${managed.enforcement}` });
    if ((managed.bypass_actors ?? []).length) findings.push({ kind: "bypass_added", message: "managed ruleset has bypass actors" });
    if (!rule(managed, "deletion")) findings.push({ kind: "deletion_allowed", message: "branch deletion protection is absent" });
    if (!rule(managed, "non_fast_forward")) findings.push({ kind: "force_push_allowed", message: "non-fast-forward protection is absent" });
    const pull = rule(managed, "pull_request")?.parameters ?? {};
    if ((pull.required_approving_review_count ?? 0) < policyDoc.reviews.minimum_approvals) findings.push({ kind: "approval_reduced", message: "approval count is below policy" });
    for (const [field, kind] of [["dismiss_stale_reviews_on_push", "stale_review_dismissal_disabled"], ["require_code_owner_review", "codeowner_review_disabled"], ["require_last_push_approval", "last_push_approval_disabled"], ["required_review_thread_resolution", "thread_resolution_disabled"]]) if (pull[field] !== true) findings.push({ kind, message: `${field} is not effective` });
    const checks = rule(managed, "required_status_checks")?.parameters?.required_status_checks ?? [];
    const source = state.check_runs.find((row) => row.name === policyDoc.github.required_check)?.app_id;
    if (!checks.some((row) => row.context === policyDoc.github.required_check && row.integration_id === source)) findings.push({ kind: "required_check_removed_or_rebound", message: "required GRC check or its source integration does not match" });
  }
  if (requireWorkflow && state.workflow?.present !== true) findings.push({ kind: "workflow_missing", message: "Noru GRC workflow is absent" });
  if (requireWorkflow && state.workflow?.immutable_pin !== true) findings.push({ kind: "workflow_unpinned", message: "Noru GRC action is not pinned to a full commit SHA" });
  if (requireWorkflow && state.workflow?.action_ref !== policyDoc.github.action_sha) findings.push({ kind: "workflow_drift", message: "Noru GRC action SHA differs from committed policy" });
  if (requireWorkflow && state.codeowners?.present !== true) findings.push({ kind: "codeowners_missing", message: "CODEOWNERS is absent" });
  if (requireWorkflow && state.codeowners?.protects_self !== true) findings.push({ kind: "codeowners_unprotected", message: "CODEOWNERS does not protect itself" });
  const successfulChecks = state.check_runs.filter((row) => row.name === policyDoc.github.required_check && row.conclusion === "success");
  if (requireWorkflow && successfulChecks.length === 0) findings.push({ kind: "check_not_successful", message: "the required GRC check has no successful run on the protected branch" });
  return { ok: findings.length === 0, enforcement: managed?.enforcement ?? "absent", protected_branch: state.repository.default_branch, required_check: findings.some((row) => row.kind.startsWith("required_check")) ? "drift" : "effective", code_owner_review: findings.some((row) => row.kind === "codeowner_review_disabled") ? "drift" : "effective", bypass_actors: managed?.bypass_actors ?? [], findings };
}

export function loadPolicy(repo) { return policy(repo); }
export function loadState(repo, fixture) { return fixture ? inspectFixture(fixture) : inspectLive(repo); }
export function planPath(repo) { return join(repo, PLAN_REL); }
