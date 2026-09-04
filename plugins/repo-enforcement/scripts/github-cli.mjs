#!/usr/bin/env node
import { realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { apply, loadPolicy, loadState, plan, redact, verify } from "./lib/github.mjs";

const USAGE = "usage: github-cli.mjs inspect|plan|apply|verify|status --repo=<path> [--state=<fixture.json>] [--write-state=<path>] [--confirm] [--now=<ISO>] [--output=json|text] [--quiet]\n";
function args(argv) {
  const out = { command: argv[0], repo: process.cwd(), json: false, quiet: false, confirm: false };
  if (!new Set(["inspect", "plan", "apply", "verify", "status"]).has(out.command)) return { error: "missing or unknown command" };
  for (const arg of argv.slice(1)) {
    if (arg.startsWith("--repo=")) out.repo = arg.slice(7);
    else if (arg.startsWith("--state=")) out.state = arg.slice(8);
    else if (arg.startsWith("--write-state=")) out.writeState = arg.slice(14);
    else if (arg.startsWith("--now=")) out.now = arg.slice(6);
    else if (arg === "--confirm") out.confirm = true;
    else if (arg === "--output=json") out.json = true;
    else if (arg === "--output=text") out.json = false;
    else if (arg === "--quiet") out.quiet = true;
    else if (arg === "-h" || arg === "--help") return { help: true };
    else return { error: `unknown option '${arg}'` };
  }
  return out;
}
export function main(argv) {
  const opts = args(argv);
  if (opts.help) return process.stdout.write(USAGE), 0;
  if (opts.error) return process.stderr.write(`error: ${opts.error}\n${USAGE}`), 2;
  try {
    const repo = realpathSync(opts.repo);
    const state = loadState(repo, opts.state);
    const policy = loadPolicy(repo);
    let payload;
    if (opts.command === "inspect") payload = { ok: true, state };
    else if (opts.command === "plan") payload = plan(repo, state, policy, opts.now, Boolean(opts.state));
    else if (opts.command === "apply") payload = apply(repo, state, policy, opts.confirm, opts.state, opts.writeState);
    else payload = verify(state, policy.policy);
    if (opts.json) process.stdout.write(`${JSON.stringify(payload, null, opts.quiet ? 0 : 2)}\n`);
    else if (!opts.quiet) process.stdout.write(`${payload.ok ? "OK" : "FAIL"}: ${opts.command}\n`);
    return payload.ok ? 0 : 1;
  } catch (error) {
    process.stderr.write(`error: ${redact(error.message)}\n`);
    return error.usage ? 2 : 1;
  }
}
function invoked() { try { return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href; } catch { return false; } }
if (invoked()) process.exit(main(process.argv.slice(2)));
