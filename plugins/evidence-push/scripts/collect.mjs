#!/usr/bin/env node
// Deterministic, offline collector for the evidence-push piece (contract requirement 2).
//
// Node built-ins only. Opens no socket. It reads two local things:
//
//   .noru/.cache/evidence-queue.json   what Noru said was unmet, written by the skill from
//                                      getOrganizationControls + getControlContext. This file is
//                                      the queue (contract requirement 9) and the collector never
//                                      invents an entry in it.
//   .noru/artifacts/                   the local files a human dropped there — the pen test PDF,
//                                      the signed access review, the UPS certificate.
//
// It hashes and types each artifact, checks it against Noru's upload limits before anyone tries an
// upload, and suggests which unmet expectation each file most plausibly satisfies by matching the
// filename against the queue's own titles. The suggestion is a starting point with a score, never
// a decision: the human writes the interpretation block.
//
// Usage:
//   node collect.mjs [--repo=<path>] [--artifacts=<dir>] [--check] [--output=json|text] [--quiet]
// Exit codes: 0 ok, 1 queue missing or manifest drifted (--check), 2 usage/IO error.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, statSync, writeFileSync,
} from "node:fs";
import { basename, join, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const PIECE = "evidence-push";
export const VERSION = "0.7.1";
const GENERATED_BY = `${PIECE}@${VERSION}`;

const HERE = fileURLToPath(new URL(".", import.meta.url));
const VOCAB = JSON.parse(readFileSync(join(HERE, "..", "references", "vocabulary.json"), "utf8"));

const DEFAULT_ARTIFACT_DIR = join(".noru", "artifacts");
// Words that carry no discriminating signal when matching a filename to a catalogue title.
const STOPWORDS = new Set([
  "the", "of", "and", "a", "an", "for", "to", "in", "on", "records", "record",
  "evidence", "document", "documents", "final", "signed", "copy", "v1", "v2",
]);

const USAGE =
  "usage: collect.mjs [--repo=<path>] [--artifacts=<dir>] [--check] [--output=json|text] [--quiet]\n";

function parseArgs(argv) {
  const opts = {
    repo: process.cwd(),
    artifacts: null,
    check: false,
    json: false,
    quiet: false,
  };
  for (const arg of argv) {
    if (arg.startsWith("--repo=")) opts.repo = arg.slice(7);
    else if (arg.startsWith("--artifacts=")) opts.artifacts = arg.slice(12);
    else if (arg === "--check") opts.check = true;
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

export function mimeFor(name) {
  const dot = name.lastIndexOf(".");
  if (dot === -1) return null;
  return VOCAB.extension_to_mime[name.slice(dot).toLowerCase()] ?? null;
}

export function tokens(text) {
  return String(text)
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((t) => t.length > 2 && !STOPWORDS.has(t));
}

/**
 * Filename-to-expectation matching. The vocabulary being matched against is the queue Noru
 * returned, so this scores against the framework's own words rather than any opinion of ours.
 * Deterministic: ties break on (control_id, item id).
 */
export function suggestMatches(fileName, queue, limit = 3) {
  const fileTokens = new Set(tokens(fileName));
  if (fileTokens.size === 0) return [];
  const scored = [];
  for (const control of queue.controls ?? []) {
    for (const item of control.unmet_evidence_items ?? []) {
      const itemTokens = tokens(item.title);
      if (itemTokens.length === 0) continue;
      let hits = 0;
      for (const token of new Set(itemTokens)) {
        if (fileTokens.has(token)) hits += 1;
      }
      if (hits === 0) continue;
      const score = Math.round((hits / new Set(itemTokens).size) * 100) / 100;
      scored.push({
        control_id: control.control_id,
        evidence_item_id: item.id,
        evidence_item_title: item.title,
        score,
      });
    }
  }
  scored.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    if (a.control_id !== b.control_id) return a.control_id < b.control_id ? -1 : 1;
    return a.evidence_item_id < b.evidence_item_id ? -1 : 1;
  });
  return scored.slice(0, limit);
}

function listArtifacts(dir) {
  const out = [];
  const stack = [dir];
  while (stack.length > 0) {
    const current = stack.pop();
    let entries;
    try {
      entries = readdirSync(current, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      const full = join(current, entry.name);
      if (entry.isDirectory()) stack.push(full);
      else if (entry.isFile() && !entry.name.startsWith(".")) out.push(full);
    }
  }
  return out.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

export function scanArtifacts(repo, artifactDir, queue) {
  const absolute = join(repo, artifactDir);
  const files = existsSync(absolute) ? listArtifacts(absolute) : [];
  return files.map((full) => {
    const rel = relative(repo, full).split(sep).join("/");
    const name = basename(full);
    const size = statSync(full).size;
    const mime = mimeFor(name);
    const problems = [];
    if (mime === null) {
      problems.push(
        `no accepted MIME type for this extension; ${VOCAB.upload_endpoint} would reject it`
      );
    }
    if (size > VOCAB.max_file_bytes) {
      problems.push(
        `${size} bytes exceeds the ${VOCAB.max_file_bytes / (1024 * 1024)}MB cap on ${VOCAB.upload_endpoint}`
      );
    }
    if (size === 0) problems.push("file is empty");
    return {
      file: rel,
      sha256: createHash("sha256").update(readFileSync(full)).digest("hex"),
      size_bytes: size,
      mime_type: mime,
      suggested_matches: suggestMatches(name, queue),
      problems,
    };
  });
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

// --- minimal deterministic YAML emitter (same shape as the ai-inventory collector) -------------
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

export function buildSkeleton(derived, provenance, queue) {
  const uploads = derived.artifacts
    .filter((a) => a.problems.length === 0)
    .map((a) => {
      const best = a.suggested_matches[0];
      return {
        file: a.file,
        sha256: a.sha256,
        size_bytes: a.size_bytes,
        mime_type: a.mime_type,
        title: basename(a.file),
        description: `Uploaded from ${provenance.slug} by ${GENERATED_BY}.`,
        tags: [PIECE],
        control_mappings: best
          ? [{ control_id: best.control_id, evidence_item_ids: [best.evidence_item_id] }]
          : [],
        interpretation: {
          owner: "TODO@example.com",
          decided_at: "TODO-YYYY-MM-DD",
          expires_at: "TODO-YYYY-MM-DD",
          rationale: best
            ? `TODO: confirm this artifact satisfies "${best.evidence_item_title}" (match score ${best.score}) and say why`
            : "TODO: no queue item matched this filename — say which expectation it satisfies and why",
        },
        needs_review: true,
      };
    });

  return {
    version: VERSION,
    piece: PIECE,
    source: { ...provenance, derived_digest: digestOf(derived) },
    queue_snapshot: {
      fetched_at: queue.fetched_at,
      via: queue.via,
      controls: queue.controls,
    },
    uploads,
  };
}

const SKELETON_HEADER = `# .noru/evidence-push.yml — generated by ${GENERATED_BY}
#
# queue_snapshot is what YOUR Noru organization said was unmet. It is not shipped by this plugin
# and it is not editable guesswork: re-run :scan to refresh it.
#
# Every TODO below is a decision a person has to make and sign for. The validator enforces:
#   * control_mappings may only reference controls and evidence items present in queue_snapshot
#   * interpretation.owner must be a person, not a team alias
#   * needs_review: true blocks the push
#
# Run:  python3 <plugin>/scripts/validate_manifest.py .noru/evidence-push.yml
`;

function readManifestDigest(manifestPath) {
  if (!existsSync(manifestPath)) return null;
  const text = readFileSync(manifestPath, "utf8");
  const m = text.match(/derived_digest:\s*"?([0-9a-f]{64})"?/);
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

  const queuePath = join(opts.repo, ".noru", ".cache", "evidence-queue.json");
  if (!existsSync(queuePath)) {
    process.stderr.write(
      `error: no queue at ${queuePath}\n` +
        "hint: this piece works Noru's queue, so :scan asks Noru first. The skill writes this file " +
        "from getOrganizationControls + getControlContext before running the collector.\n"
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

  const artifactDir = opts.artifacts ?? DEFAULT_ARTIFACT_DIR;
  const artifacts = scanArtifacts(opts.repo, artifactDir, queue);
  const unmet = (queue.controls ?? []).reduce(
    (sum, c) => sum + (c.unmet_evidence_items ?? []).length,
    0
  );
  const derived = {
    piece: PIECE,
    generated_by: GENERATED_BY,
    artifact_dir: artifactDir,
    queue_controls: (queue.controls ?? []).length,
    queue_unmet_items: unmet,
    artifacts,
  };

  const provenance = repoProvenance(opts.repo);
  const digest = digestOf(derived);
  const manifestPath = join(opts.repo, ".noru", "evidence-push.yml");
  const derivedPath = join(opts.repo, ".noru", ".cache", "evidence-push.derived.json");

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
          SKELETON_HEADER + toYaml(buildSkeleton(derived, provenance, queue)),
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

  const rejected = artifacts.filter((a) => a.problems.length > 0);
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
      queue_controls: derived.queue_controls,
      queue_unmet_items: unmet,
      artifacts: artifacts.length,
      uploadable: artifacts.length - rejected.length,
      rejected: rejected.length,
      unmatched: artifacts.filter((a) => a.suggested_matches.length === 0).length,
    },
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    const lines = [
      `queue: ${derived.queue_controls} control(s), ${unmet} unmet expectation(s)`,
      `artifacts in ${artifactDir}: ${artifacts.length}`,
    ];
    for (const a of artifacts) {
      const best = a.suggested_matches[0];
      lines.push(
        `  ${a.problems.length > 0 ? "x" : "-"} ${a.file}` +
          (best
            ? `  ->  ${best.control_id} / ${best.evidence_item_id} "${best.evidence_item_title}" (score ${best.score})`
            : "  ->  no queue item matched this filename")
      );
      for (const problem of a.problems) lines.push(`      ${problem}`);
    }
    lines.push(`derived facts: ${summary.derived_facts}`);
    if (wroteSkeleton) lines.push(`wrote skeleton: ${summary.manifest}`);
    if (drift) lines.push("DRIFT: the manifest does not match the artifacts and queue as they are now");
    process.stdout.write(`${lines.join("\n")}\n`);
  }

  return opts.check && drift ? 1 : 0;
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
