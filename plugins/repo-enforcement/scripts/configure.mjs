#!/usr/bin/env node
// Plan and apply repository-local enforcement files. No GitHub administration occurs here.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const VERSION = "0.6.0";
const PLAN_REL = ".noru/.cache/repo-enforcement-files.json";
const ALLOWED_FILES = new Set([
  ".noru/enforcement.yml", ".noru/enforcement-baseline.json",
  ".github/workflows/noru-grc.yml", ".github/CODEOWNERS", ".gitignore",
]);
const USAGE = `usage:
  configure.mjs inspect --repo=<path> [--output=json|text] [--quiet]
  configure.mjs plan --repo=<path> --action-sha=<40 hex> --grc-reviewers=@org/team
    --privacy-reviewers=@org/team --security-reviewers=@org/team --break-glass=@org/team
    [--mode=strict|ratchet] [--scope=repository|organization] [--maximum-days=N]
    [--enforcement=active|evaluate] [--now=<ISO timestamp>] [--output=json|text] [--quiet]
  configure.mjs apply --repo=<path> --confirm [--output=json|text] [--quiet]
`;

function parseArgs(argv) {
  const opts = {
    command: argv[0], repo: process.cwd(), mode: "ratchet", scope: "repository",
    maximumDays: 30, enforcement: "active", json: false, quiet: false, confirm: false,
  };
  if (!new Set(["inspect", "plan", "apply"]).has(opts.command)) return { error: "missing or unknown command" };
  for (const arg of argv.slice(1)) {
    if (arg.startsWith("--repo=")) opts.repo = arg.slice(7);
    else if (arg.startsWith("--action-sha=")) opts.actionSha = arg.slice(13);
    else if (arg.startsWith("--grc-reviewers=")) opts.grc = arg.slice(16);
    else if (arg.startsWith("--privacy-reviewers=")) opts.privacy = arg.slice(20);
    else if (arg.startsWith("--security-reviewers=")) opts.security = arg.slice(21);
    else if (arg.startsWith("--break-glass=")) opts.breakGlass = arg.slice(14);
    else if (arg.startsWith("--mode=")) opts.mode = arg.slice(7);
    else if (arg.startsWith("--scope=")) opts.scope = arg.slice(8);
    else if (arg.startsWith("--maximum-days=")) opts.maximumDays = Number(arg.slice(15));
    else if (arg.startsWith("--enforcement=")) opts.enforcement = arg.slice(14);
    else if (arg.startsWith("--now=")) opts.now = arg.slice(6);
    else if (arg === "--confirm") opts.confirm = true;
    else if (arg === "--output=json") opts.json = true;
    else if (arg === "--output=text") opts.json = false;
    else if (arg === "--quiet") opts.quiet = true;
    else if (arg === "-h" || arg === "--help") return { help: true };
    else return { error: `unknown option '${arg}'` };
  }
  return opts;
}

