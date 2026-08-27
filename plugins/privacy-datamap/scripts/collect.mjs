#!/usr/bin/env node
// Deterministic, offline collector for the privacy-datamap piece (contract requirement 2).
//
// Node built-ins only. Opens no socket. Same repository state in, byte-identical derived output —
// scripts/contract_test.py runs this twice and diffs the result, so a timestamp or an unsorted
// directory listing anywhere in here will fail the build.
//
// TODO: replace collectFacts() with what this piece actually collects.
//
// Usage: node collect.mjs [--repo=<path>] [--check] [--output=json|text] [--quiet]
// Exit codes: 0 ok, 1 drift against the manifest (--check), 2 usage/IO error.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, join, relative, sep } from "node:path";

export const PIECE = "privacy-datamap";
export const VERSION = "0.1.0";
const GENERATED_BY = `${PIECE}@${VERSION}`;

const SKIP_DIRS = new Set([
  ".git", "node_modules", "dist", "build", "out", ".next", ".turbo", "coverage",
  "vendor", "target", ".venv", "venv", "__pycache__", ".noru",
]);
const USAGE =
  "usage: collect.mjs [--repo=<path>] [--check] [--output=json|text] [--quiet]\n";

function parseArgs(argv) {
  const opts = { repo: process.cwd(), check: false, json: false, quiet: false };
  for (const arg of argv) {
    if (arg.startsWith("--repo=")) opts.repo = arg.slice(7);
    else if (arg === "--check") opts.check = true;
    else if (arg === "--output=json") opts.json = true;
    else if (arg === "--output=text") opts.json = false;
    else if (arg === "--quiet") opts.quiet = true;
    else if (arg === "-h" || arg === "--help") return { help: true };
    else return { error: `unknown option '${arg}'` };
  }
  return opts;
}

function walk(root) {
  const out = [];
  const stack = [root];
  while (stack.length > 0) {
    const dir = stack.pop();
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        if (!SKIP_DIRS.has(entry.name)) stack.push(full);
      } else if (entry.isFile()) {
        out.push(relative(root, full).split(sep).join("/"));
      }
    }
  }
  // Sorted, so the result never depends on directory iteration order. Do not remove.
  return out.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
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

export function repoProvenance(repo) {
  const remote = gitValue(repo, ["remote", "get-url", "origin"], "");
  let slug = basename(repo) || "repository";
  const match = remote.match(/[:/]([^/:]+\/[^/]+?)(?:\.git)?$/);
  if (match) slug = match[1];
  return {
    slug,
    commit_sha: gitValue(repo, ["rev-parse", "HEAD"], "unknown"),
    branch: gitValue(repo, ["rev-parse", "--abbrev-ref", "HEAD"], "unknown"),
    generated_by: GENERATED_BY,
  };
}

// TODO: this is the piece. Everything above and below is plumbing.
export function collectFacts(repo) {
  const files = walk(repo);
  return {
    piece: PIECE,
    generated_by: GENERATED_BY,
    files_scanned: files.length,
    // TODO: replace with the facts this piece can stand behind, each carrying file:line.
    findings: [],
  };
}

export function digestOf(derived) {
  return createHash("sha256").update(JSON.stringify(derived, null, 0)).digest("hex");
}

const PLAIN_SAFE = /^[A-Za-z0-9_][A-Za-z0-9 _./@-]*$/;

function yamlScalar(value) {
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  const s = String(value);
  if (s === "" || !PLAIN_SAFE.test(s) || /^(true|false|null|yes|no|on|off)$/i.test(s)) {
    return `"${s.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n")}"`;
  }
  return s;
}

export function toYaml(value, indent = 0) {
  const pad = " ".repeat(indent);
  if (Array.isArray(value)) {
    if (value.length === 0) return `${pad}[]\n`;
    return value
      .map((item) => {
        if (item !== null && typeof item === "object") {
          return `${pad}- ${toYaml(item, indent + 2).slice(indent + 2)}`;
        }
        return `${pad}- ${yamlScalar(item)}\n`;
      })
      .join("");
  }
  if (value !== null && typeof value === "object") {
    const keys = Object.keys(value);
    if (keys.length === 0) return `${pad}{}\n`;
    return keys
      .map((key) => {
        const child = value[key];
        if (Array.isArray(child)) {
          if (child.length === 0) return `${pad}${key}: []\n`;
          return `${pad}${key}:\n${toYaml(child, indent + 2)}`;
        }
        if (child !== null && typeof child === "object") {
          return `${pad}${key}:\n${toYaml(child, indent + 2)}`;
        }
        return `${pad}${key}: ${yamlScalar(child)}\n`;
      })
      .join("");
  }
  return `${pad}${yamlScalar(value)}\n`;
}

export function buildSkeleton(derived, provenance) {
  return {
    version: VERSION,
    piece: PIECE,
    source: { ...provenance, derived_digest: digestOf(derived) },
    // TODO: turn derived.findings into items a human can review and sign for.
    items: [],
  };
}

const HEADER = `# .noru/privacy-datamap.yml — generated by ${GENERATED_BY}
#
# Every TODO below is a decision a person has to make and sign for. The validator enforces:
#   * refs[] must cite the repository lines (file:line) that produced each claim
#   * interpretation.owner must be a person, not a team alias
#   * needs_review: true blocks the push
`;

function readManifestDigest(manifestPath) {
  if (!existsSync(manifestPath)) return null;
  const m = readFileSync(manifestPath, "utf8").match(/derived_digest:\s*"?([0-9a-f]{64})"?/);
  return m ? m[1] : "";
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

  const derived = collectFacts(opts.repo);
  const provenance = repoProvenance(opts.repo);
  const digest = digestOf(derived);
  const manifestPath = join(opts.repo, ".noru", "privacy-datamap.yml");
  const derivedPath = join(opts.repo, ".noru", ".cache", "privacy-datamap.derived.json");

  let wroteSkeleton = false;
  let drift = false;
  try {
    mkdirSync(join(opts.repo, ".noru", ".cache"), { recursive: true });
    writeFileSync(derivedPath, `${JSON.stringify(derived, null, 2)}\n`, "utf8");
    const existing = readManifestDigest(manifestPath);
    if (existing === null) {
      if (opts.check) drift = true;
      else {
        writeFileSync(manifestPath, HEADER + toYaml(buildSkeleton(derived, provenance)), "utf8");
        wroteSkeleton = true;
      }
    } else if (existing !== digest) {
      drift = true;
    }
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    return 2;
  }

  const summary = {
    piece: PIECE,
    ok: !(opts.check && drift),
    repo: opts.repo,
    manifest: relative(opts.repo, manifestPath).split(sep).join("/"),
    derived_facts: relative(opts.repo, derivedPath).split(sep).join("/"),
    derived_digest: digest,
    drift,
    wrote_skeleton: wroteSkeleton,
    provenance,
    counts: { files_scanned: derived.files_scanned, findings: derived.findings.length },
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    process.stdout.write(
      [
        `scanned ${derived.files_scanned} file(s) in ${opts.repo}`,
        `findings: ${derived.findings.length}`,
        `derived facts: ${summary.derived_facts}`,
        wroteSkeleton ? `wrote skeleton: ${summary.manifest}` : "",
        drift ? "DRIFT: the manifest does not match the repository as it is now" : "",
      ]
        .filter(Boolean)
        .join("\n") + "\n"
    );
  }
  return opts.check && drift ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  process.exit(main(process.argv.slice(2)));
}
