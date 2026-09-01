#!/usr/bin/env node
// Hub check: is this machine and this repository ready to run a last-mile piece?
//
// Node built-ins only, no network. It reports whether NORU_API_KEY is *present* — never its value,
// never a prefix, never a length. Nothing here reads a credential for any purpose other than
// answering "is it set".
//
// Usage: node doctor.mjs [--repo=<path>] [--output=json|text] [--quiet]
// Exit codes: 0 = everything required is present, 1 = at least one required check failed,
//             2 = usage error.

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const USAGE = "usage: doctor.mjs [--repo=<path>] [--output=json|text] [--quiet]\n";

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

function tryRun(command, args) {
  try {
    return execFileSync(command, args, {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return null;
  }
}

function gitignoreCovers(repo, pattern) {
  const path = join(repo, ".gitignore");
  if (!existsSync(path)) return false;
  return readFileSync(path, "utf8")
    .split("\n")
    .some((line) => line.trim() === pattern);
}

export function runChecks(repo) {
  const nodeMajor = Number(process.versions.node.split(".")[0]);
  const python = tryRun("python3", ["--version"]);
  const git = tryRun("git", ["--version"]);
  const inGitRepo = tryRun("git", ["-C", repo, "rev-parse", "--is-inside-work-tree"]) === "true";

  return [
    {
      id: "node",
      required: true,
      ok: nodeMajor >= 18,
      detail: `node ${process.versions.node}`,
      hint: "Collectors and the REST upload need Node 18 or newer (global fetch, FormData, Blob).",
    },
    {
      id: "python3",
      required: true,
      ok: python !== null,
      detail: python ?? "not found on PATH",
      hint: "Validators are Python 3 standard library only. Nothing to install, but python3 must exist.",
    },
    {
      id: "git",
      required: true,
      ok: git !== null,
      detail: git ?? "not found on PATH",
      hint: "Provenance (slug, commit sha, branch) comes from git. Without it a push carries no provenance.",
    },
    {
      id: "git-repository",
      required: true,
      ok: inGitRepo,
      detail: inGitRepo ? repo : `${repo} is not inside a git work tree`,
      hint: "Manifests are meant to be committed and reviewed in a pull request.",
    },
    {
      id: "noru-dir",
      required: false,
      ok: existsSync(join(repo, ".noru")),
      detail: existsSync(join(repo, ".noru")) ? ".noru/ exists" : "no .noru/ yet",
      hint: "A piece's :scan creates it on first run.",
    },
    {
      id: "cache-ignored",
      required: false,
      ok: gitignoreCovers(repo, ".noru/.cache/"),
      detail: gitignoreCovers(repo, ".noru/.cache/")
        ? ".noru/.cache/ is gitignored"
        : ".noru/.cache/ is not in .gitignore",
      hint:
        "Commit .noru/<piece>.yml — it is the reviewable artifact. Keep .noru/.cache/ out of git: " +
        "it holds machine state and snapshots of your organization's data.",
    },
    {
      id: "noru-api-key",
      required: false,
      // Presence only. The value is never read, printed, hashed or stored.
      ok: Boolean(process.env.NORU_API_KEY),
      detail: process.env.NORU_API_KEY ? "NORU_API_KEY is set" : "NORU_API_KEY is not set",
      hint:
        "Only the evidence-push upload needs it, because file upload is REST-only. Everything else " +
        "goes over MCP, where the client owns authentication (OAuth where supported).",
    },
  ];
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

  const checks = runChecks(opts.repo);
  const failed = checks.filter((c) => c.required && !c.ok);
  const payload = { ok: failed.length === 0, repo: opts.repo, checks };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(payload, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    for (const check of checks) {
      const mark = check.ok ? "ok  " : check.required ? "FAIL" : "note";
      process.stdout.write(`  ${mark}  ${check.id.padEnd(16)} ${check.detail}\n`);
      if (!check.ok) process.stdout.write(`        ${check.hint}\n`);
    }
    process.stdout.write(
      failed.length === 0
        ? "\nReady.\n"
        : `\n${failed.length} required check(s) failed.\n`
    );
  }
  return failed.length === 0 ? 0 : 1;
}

// Reduce both sides to one form before comparing: `import.meta.url` is the realpath and is
// percent-encoded, while `process.argv[1]` is the path as it was typed — and /tmp and /var are
// symlinks on macOS, so the two differ routinely. A raw comparison is then false and the script
// exits 0 having done nothing. realpathSync throws when argv[1] is not a path at all (`node -e`,
// or an import), which is not a direct invocation either.
function invokedAsScript() {
  try {
    return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    return false;
  }
}

if (invokedAsScript()) {
  process.exit(main(process.argv.slice(2)));
}
