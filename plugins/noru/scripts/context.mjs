#!/usr/bin/env node
// Hub context: the repository facts every piece reuses, gathered once.
//
// Node built-ins only, no network. Deterministic: same repository state, same output. It emits no
// timestamp for that reason — a piece that needs one records when it called Noru, not when this ran.
//
// Usage: node context.mjs [--repo=<path>] [--output=json|text] [--quiet]
// Exit codes: 0 = ok, 2 = usage / not a directory.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { basename, join } from "node:path";

const USAGE = "usage: context.mjs [--repo=<path>] [--output=json|text] [--quiet]\n";

function parseArgs(argv) {
  const opts = { repo: process.cwd(), json: false, quiet: false };
  for (const arg of argv) {
    if (arg.startsWith("--repo=")) opts.repo = arg.slice(7);
    else if (arg === "--output=json") opts.json = true;
    else if (arg === "--output=text") opts.json = false;
    else if (arg === "--quiet") opts.quiet = true;
    else if (arg === "-h" || arg === "--help") return { help: true };
    else return { error: `unknown option '${arg}'` };
  }
  return opts;
}

function gitValue(repo, args, fallback) {
  try {
    return (
      execFileSync("git", ["-C", repo, ...args], {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      }).trim() || fallback
    );
  } catch {
    return fallback;
  }
}

export function provenance(repo) {
  const remote = gitValue(repo, ["remote", "get-url", "origin"], "");
  let slug = basename(repo) || "repository";
  const match = remote.match(/[:/]([^/:]+\/[^/]+?)(?:\.git)?$/);
  if (match) slug = match[1];
  const dirty = gitValue(repo, ["status", "--porcelain"], "");
  return {
    slug,
    commit_sha: gitValue(repo, ["rev-parse", "HEAD"], "unknown"),
    branch: gitValue(repo, ["rev-parse", "--abbrev-ref", "HEAD"], "unknown"),
    // A push from a dirty tree records a commit sha that does not describe what was scanned.
    working_tree_clean: dirty === "",
  };
}

export function manifests(repo) {
  const dir = join(repo, ".noru");
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .filter((name) => name.endsWith(".yml") || name.endsWith(".yaml"))
    .sort()
    .map((name) => {
      const full = join(dir, name);
      const bytes = readFileSync(full);
      return {
        piece: name.replace(/\.ya?ml$/, ""),
        path: `.noru/${name}`,
        size_bytes: statSync(full).size,
        sha256: createHash("sha256").update(bytes).digest("hex"),
      };
    });
}

function main(argv) {
  const opts = parseArgs(argv);
  if (opts.help) {
    process.stdout.write(USAGE);
    return 0;
  }
  if (opts.error) {
    process.stderr.write(`error: ${opts.error}\n${USAGE}`);
    return 2;
  }
  if (!existsSync(opts.repo)) {
    process.stderr.write(`error: no such directory: ${opts.repo}\n`);
    return 2;
  }

  const payload = {
    repo: opts.repo,
    provenance: provenance(opts.repo),
    manifests: manifests(opts.repo),
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(payload, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    const p = payload.provenance;
    process.stdout.write(
      [
        `slug:   ${p.slug}`,
        `commit: ${p.commit_sha}`,
        `branch: ${p.branch}`,
        `tree:   ${p.working_tree_clean ? "clean" : "DIRTY — a push would record a commit that does not describe what was scanned"}`,
        payload.manifests.length > 0 ? "manifests:" : "manifests: (none)",
        ...payload.manifests.map((m) => `  ${m.path}  ${m.sha256.slice(0, 12)}  ${m.size_bytes}B`),
      ].join("\n") + "\n"
    );
  }
  return 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  process.exit(main(process.argv.slice(2)));
}