function sha(value) { return createHash("sha256").update(value).digest("hex"); }
function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, child]) => [key, stable(child)]));
  return value;
}
function objectSha(value) { return sha(JSON.stringify(stable(value))); }
function fileText(path) { return existsSync(path) ? readFileSync(path, "utf8") : null; }
function fileSha(value) { return value === null ? null : sha(value); }
function git(repo, args) {
  return execFileSync("git", ["-C", repo, ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}
function isoNow(value) {
  const date = value ? new Date(value) : new Date();
  if (Number.isNaN(date.valueOf())) throw new Error("--now must be an ISO timestamp");
  return date;
}
function team(value, name) {
  if (!/^@[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(value ?? "")) {
    throw new Error(`${name} must name a real team as @org/team`);
  }
  return value;
}
function replaceManagedBlock(existing, block) {
  const begin = "# BEGIN NORU GRC ENFORCEMENT";
  const end = "# END NORU GRC ENFORCEMENT";
  const text = existing ?? "";
  const start = text.indexOf(begin);
  const finish = text.indexOf(end);
  let kept = text.trimEnd();
  if (start >= 0 && finish >= start) {
    kept = (text.slice(0, start) + text.slice(finish + end.length)).trimEnd();
  }
  return `${kept}${kept ? "\n\n" : ""}${block.trim()}\n`;
}

function policyObject(opts) {
  return {
    version: 1,
    adoption: opts.mode === "ratchet" ? { mode: opts.mode, baseline: ".noru/enforcement-baseline.json" } : { mode: opts.mode },
    pieces: {
      "ai-inventory": { required: true },
      "iac-scan": { required: true },
      "privacy-datamap": { required: true, fail_on: ["drift", "needs_review", "missing_interpretation", "expired", "coverage", "tooling"] },
    },
    reviews: {
      minimum_approvals: 1,
      dismiss_stale_approvals: true,
      require_last_push_approval: true,
      require_code_owner_review: true,
      require_thread_resolution: true,
    },
    ownership: {
      grc_reviewers: opts.grc,
      privacy_reviewers: opts.privacy,
      security_reviewers: opts.security,
      break_glass: opts.breakGlass,
    },
    exceptions: { maximum_days: opts.maximumDays, require_named_owner: true, require_rationale: true },
    github: {
      target: "default_branch",
      scope: opts.scope,
      ruleset_name: "Noru GRC — governed development",
      required_check: "Noru GRC / validate",
      action_sha: opts.actionSha,
      enforcement: opts.enforcement,
    },
  };
}

function policyText(opts) {
  return `version: 1

adoption:
  mode: ${opts.mode}
${opts.mode === "ratchet" ? "  baseline: .noru/enforcement-baseline.json\n" : ""}
pieces:
  ai-inventory:
    required: true
  iac-scan:
    required: true
  privacy-datamap:
    required: true
    fail_on: [drift, needs_review, missing_interpretation, expired, coverage, tooling]

reviews:
  minimum_approvals: 1
  dismiss_stale_approvals: true
  require_last_push_approval: true
  require_code_owner_review: true
  require_thread_resolution: true

ownership:
  grc_reviewers: "${opts.grc}"
  privacy_reviewers: "${opts.privacy}"
  security_reviewers: "${opts.security}"
  break_glass: "${opts.breakGlass}"

exceptions:
  maximum_days: ${opts.maximumDays}
  require_named_owner: true
  require_rationale: true

github:
  target: default_branch
  scope: ${opts.scope}
  ruleset_name: "Noru GRC — governed development"
  required_check: "Noru GRC / validate"
  action_sha: ${opts.actionSha}
  enforcement: ${opts.enforcement}
`;
}

function desiredFiles(repo, opts) {
  const policy = policyObject(opts);
  const workflow = readFileSync(join(ROOT, "assets", "github", "noru-grc.yml"), "utf8")
    .replaceAll("__NORU_ENFORCE_SHA__", opts.actionSha);
  const codeownersBlock = readFileSync(join(ROOT, "assets", "CODEOWNERS.template"), "utf8")
    .replaceAll("__GRC_REVIEWERS__", opts.grc)
    .replaceAll("__SECURITY_REVIEWERS__", opts.security);
  const codeownersPath = join(repo, ".github", "CODEOWNERS");
  const ignored = fileText(join(repo, ".gitignore")) ?? "";
  const gitignore = ignored.split("\n").some((line) => line.trim() === ".noru/.cache/")
    ? ignored
    : `${ignored.trimEnd()}${ignored.trim() ? "\n" : ""}.noru/.cache/\n`;
  const files = {
    ".noru/enforcement.yml": policyText(opts),
    ".github/workflows/noru-grc.yml": workflow,
    ".github/CODEOWNERS": replaceManagedBlock(fileText(codeownersPath), codeownersBlock),
    ".gitignore": gitignore,
  };
  if (opts.mode === "ratchet" && !existsSync(join(repo, ".noru", "enforcement-baseline.json"))) {
    files[".noru/enforcement-baseline.json"] = `${JSON.stringify({ version: 1, policy_digest: objectSha(policy), violations: [] }, null, 2)}\n`;
  }
  return files;
}

function inspect(repo) {
  const tracked = (path) => existsSync(join(repo, path));
  return {
    ok: true,
    repository: { root: repo, commit_sha: git(repo, ["rev-parse", "HEAD"]), branch: git(repo, ["branch", "--show-current"]) },
    files: {
      policy: tracked(".noru/enforcement.yml"), baseline: tracked(".noru/enforcement-baseline.json"),
      workflow: tracked(".github/workflows/noru-grc.yml"), codeowners: tracked(".github/CODEOWNERS"),
    },
  };
}

function makePlan(repo, opts) {
  if (!/^[0-9a-f]{40}$/.test(opts.actionSha ?? "")) throw new Error("--action-sha must be a full 40-character commit SHA");
  for (const [value, name] of [[opts.grc, "--grc-reviewers"], [opts.privacy, "--privacy-reviewers"], [opts.security, "--security-reviewers"], [opts.breakGlass, "--break-glass"]]) team(value, name);
  if (!new Set(["strict", "ratchet"]).has(opts.mode)) throw new Error("--mode must be strict or ratchet");
  if (!new Set(["repository", "organization"]).has(opts.scope)) throw new Error("--scope must be repository or organization");
  if (!new Set(["active", "evaluate"]).has(opts.enforcement)) throw new Error("--enforcement must be active or evaluate");
  if (!Number.isInteger(opts.maximumDays) || opts.maximumDays < 1 || opts.maximumDays > 365) throw new Error("--maximum-days must be between 1 and 365");
  const now = isoNow(opts.now);
  const desired = desiredFiles(repo, opts);
  const operations = Object.entries(desired).sort().map(([path, content]) => {
    const before = fileText(join(repo, path));
    return { path, action: before === null ? "create" : before === content ? "skip" : "update", before_sha256: fileSha(before), after_sha256: sha(content), content };
  });
  const plan = {
    version: 1, plugin_version: VERSION, repository: { root: repo, commit_sha: git(repo, ["rev-parse", "HEAD"]) },
    created_at: now.toISOString(), expires_at: new Date(now.valueOf() + 60 * 60 * 1000).toISOString(),
    operations,
  };
  plan.plan_digest = sha(JSON.stringify(plan));
  const path = join(repo, PLAN_REL);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `${JSON.stringify(plan, null, 2)}\n`, "utf8");
  return { ok: true, plan: PLAN_REL, counts: Object.fromEntries(["create", "update", "skip"].map((kind) => [kind, operations.filter((row) => row.action === kind).length])), operations: operations.map(({ content, ...row }) => row) };
}

function applyPlan(repo, opts) {
  if (!opts.confirm) throw Object.assign(new Error("refusing to apply repository files without --confirm"), { usage: true });
  const path = join(repo, PLAN_REL);
  if (!existsSync(path)) throw new Error(`no local file plan at ${PLAN_REL}; run configure.mjs plan`);
  const plan = JSON.parse(readFileSync(path, "utf8"));
  const { plan_digest: recordedDigest, ...unsignedPlan } = plan;
  if (recordedDigest !== sha(JSON.stringify(unsignedPlan))) throw new Error("local file plan digest is invalid; re-plan");
  if (plan.plugin_version !== VERSION) throw new Error("local file plan was created by another plugin version; re-plan");
  const paths = plan.operations.map((operation) => operation.path);
  if (new Set(paths).size !== paths.length || paths.some((entry) => !ALLOWED_FILES.has(entry))) throw new Error("local file plan contains an unexpected or duplicate path; re-plan");
  for (const operation of plan.operations) {
    if (operation.after_sha256 !== sha(operation.content)) throw new Error(`${operation.path} content does not match its planned digest; re-plan`);
    if (!new Set(["create", "update", "skip"]).has(operation.action)) throw new Error(`${operation.path} has an invalid planned action; re-plan`);
  }
  if (plan.repository.root !== repo || plan.repository.commit_sha !== git(repo, ["rev-parse", "HEAD"])) throw new Error("repository identity changed after planning; re-plan");
  if (new Date(plan.expires_at) <= new Date()) throw new Error("local file plan expired; re-plan");
  for (const operation of plan.operations) {
    if (fileSha(fileText(join(repo, operation.path))) !== operation.before_sha256) throw new Error(`${operation.path} changed after planning; re-plan`);
  }
  for (const operation of plan.operations) {
    if (operation.action === "skip") continue;
    const target = join(repo, operation.path);
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, operation.content, "utf8");
  }
  return { ok: true, verified: plan.operations.every((row) => fileSha(fileText(join(repo, row.path))) === row.after_sha256), counts: Object.fromEntries(["create", "update", "skip"].map((kind) => [kind, plan.operations.filter((row) => row.action === kind).length])) };
}

export function main(argv) {
  const opts = parseArgs(argv);
  if (opts.help) return process.stdout.write(USAGE), 0;
  if (opts.error) return process.stderr.write(`error: ${opts.error}\n${USAGE}`), 2;
  try {
    const repo = realpathSync(opts.repo);
    const payload = opts.command === "inspect" ? inspect(repo) : opts.command === "plan" ? makePlan(repo, opts) : applyPlan(repo, opts);
    if (opts.json) process.stdout.write(`${JSON.stringify(payload, null, opts.quiet ? 0 : 2)}\n`);
    else if (!opts.quiet) process.stdout.write(`${payload.ok ? "OK" : "FAIL"}: ${JSON.stringify(payload.counts ?? payload.files)}\n`);
    return payload.ok ? 0 : 1;
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    return error.usage ? 2 : 1;
  }
}

function invoked() { try { return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href; } catch { return false; } }
if (invoked()) process.exit(main(process.argv.slice(2)));
