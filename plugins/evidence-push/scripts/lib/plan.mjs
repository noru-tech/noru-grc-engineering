// --- BEGIN VENDORED plan ---
// Canonical copy: plugins/noru/scripts/lib/plan.mjs. Every piece vendors this file verbatim at
// plugins/<piece>/scripts/lib/plan.mjs so an installed plugin is self-contained and never imports
// across plugin boundaries. scripts/check_vendored_lib.py fails CI if a copy drifts.
//
// This is the machinery behind contract requirement 5: :diff writes a plan, :push refuses to act
// without one. Node built-ins only.

import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";

export const PLAN_VERSION = 1;

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
  const payload = {
    plan_version: PLAN_VERSION,
    created_at: plan.created_at,
    piece: plan.piece,
    manifest: plan.manifest,
    manifest_sha256: plan.manifest_sha256,
    provenance: plan.provenance,
    operations: plan.operations,
    summary: plan.summary,
  };
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
    `  provenance: ${plan.provenance.slug}@${String(plan.provenance.commit_sha).slice(0, 12)} (${plan.provenance.branch})`,
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
