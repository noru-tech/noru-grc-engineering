// --- BEGIN VENDORED plan ---
// Canonical copy: plugins/noru/scripts/lib/plan.mjs. Every piece vendors this file verbatim at
// plugins/<piece>/scripts/lib/plan.mjs so an installed plugin is self-contained and never imports
// across plugin boundaries. scripts/check_vendored_lib.py fails CI if a copy drifts.
//
// This is the machinery behind contract requirement 5: :diff writes a plan, :push refuses to act
// without one. Node built-ins only.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync, existsSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

export const PLAN_VERSION = 2;
export const PLAN_TTL_MS = 60 * 60 * 1000;

const PLUGIN_ROOT = dirname(dirname(dirname(fileURLToPath(import.meta.url))));

function readJson(path, label) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`${label} at ${path} is not readable JSON (${error.message})`);
  }
}

function gitValue(repo, args, fallback = null) {
  try {
    return execFileSync("git", ["-C", repo, ...args], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim() || fallback;
  } catch {
    return fallback;
  }
}

function safeLocation(value, label) {
  try {
    const url = new URL(value);
    url.username = "";
    url.password = "";
    url.search = "";
    url.hash = "";
    return url.toString();
  } catch {
    if (label === "MCP endpoint") throw new Error(`Noru state ${label} is not a valid URL`);
    return value;
  }
}

function connectionFromState(state) {
  const connection = state?.connection;
  const organization = connection?.organization;
  if (!organization?.id || !organization?.name || !connection?.endpoint) {
    throw new Error(
      "Noru state has no bound connection — refresh .noru/.cache/noru-state.json with " +
        "connection.organization { id, name }, connection.endpoint and connection.scopes",
    );
  }
  if (!Array.isArray(connection.scopes) || connection.scopes.some((scope) => typeof scope !== "string")) {
    throw new Error("Noru state connection.scopes must be an array of granted scope names");
  }
  return {
    organization: { id: organization.id, name: organization.name },
    endpoint: safeLocation(connection.endpoint, "MCP endpoint"),
    scopes: [...new Set(connection.scopes)].sort(),
  };
}

function pluginDeclaration() {
  return readJson(join(PLUGIN_ROOT, "piece.json"), "piece declaration");
}

function pluginVersion() {
  return readJson(join(PLUGIN_ROOT, ".codex-plugin", "plugin.json"), "plugin manifest").version;
}

function requiredScopes(declaration, operations) {
  const scopes = new Set(declaration.scopes?.read ?? []);
  for (const operation of operations) {
    if (operation.effect !== "skip" && operation.scope) scopes.add(operation.scope);
  }
  return [...scopes].sort();
}

function repositoryBinding(repo, provenance) {
  const root = realpathSync(repo);
  return {
    root,
    remote: safeLocation(
      gitValue(root, ["remote", "get-url", "origin"], provenance.slug),
      "repository remote",
    ),
    branch: gitValue(root, ["branch", "--show-current"], provenance.branch),
    commit_sha: gitValue(root, ["rev-parse", "HEAD"], provenance.commit_sha),
  };
}

function scopeGranted(granted, required) {
  if (granted.includes(required) || granted.includes("*")) return true;
  const family = required.split(":", 1)[0];
  return granted.includes(`${family}:*`);
}

/** Where a piece's plan lives inside the target repository. Machine-owned, not committed. */
export function planPathFor(repo, piece) {
  return join(repo, ".noru", ".cache", `${piece}.plan.json`);
}

export function sha256OfFile(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

/**
 * Anything that looks like a credential is scrubbed before it can reach stdout, a plan file or an
 * error message. A piece never handles secrets, but a REST error body can echo a header back.
 */
export function redact(value) {
  if (value === null || value === undefined) return value;
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text
    .replace(/\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}/gi, "$1<redacted>")
    .replace(/\b(noru_[A-Za-z0-9]{6,})/g, "<redacted>")
    .replace(
      /("?(?:api[_-]?key|authorization|token|secret|password)"?\s*[:=]\s*"?)([^"\s,}]{6,})/gi,
      "$1<redacted>"
    );
}

/**
 * Write the plan :push will later require. `operations` is the ordered list of writes, each already
 * carrying its idempotency decision, so a reviewer sees create-vs-update-vs-skip before anything
 * happens.
 */
