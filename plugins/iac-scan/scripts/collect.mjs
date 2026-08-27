#!/usr/bin/env node
// Deterministic, offline collector for the iac-scan piece (contract requirement 2).
//
// Node built-ins only. Opens no socket. It reads two local things:
//
//   .noru/.cache/iac-queue.json   what this piece already has open in Noru, plus the organization's
//                                 own assets and risks, written by the skill from
//                                 getSecurityFindings + getOrganizationAssets + getOrganizationRisks.
//                                 The open set is the half of the queue a repository cannot know:
//                                 a rule that stopped firing has a finding somewhere that should be
//                                 closed, and only Noru knows it is still open.
//   the repository                Terraform, CloudFormation, Kubernetes and pipeline configuration.
//
// A finding is a POINTER, never a copy. No matched line text is ever written into the derived facts
// or the manifest — one of the bundled rules fires on a line that contains a credential, and a
// scanner that quoted what it found would put that credential into a committed file and then into a
// public pull request. The citation is `file:line`; read the line there.
//
// Identity: a finding is keyed on the check and on the *resource* it fired against, not on the line
// number. Moving a block down a file must not close one finding and open another; changing what the
// block says must.
//
// Usage:
//   node collect.mjs [--repo=<path>] [--check] [--output=json|text] [--quiet]
// Exit codes: 0 ok, 1 queue missing or manifest drifted (--check), 2 usage/IO error.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync,
} from "node:fs";
import { basename, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

export const PIECE = "iac-scan";
export const VERSION = "0.1.0";
const GENERATED_BY = `${PIECE}@${VERSION}`;

// The `source` this piece's findings carry in Noru. A finding is keyed on (source, externalId), so
// this string is part of every identity the piece owns and changing it orphans everything it wrote.
export const FINDING_SOURCE = "iac-scan";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const VOCAB = JSON.parse(readFileSync(join(HERE, "..", "references", "vocabulary.json"), "utf8"));
const RULES = JSON.parse(readFileSync(join(HERE, "..", "references", "checks.json"), "utf8"));

const SKIP_DIRS = new Set([
  ".git", "node_modules", "dist", "build", "out", ".next", ".turbo", "coverage",
  "vendor", "target", ".venv", "venv", "__pycache__", ".noru", ".terraform",
]);
// A configuration file large enough to be a state dump or a lock file is not hand-written
// configuration, and reading it would only slow the scan down.
const MAX_FILE_BYTES = 2_000_000;
const SEVERITY_RANK = { critical: 0, high: 1, medium: 2, low: 3 };

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

/**
 * The day the configuration was observed: the commit date of the state that was scanned, not the
 * day the scan happened to run. The collector reads no clock — that is what keeps it deterministic —
 * and a commit date is the more honest anchor anyway.
 */
export function observedOn(repo) {
  const value = gitValue(repo, ["log", "-1", "--format=%cs"], "");
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
}

// --- what kind of document is this? -------------------------------------------------------------
export function classify(path, text) {
  if (path.endsWith(".tf") || path.endsWith(".tf.json")) return "terraform";
  if (/^\.github\/workflows\/[^/]+\.ya?ml$/.test(path)) return "github_actions";
  if (basename(path) === ".gitlab-ci.yml" || /^\.gitlab\/ci\/.*\.ya?ml$/.test(path)) {
    return "gitlab_ci";
  }
  if (!/\.(ya?ml|json)$/.test(path)) return null;
  if (/^\s*AWSTemplateFormatVersion\s*:/m.test(text)) return "cloudformation";
  if (/^Resources\s*:/m.test(text) && /Type\s*:\s*['"]?AWS::/m.test(text)) return "cloudformation";
  if (/^\s*apiVersion\s*:/m.test(text) && /^\s*kind\s*:/m.test(text)) return "kubernetes";
  return null;
}

// --- which resource is this line inside? --------------------------------------------------------
const TF_BLOCK_RE = /^resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"\s*\{/;

/**
 * A per-line map from line index to the resource that encloses it, so a finding can be identified
 * by what it is about rather than by where it happened to sit today.
 */
export function resourceIndex(technology, lines) {
  const index = new Array(lines.length).fill(null);

  if (technology === "terraform") {
    let current = null;
    let depth = 0;
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      if (depth === 0) {
        const match = TF_BLOCK_RE.exec(line);
        current = match ? `${match[1]}.${match[2]}` : null;
      }
      index[i] = current;
      depth += (line.match(/\{/g) ?? []).length - (line.match(/\}/g) ?? []).length;
      if (depth < 0) depth = 0;
    }
    return index;
  }

  if (technology === "kubernetes") {
    // One YAML stream may hold several documents; each gets its own kind and name.
    const starts = [0];
    for (let i = 0; i < lines.length; i += 1) {
      if (/^---\s*$/.test(lines[i])) starts.push(i + 1);
    }
    for (let s = 0; s < starts.length; s += 1) {
      const from = starts[s];
      const to = s + 1 < starts.length ? starts[s + 1] - 1 : lines.length;
      let kind = null;
      let name = null;
      for (let i = from; i < to; i += 1) {
        const k = /^kind:\s*([A-Za-z0-9_-]+)\s*$/.exec(lines[i]);
        if (k) kind = k[1];
        const n = /^\s{2}name:\s*([A-Za-z0-9_.-]+)\s*$/.exec(lines[i]);
        if (n && name === null) name = n[1];
      }
      const label = kind === null ? null : `${kind}${name === null ? "" : `.${name}`}`;
      for (let i = from; i < to; i += 1) index[i] = label;
    }
    return index;
  }

  if (technology === "cloudformation" || technology === "github_actions") {
    const opener = technology === "cloudformation" ? /^Resources\s*:/ : /^jobs\s*:/;
    const child = /^\s{2}([A-Za-z0-9_-]+)\s*:\s*$/;
    let inside = false;
    let current = null;
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      if (opener.test(line)) {
        inside = true;
        current = null;
      } else if (/^[A-Za-z]/.test(line)) {
        inside = false;
        current = null;
      } else if (inside) {
        const match = child.exec(line);
        if (match) current = match[1];
      }
      index[i] = current;
    }
    return index;
  }

  return index;
}

// --- Terraform blocks, for the checks that are about what is NOT there ---------------------------
export function terraformBlocks(lines) {
  const blocks = [];
  let open = null;
  let depth = 0;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (open === null) {
      const match = TF_BLOCK_RE.exec(line);
      if (match === null) continue;
      open = { type: match[1], name: match[2], start: i + 1, body: [] };
      depth = 0;
    }
    open.body.push(line);
    depth += (line.match(/\{/g) ?? []).length - (line.match(/\}/g) ?? []).length;
    if (depth <= 0) {
      blocks.push({ ...open, body: open.body.join("\n") });
      open = null;
    }
  }
  if (open !== null) blocks.push({ ...open, body: open.body.join("\n") });
  return blocks;
}

