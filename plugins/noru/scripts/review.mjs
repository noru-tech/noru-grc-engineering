#!/usr/bin/env node
// Select the last-mile pieces relevant to one branch diff. Local and read-only: this script makes
// no Noru call, writes no manifest and runs no piece. The /noru:review command orchestrates the
// selected skills after showing this selection to the user.

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const ROUTING_PATH = join(ROOT, "references", "routing.json");
const SIGNALS_PATH = join(ROOT, "references", "review-signals.json");
const USAGE =
  "usage: review.mjs [--repo=<path>] [--base-ref=<ref>] [--pieces=a,b] " +
  "[--available-pieces=a,b] [--include-untracked] [--with-diff|--run-diff] " +
  "[--output=json|text] [--quiet]\n";

function parseArgs(argv) {
  const opts = {
    repo: process.cwd(),
    baseRef: "origin/main",
    explicit: null,
    available: null,
    includeUntracked: false,
    withDiff: false,
    json: false,
    quiet: false,
  };
  for (const arg of argv) {
    if (arg.startsWith("--repo=")) opts.repo = arg.slice(7);
    else if (arg.startsWith("--base-ref=")) opts.baseRef = arg.slice(11);
    else if (arg.startsWith("--pieces=")) {
      opts.explicit = arg.slice(9).split(",").map((value) => value.trim()).filter(Boolean);
    } else if (arg.startsWith("--available-pieces=")) {
      opts.available = arg.slice(19).split(",").map((value) => value.trim()).filter(Boolean);
    } else if (arg === "--include-untracked") opts.includeUntracked = true;
    else if (arg === "--with-diff" || arg === "--run-diff") opts.withDiff = true;
    else if (arg === "--output=json") opts.json = true;
    else if (arg === "--output=text") opts.json = false;
    else if (arg === "--quiet") opts.quiet = true;
    else if (arg === "-h" || arg === "--help") return { help: true };
    else return { error: `unknown option '${arg}'` };
  }
  return opts;
}

