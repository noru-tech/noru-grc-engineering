#!/usr/bin/env node
const { spawnSync } = require("node:child_process");
const { appendFileSync, mkdirSync } = require("node:fs");
const { dirname, resolve } = require("node:path");

const actionRoot = resolve(__dirname, "..");
const suiteRoot = resolve(actionRoot, "..", "..");
const repo = resolve(process.env.GITHUB_WORKSPACE || process.cwd(), process.env.INPUT_REPO || ".");
const policy = resolve(repo, process.env.INPUT_POLICY || ".noru/enforcement.yml");
const report = process.env.INPUT_REPORT_PATH
  ? resolve(repo, process.env.INPUT_REPORT_PATH)
  : resolve(process.env.RUNNER_TEMP || "/tmp", "noru-grc-enforcement.json");
const asOf = process.env["INPUT_AS-OF"] || "";

function workflowLine(file, value) {
  if (file) appendFileSync(file, `${value}\n`, "utf8");
}
function commandEscape(value) {
  return String(value).replace(/%/g, "%25").replace(/\r/g, "%0D").replace(/\n/g, "%0A");
}

if (!/^\d{4}-\d{2}-\d{2}$/.test(asOf)) {
  process.stderr.write("::error::as-of is required and must be YYYY-MM-DD\n");
  process.exit(2);
}

mkdirSync(dirname(report), { recursive: true });
const result = spawnSync(
  "python3",
  [
    resolve(suiteRoot, "plugins", "repo-enforcement", "scripts", "enforce.py"),
    "validate",
    `--repo=${repo}`,
    `--suite-root=${suiteRoot}`,
    `--registry=${resolve(actionRoot, "registry.json")}`,
    `--policy=${policy}`,
    `--as-of=${asOf}`,
    "--output=json",
    "--quiet",
  ],
  { encoding: "utf8", env: Object.fromEntries(Object.entries(process.env).filter(([key]) => !/(?:TOKEN|SECRET|PASSWORD|API_KEY|AUTHORIZATION)/i.test(key))) },
);

let payload;
try {
  payload = JSON.parse(result.stdout || "");
} catch {
  process.stderr.write(`::error::repository enforcement did not return JSON: ${commandEscape(result.stderr || result.stdout || "no output")}\n`);
  process.exit(result.status || 2);
}
require("node:fs").writeFileSync(report, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
for (const violation of payload.new_violations || []) {
  process.stderr.write(`::error title=Noru GRC ${commandEscape(violation.piece)}/${commandEscape(violation.rule)}::${commandEscape(violation.subject)} — ${commandEscape(violation.message)}\n`);
}
for (const entry of payload.expired_exceptions || []) {
  process.stderr.write(`::error title=Noru GRC expired baseline::${commandEscape(entry.subject || entry.fingerprint)}\n`);
}
for (const entry of payload.stale_baseline_entries || []) {
  process.stderr.write(`::error title=Noru GRC stale baseline::${commandEscape(entry.subject || entry.fingerprint)}\n`);
}

const summary = [
  "## Noru GRC repository enforcement",
  "",
  `**${payload.ok ? "Pass" : "Fail"}**`,
  "",
  `- New violations: ${(payload.new_violations || []).length}`,
  `- Baselined violations: ${(payload.baselined_violations || []).length}`,
  `- Expired exceptions: ${(payload.expired_exceptions || []).length}`,
  `- Stale baseline entries: ${(payload.stale_baseline_entries || []).length}`,
  `- Report: \`${report}\``,
  "",
].join("\n");
workflowLine(process.env.GITHUB_STEP_SUMMARY, summary);
workflowLine(process.env.GITHUB_OUTPUT, `report=${report}`);
workflowLine(process.env.GITHUB_OUTPUT, `new-violations=${(payload.new_violations || []).length}`);
workflowLine(process.env.GITHUB_OUTPUT, `baselined-violations=${(payload.baselined_violations || []).length}`);
process.stdout.write(`${JSON.stringify({ ok: payload.ok, report })}\n`);
process.exit(payload.ok ? 0 : 1);