// --- the rules ----------------------------------------------------------------------------------
const compiled = new Map();

function rx(pattern) {
  let value = compiled.get(pattern);
  if (value === undefined) {
    value = new RegExp(pattern, "m");
    compiled.set(pattern, value);
  }
  return value;
}

function anyMatch(patterns, text) {
  return (patterns ?? []).some((p) => rx(p).test(text));
}

/**
 * The identity a finding keeps across scans: the rule, and the resource it fired against. Not the
 * line, because moving a block must not look like closing one problem and opening another.
 */
export function findingKey(check, file, resource) {
  const identity = createHash("sha256").update(`${file}#${resource ?? ""}`).digest("hex");
  return `${check}.${identity.slice(0, 12)}`;
}

function makeFinding(check, file, resource, line) {
  return {
    key: findingKey(check.id, file, resource),
    check: check.id,
    technology: check.technology,
    severity: check.severity,
    category: check.category,
    title: check.title,
    file,
    resource: resource ?? null,
    ref: `${file}:${line}`,
  };
}

export function evaluateFile(path, text, technology) {
  const lines = text.split("\n");
  const resources = resourceIndex(technology, lines);
  const out = [];
  const seen = new Set();

  const record = (finding) => {
    // One finding per (rule, resource): a rule firing on four lines of one block is one thing to
    // fix, and four findings would be four things somebody has to disposition.
    if (seen.has(finding.key)) return;
    seen.add(finding.key);
    out.push(finding);
  };

  for (const check of RULES.checks) {
    if (check.technology !== technology) continue;
    if (check.file_match && !check.file_match.every((p) => rx(p).test(text))) continue;

    if (check.detector === "line") {
      for (let i = 0; i < lines.length; i += 1) {
        if (anyMatch(check.line_match, lines[i])) {
          record(makeFinding(check, path, resources[i], i + 1));
        }
      }
      continue;
    }

    // block_match and block_missing are Terraform-only: they need a block with a known extent.
    if (technology !== "terraform") continue;
    for (const block of terraformBlocks(lines)) {
      if (!(check.block_types ?? []).includes(block.type)) continue;
      const hit =
        check.detector === "block_missing"
          ? !anyMatch(check.expect, block.body)
          : anyMatch(check.match, block.body);
      if (hit) record(makeFinding(check, path, `${block.type}.${block.name}`, block.start));
    }
  }
  return out;
}