export function writePlan(planPath, plan) {
  const repo = realpathSync(plan.repo ?? dirname(dirname(dirname(planPath))));
  const declaration = pluginDeclaration();
  const connection = connectionFromState(plan.state);
  const generated = new Date();
  const manifestPath = join(repo, plan.manifest);
  const payload = {
    plan_version: PLAN_VERSION,
    created_at: plan.created_at,
    generated_at: generated.toISOString(),
    expires_at: new Date(generated.getTime() + PLAN_TTL_MS).toISOString(),
    piece: plan.piece,
    piece_version: pluginVersion(),
    manifest: plan.manifest,
    manifest_sha256: plan.manifest_sha256,
    target: {
      organization_id: connection.organization.id,
      organization_name: connection.organization.name,
      mcp_endpoint: connection.endpoint,
    },
    repository: repositoryBinding(repo, plan.provenance),
    required_scopes: requiredScopes(declaration, plan.operations),
    provenance: plan.provenance,
    operations: plan.operations,
    summary: plan.summary,
  };
  const relativeManifest = relative(repo, realpathSync(manifestPath));
  if (relativeManifest.startsWith("..") || isAbsolute(relativeManifest)) {
    throw new Error(`manifest ${plan.manifest} does not resolve inside ${repo}`);
  }
  mkdirSync(dirname(planPath), { recursive: true });
  writeFileSync(planPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  return payload;
}

export function readPlan(planPath) {
  if (!existsSync(planPath)) {
    return { ok: false, reason: `no plan at ${planPath} — run the piece's :diff command first` };
  }
  let plan;
  try {
    plan = JSON.parse(readFileSync(planPath, "utf8"));
  } catch (error) {
    return { ok: false, reason: `plan at ${planPath} is not readable JSON (${error.message})` };
  }
  if (plan.plan_version !== PLAN_VERSION) {
    return { ok: false, reason: `plan at ${planPath} was written by a different version — re-run :diff` };
  }
  return { ok: true, plan };
}

/**
 * The freshness check is the actual security control: a plan is only valid for the exact manifest
 * bytes it was computed from. Edit the manifest, and the plan you reviewed no longer describes what
 * would happen.
 */
export function assertPlanFresh(plan, manifestPath) {
  if (!existsSync(manifestPath)) {
    return { ok: false, reason: `manifest ${manifestPath} no longer exists — re-run :scan and :diff` };
  }
  const current = sha256OfFile(manifestPath);
  if (current !== plan.manifest_sha256) {
    return {
      ok: false,
      reason:
        `the manifest changed after the plan was written (plan ${plan.manifest_sha256.slice(0, 12)}, ` +
        `file ${current.slice(0, 12)}) — re-run :diff and review it again before pushing`,
    };
  }

  const generated = Date.parse(plan.generated_at);
  const expires = Date.parse(plan.expires_at);
  if (!Number.isFinite(generated) || !Number.isFinite(expires) || expires <= generated) {
    return { ok: false, reason: "the plan has no valid generation and expiry window — re-run :diff" };
  }
  if (Date.now() >= expires) {
    return { ok: false, reason: `the reviewed plan expired at ${plan.expires_at} — re-run :diff` };
  }

  const repo = realpathSync(dirname(dirname(manifestPath)));
  const expectedManifest = relative(repo, realpathSync(manifestPath));
  if (expectedManifest !== plan.manifest) {
    return {
      ok: false,
      reason: `manifest path changed (plan ${plan.manifest}, current ${expectedManifest}) — re-run :diff`,
    };
  }

  let connection;
  try {
    connection = connectionFromState(
      readJson(join(repo, ".noru", ".cache", "noru-state.json"), "Noru state snapshot"),
    );
  } catch (error) {
    return { ok: false, reason: error.message };
  }

  const comparisons = [
    ["organization", plan.target?.organization_id, connection.organization.id],
    ["MCP endpoint", plan.target?.mcp_endpoint, connection.endpoint],
    ["piece version", plan.piece_version, pluginVersion()],
  ];
  const currentRepository = repositoryBinding(repo, plan.provenance ?? {});
  for (const field of ["root", "remote", "branch", "commit_sha"]) {
    comparisons.push([`repository ${field}`, plan.repository?.[field], currentRepository[field]]);
  }
  for (const [label, expected, actual] of comparisons) {
    if (!expected || expected !== actual) {
      return {
        ok: false,
        reason: `${label} changed (plan ${expected ?? "(missing)"}, current ${actual ?? "(missing)"}) — re-run :diff`,
      };
    }
  }

  const missingScopes = (plan.required_scopes ?? []).filter(
    (scope) => !scopeGranted(connection.scopes, scope),
  );
  if (missingScopes.length > 0) {
    return {
      ok: false,
      reason: `the current Noru connection lacks required scope(s): ${missingScopes.join(", ")}`,
    };
  }
  return { ok: true };
}

/** Shared flag parsing so every entrypoint honours --output=json, --quiet and --confirm alike. */
export function parseCommonArgs(argv, extra = {}) {
  const opts = {
    repo: process.cwd(),
    json: false,
    quiet: false,
    confirm: false,
    help: false,
    rest: [],
    ...extra,
  };
  for (const arg of argv) {
    if (arg.startsWith("--repo=")) opts.repo = arg.slice(7);
    else if (arg === "--output=json") opts.json = true;
    else if (arg === "--output=text") opts.json = false;
    else if (arg === "--quiet") opts.quiet = true;
    else if (arg === "--confirm") opts.confirm = true;
    else if (arg === "-h" || arg === "--help") opts.help = true;
    else opts.rest.push(arg);
  }
  return opts;
}

export function summarize(operations) {
  const summary = { create: 0, update: 0, skip: 0, total: operations.length };
  for (const op of operations) {
    if (op.effect in summary) summary[op.effect] += 1;
  }
  return summary;
}

export function renderPlanText(plan) {
  const lines = [
    `plan for ${plan.piece} (${plan.manifest})`,
    `  organization: ${plan.target.organization_name} (${plan.target.organization_id})`,
    `  endpoint: ${plan.target.mcp_endpoint}`,
    `  provenance: ${plan.provenance.slug}@${String(plan.provenance.commit_sha).slice(0, 12)} (${plan.provenance.branch})`,
    `  valid until: ${plan.expires_at}`,
    `  required scopes: ${plan.required_scopes.join(", ") || "none"}`,
    "",
  ];
  for (const op of plan.operations) {
    const mark = op.effect === "create" ? "+" : op.effect === "update" ? "~" : "=";
    lines.push(`  ${mark} ${op.effect.padEnd(6)} ${op.operation.padEnd(22)} ${op.subject}`);
    if (op.reason) lines.push(`      ${op.reason}`);
  }
  lines.push(
    "",
    `  ${plan.summary.create} to create, ${plan.summary.update} to update, ${plan.summary.skip} unchanged`,
    "",
    "  Nothing has been written. Review the above, then run the piece's :push command with --confirm.",
  );
  return lines.join("\n");
}
// --- END VENDORED plan ---