function git(repo, args) {
  return execFileSync("git", ["-C", repo, ...args], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

function trackedChanges(repo, mergeBase) {
  const output = execFileSync(
    "git",
    ["-C", repo, "diff", "--name-status", "--find-renames", "-z", mergeBase],
    { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
  );
  const tokens = output.split("\0").filter(Boolean);
  const files = [];
  for (let index = 0; index < tokens.length;) {
    const status = tokens[index++];
    if (status.startsWith("R") || status.startsWith("C")) {
      const previousPath = tokens[index++];
      const path = tokens[index++];
      files.push({ status, path, previous_path: previousPath, tracked: true });
    } else {
      files.push({ status, path: tokens[index++], tracked: true });
    }
  }
  return files;
}

function untrackedFiles(repo) {
  const output = execFileSync(
    "git",
    ["-C", repo, "ls-files", "--others", "--exclude-standard", "-z"],
    { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
  );
  return output.split("\0").filter(Boolean).map((path) => ({ status: "?", path, tracked: false }));
}

function contentAt(repo, file) {
  if (file.status === "D") return null;
  const path = join(repo, file.path);
  if (!existsSync(path)) return null;
  try {
    const value = readFileSync(path, "utf8");
    return value.length <= 1024 * 1024 ? value : null;
  } catch {
    return null;
  }
}

function firstContentMatch(content, pattern) {
  const regex = new RegExp(pattern, "i");
  const lines = content.split("\n");
  const index = lines.findIndex((line) => regex.test(line));
  return index < 0 ? null : index + 1;
}

function reasonsFor(repo, files, rules) {
  const reasons = [];
  const seen = new Set();
  for (const file of files) {
    for (const rule of rules.paths ?? []) {
      if (new RegExp(rule.pattern, "i").test(file.path)) {
        const value = `${rule.reason}: ${file.path}`;
        if (!seen.has(value)) reasons.push(value), seen.add(value);
      }
    }
    const content = contentAt(repo, file);
    if (content === null) continue;
    for (const rule of rules.content ?? []) {
      const line = firstContentMatch(content, rule.pattern);
      if (line !== null) {
        const value = `${rule.reason}: ${file.path}:${line}`;
        if (!seen.has(value)) reasons.push(value), seen.add(value);
      }
    }
  }
  return reasons.slice(0, 8);
}

export function reviewRepository(opts) {
  const repo = realpathSync(opts.repo);
  if (git(repo, ["rev-parse", "--is-inside-work-tree"]) !== "true") {
    throw new Error(`${repo} is not a git work tree`);
  }
  const mergeBase = git(repo, ["merge-base", opts.baseRef, "HEAD"]);
  const head = git(repo, ["rev-parse", "HEAD"]);
  const branch = git(repo, ["branch", "--show-current"]);
  const tracked = trackedChanges(repo, mergeBase);
  const untracked = untrackedFiles(repo);
  const considered = opts.includeUntracked ? [...tracked, ...untracked] : tracked;
  const catalogue = JSON.parse(readFileSync(ROUTING_PATH, "utf8")).pieces;
  const signals = JSON.parse(readFileSync(SIGNALS_PATH, "utf8")).pieces;
  const names = new Set(catalogue.map((piece) => piece.name));
  const unknown = (opts.explicit ?? []).filter((name) => !names.has(name));
  if (unknown.length > 0) throw new Error(`unknown piece(s): ${unknown.join(", ")}`);
  const unknownAvailable = (opts.available ?? []).filter((name) => !names.has(name));
  if (unknownAvailable.length > 0) {
    throw new Error(`unknown available piece(s): ${unknownAvailable.join(", ")}`);
  }
  const available = opts.available == null ? null : new Set(opts.available);

  const pieces = catalogue.map((piece) => {
    const installed = available === null ? null : available.has(piece.name);
    if (opts.explicit !== null) {
      const selected = opts.explicit.includes(piece.name);
      return {
        name: piece.name,
        disposition: selected ? "selected" : "skipped",
        reasons: [selected ? "explicitly requested" : "not in the explicit piece selection"],
        installed,
        run_state:
          selected && installed === false ? "unavailable" : selected ? "ready" : "not_selected",
        availability_reason:
          installed === false
            ? `the ${piece.name} plugin is not present in the host's available skills`
            : installed === true
              ? `the ${piece.name} plugin is available independently`
              : "installed-plugin availability was not supplied to the selector",
      };
    }
    const reasons = reasonsFor(repo, considered, signals[piece.name] ?? {});
    const selected = reasons.length > 0;
    return {
      name: piece.name,
      disposition: selected ? "selected" : "skipped",
      reasons: selected ? reasons : ["no branch-change signal matched"],
      installed,
      run_state:
        selected && installed === false ? "unavailable" : selected ? "ready" : "not_selected",
      availability_reason:
        installed === false
          ? `the ${piece.name} plugin is not present in the host's available skills`
          : installed === true
            ? `the ${piece.name} plugin is available independently`
            : "installed-plugin availability was not supplied to the selector",
    };
  });

  return {
    repository: repo,
    branch,
    head,
    base_ref: opts.baseRef,
    merge_base: mergeBase,
    include_untracked: opts.includeUntracked,
    requested_diff: opts.withDiff,
    clean: considered.length === 0,
    changed_files: considered,
    excluded_untracked: opts.includeUntracked ? [] : untracked.map((file) => file.path),
    available_pieces: opts.available ?? null,
    pieces,
  };
}

function render(report) {
  const lines = [
    `Noru branch review: ${report.branch || "(detached)"} against ${report.base_ref}`,
    `  ${report.changed_files.length} changed file(s); ${report.excluded_untracked.length} untracked file(s) excluded`,
    "",
  ];
  if (report.clean && !report.pieces.some((piece) => piece.disposition === "selected")) {
    lines.push("No considered branch changes. No pieces selected.", "");
  }
  for (const piece of report.pieces) {
    const availability = piece.run_state === "unavailable" ? " (not installed)" : "";
    lines.push(
      `  ${piece.disposition === "selected" ? "+" : "-"} ${piece.name}: ${piece.disposition}${availability}`,
    );
    for (const reason of piece.reasons) lines.push(`      ${reason}`);
    if (piece.run_state === "unavailable") lines.push(`      ${piece.availability_reason}`);
  }
  if (report.excluded_untracked.length > 0) {
    lines.push("", "Untracked files were reported but not used for routing; stage them or pass --include-untracked:");
    for (const path of report.excluded_untracked) lines.push(`  ? ${path}`);
  }
  lines.push("", "Nothing was written to Noru.");
  return lines.join("\n");
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
  try {
    const report = reviewRepository(opts);
    if (opts.json) process.stdout.write(`${JSON.stringify(report, null, opts.quiet ? 0 : 2)}\n`);
    else if (!opts.quiet) process.stdout.write(`${render(report)}\n`);
    return 0;
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    return 2;
  }
}

function invokedAsScript() {
  try {
    return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    return false;
  }
}

if (invokedAsScript()) process.exit(main(process.argv.slice(2)));