export function collectFacts(repo, queue) {
  const scanned = [];
  const findings = [];
  for (const rel of walk(repo)) {
    const full = join(repo, rel);
    let size = 0;
    try {
      size = statSync(full).size;
    } catch {
      continue;
    }
    if (size > MAX_FILE_BYTES) continue;
    let text;
    try {
      text = readFileSync(full, "utf8");
    } catch {
      continue;
    }
    // A NUL byte means this is not a text document. Written as an escape on purpose: a raw control
    // character makes grep treat the whole file as binary and skip it, and this repository's secret
    // scan is a grep.
    if (text.includes("\u0000")) continue;
    const technology = classify(rel, text);
    if (technology === null) continue;
    scanned.push({ file: rel, technology });
    findings.push(...evaluateFile(rel, text, technology));
  }

  findings.sort((a, b) => {
    const rank = SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity];
    if (rank !== 0) return rank;
    return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
  });

  const open = queue.open_findings ?? [];
  const produced = new Set(findings.map((f) => f.key));
  return {
    piece: PIECE,
    generated_by: GENERATED_BY,
    finding_source: FINDING_SOURCE,
    checks_available: RULES.checks.length,
    configuration_files: scanned,
    queue_open_findings: open.length,
    // A finding Noru still holds open whose rule no longer fires here. Surfaced in the scan output
    // so the reviewer meets it before the plan does.
    queue_no_longer_reproducing: open
      .map((row) => String(row.external_id ?? ""))
      .filter((id) => id !== "" && !produced.has(id.split(":").slice(1).join(":")))
      .sort(),
    findings,
  };
}

export function digestOf(derived) {
  // `generated_by` is deliberately NOT hashed. This digest answers one question — has the
  // repository changed since the manifest was written? — and the version of the tool that read it
  // is not a fact about the repository.
  //
  // Hashing it made a plugin upgrade indistinguishable from a schema change: every committed
  // manifest reported drift on the next run and CI mode failed with exit 3, for repositories where
  // nothing had moved. It stays in the derived file, and in the manifest, as provenance.
  const { generated_by, ...facts } = derived;
  void generated_by;
  return createHash("sha256").update(JSON.stringify(facts, null, 0)).digest("hex");
}

// --- minimal deterministic YAML emitter (same shape as the other pieces' collectors) -------------
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
          const body = toYaml(item, indent + 2);
          return `${pad}- ${body.slice(indent + 2)}`;
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

/** Deterministic date arithmetic: no clock is read anywhere in this collector. */
export function addDays(isoDate, days) {
  const [year, month, day] = isoDate.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day) + days * 86400000).toISOString().slice(0, 10);
}

export function buildSkeleton(derived, provenance, queue, observed) {
  const observedOnValue = observed ?? "TODO-YYYY-MM-DD";
  const horizon = VOCAB.status_horizon_days.open;
  const findings = derived.findings.map((finding) => ({
    key: finding.key,
    check: finding.check,
    technology: finding.technology,
    severity: finding.severity,
    category: finding.category,
    status: "open",
    title: finding.title,
    file: finding.file,
    resource: finding.resource,
    observed_on: observedOnValue,
    refs: [finding.ref],
    interpretation: {
      owner: "TODO@example.com",
      decided_at: observedOnValue,
      expires_at: observed === null ? "TODO-YYYY-MM-DD" : addDays(observed, Math.round(horizon / 2)),
      rationale:
        "TODO: read the cited line, say whether this is real in this environment and what happens " +
        "next. If it is not real, set status to false_positive and say here what makes it one.",
    },
    needs_review: true,
  }));

  return {
    version: VERSION,
    piece: PIECE,
    source: { ...provenance, derived_digest: digestOf(derived) },
    queue_snapshot: {
      fetched_at: queue.fetched_at,
      via: queue.via,
      source: queue.source ?? FINDING_SOURCE,
      open_findings: queue.open_findings ?? [],
      assets: queue.assets ?? [],
      risks: queue.risks ?? [],
    },
    findings,
  };
}

const SKELETON_HEADER = `# .noru/iac-scan.yml — generated by ${GENERATED_BY}
#
# queue_snapshot is what YOUR Noru organization already holds: the findings this piece has open, the
# assets a finding could be attached to, and the risks one could be filed against. It is not shipped
# by this plugin — re-run :scan to refresh it.
#
# The collector can say which line matched which rule. It cannot say whether the finding is real in
# your environment, how bad it is here, or who is going to act on it. Every TODO below is yours.
# The validator enforces:
#   * asset_external_id and risk_id may only name things present in queue_snapshot
#   * interpretation.expires_at is mandatory and is measured from observed_on, not from decided_at
#   * accepting a finding or calling it a false positive still expires, and needs a real rationale
#   * needs_review: true blocks the push
#
# No matched line text appears in this file by design: one of the rules fires on a line that holds a
# credential, and a scanner that quoted what it found would commit it. Read the citation instead.
#
# Run:  python3 <plugin>/scripts/validate_manifest.py .noru/iac-scan.yml
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

  const queuePath = join(opts.repo, ".noru", ".cache", "iac-queue.json");
  if (!existsSync(queuePath)) {
    process.stderr.write(
      `error: no queue at ${queuePath}\n` +
        "hint: this piece works Noru's queue, so :scan asks Noru first. The skill writes this file " +
        "from getSecurityFindings + getOrganizationAssets + getOrganizationRisks before running " +
        "the collector. Without it a re-scan cannot tell which findings should now be closed.\n"
    );
    return 1;
  }
  let queue;
  try {
    queue = JSON.parse(readFileSync(queuePath, "utf8"));
  } catch (error) {
    process.stderr.write(`error: ${queuePath} is not readable JSON (${error.message})\n`);
    return 2;
  }

  const derived = collectFacts(opts.repo, queue);
  const provenance = repoProvenance(opts.repo);
  const digest = digestOf(derived);
  const manifestPath = join(opts.repo, ".noru", "iac-scan.yml");
  const derivedPath = join(opts.repo, ".noru", ".cache", "iac-scan.derived.json");

  let wroteSkeleton = false;
  let drift = false;
  try {
    mkdirSync(join(opts.repo, ".noru", ".cache"), { recursive: true });
    writeFileSync(derivedPath, `${JSON.stringify(derived, null, 2)}\n`, "utf8");
    const existing = readManifestDigest(manifestPath);
    if (existing === null) {
      if (opts.check) {
        drift = true;
      } else {
        writeFileSync(
          manifestPath,
          SKELETON_HEADER +
            toYaml(buildSkeleton(derived, provenance, queue, observedOn(opts.repo))),
          "utf8"
        );
        wroteSkeleton = true;
      }
    } else if (existing !== digest) {
      drift = true;
    }
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    return 2;
  }

  const bySeverity = {};
  for (const finding of derived.findings) {
    bySeverity[finding.severity] = (bySeverity[finding.severity] ?? 0) + 1;
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
    counts: {
      configuration_files: derived.configuration_files.length,
      findings: derived.findings.length,
      by_severity: bySeverity,
      queue_open_findings: derived.queue_open_findings,
      no_longer_reproducing: derived.queue_no_longer_reproducing.length,
    },
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    const perTechnology = VOCAB.technology
      .map((t) => `${t} ${derived.configuration_files.filter((f) => f.technology === t).length}`)
      .join(", ");
    const perSeverity = VOCAB.severity
      .filter((s) => bySeverity[s])
      .map((s) => `${bySeverity[s]} ${s}`)
      .join(", ");
    const lines = [
      `configuration files: ${derived.configuration_files.length} (${perTechnology})`,
      `findings: ${derived.findings.length}${perSeverity === "" ? "" : ` (${perSeverity})`}`,
    ];
    for (const finding of derived.findings) {
      lines.push(
        `  ${finding.severity.padEnd(8)} ${finding.check}`,
        `      ${finding.ref}${finding.resource === null ? "" : `  (${finding.resource})`}`
      );
    }
    if (derived.queue_no_longer_reproducing.length > 0) {
      lines.push(
        `${derived.queue_no_longer_reproducing.length} finding(s) open in Noru no longer reproduce ` +
          "here — :diff will plan to close them"
      );
    }
    lines.push(`derived facts: ${summary.derived_facts}`);
    if (wroteSkeleton) lines.push(`wrote skeleton: ${summary.manifest}`);
    if (drift) lines.push("DRIFT: the manifest does not match the configuration as it is now");
    process.stdout.write(`${lines.join("\n")}\n`);
  }

  return opts.check && drift ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  process.exit(main(process.argv.slice(2)));
}
