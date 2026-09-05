#!/usr/bin/env node
// Deterministic, offline collector for the review-signoff piece (contract requirement 2).
//
// Node built-ins only. Opens no socket. It reads two local things:
//
//   .noru/.cache/review-queue.json   what Noru said was due, written by the skill from
//                                    getOrganizationControls + getControlContext +
//                                    getEvidenceForControl. Two halves: expectations with nothing
//                                    linked, and linked evidence that has expired or is close to
//                                    it. A review that went stale is due again — that is the part
//                                    of the queue only Noru can tell you about.
//   reviews/                         the machine output a human is about to attest to: the account
//                                    export, the rule dump, the baseline scan, the asset list.
//
// What it derives is small on purpose: the digest of the export, how many records are in it, and a
// suggested cadence-consistent expiry. The reviewer supplies everything that matters — the count
// they actually confirmed, the exceptions, and the sign-off.
//
// Usage:
//   node collect.mjs [--repo=<path>] [--reviews=<dir>] [--check] [--output=json|text] [--quiet]
// Exit codes: 0 ok, 1 queue missing or manifest drifted (--check), 2 usage/IO error.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, statSync, writeFileSync,
} from "node:fs";
import { basename, extname, join, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const PIECE = "review-signoff";
export const VERSION = "0.7.1";
const GENERATED_BY = `${PIECE}@${VERSION}`;

const HERE = fileURLToPath(new URL(".", import.meta.url));
const VOCAB = JSON.parse(readFileSync(join(HERE, "..", "references", "vocabulary.json"), "utf8"));

const DEFAULT_REVIEWS_DIR = "reviews";
const DATE_RE = /(\d{4}-\d{2}-\d{2})/;
const STOPWORDS = new Set([
  "the", "of", "and", "a", "an", "for", "to", "in", "on", "records", "record",
  "evidence", "export", "report", "final", "signed", "copy", "v1", "v2",
]);
// Cadence names that appear in filenames, mapped to the vocabulary's cadence values.
const CADENCE_HINTS = {
  monthly: ["monthly", "month"],
  quarterly: ["quarterly", "quarter"],
  semiannual: ["semiannual", "semi", "halfyearly", "biannual"],
  annual: ["annual", "annually", "yearly", "year"],
};

const USAGE =
  "usage: collect.mjs [--repo=<path>] [--reviews=<dir>] [--check] [--output=json|text] [--quiet]\n";

function parseArgs(argv) {
  const opts = { repo: process.cwd(), reviews: null, check: false, json: false, quiet: false };
  for (const arg of argv) {
    if (arg.startsWith("--repo=")) opts.repo = arg.slice(7);
    else if (arg.startsWith("--reviews=")) opts.reviews = arg.slice(10);
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

export function tokens(text) {
  return String(text)
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((t) => t.length > 2 && !STOPWORDS.has(t));
}

export function slugKey(text) {
  const slug = String(text)
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[^a-z0-9]+/, "")
    .replace(/-+$/, "");
  return slug === "" ? "review" : slug;
}

/**
 * How many records the export actually contains. For a delimited file the header row is not a
 * record; for anything else a non-empty line is the best honest guess, and the reviewer corrects it.
 * The point of the number is that outcome.confirmed + outcome.exceptions has to reconcile with it.
 */
export function countRecords(text, extension) {
  const lines = text.split(/\r?\n/).filter((line) => line.trim() !== "");
  if (VOCAB.row_counted_extensions.includes(extension)) {
    return Math.max(lines.length - 1, 0);
  }
  if (extension === ".json") {
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) return parsed.length;
    } catch {
      return lines.length;
    }
  }
  return lines.length;
}

/** Deterministic date arithmetic: no clock is read anywhere in this collector. */
export function addDays(isoDate, days) {
  const [year, month, day] = isoDate.split("-").map(Number);
  const base = Date.UTC(year, month - 1, day) + days * 86400000;
  return new Date(base).toISOString().slice(0, 10);
}

export function suggestKind(fileName) {
  const haystack = new Set(tokens(fileName));
  const scored = [];
  for (const [kind, aliases] of Object.entries(VOCAB.kind_aliases)) {
    let hits = 0;
    for (const alias of aliases) {
      if (haystack.has(alias)) hits += 1;
    }
    if (hits === 0) continue;
    scored.push({ kind, score: Math.round((hits / aliases.length) * 100) / 100 });
  }
  scored.sort((a, b) => (b.score !== a.score ? b.score - a.score : a.kind < b.kind ? -1 : 1));
  return scored[0] ?? null;
}

export function suggestCadence(fileName) {
  const haystack = new Set(tokens(fileName));
  for (const cadence of VOCAB.cadence) {
    for (const hint of CADENCE_HINTS[cadence] ?? []) {
      if (haystack.has(hint)) return cadence;
    }
  }
  // A "qN" or "hN" period marker is a strong, common signal.
  const lower = fileName.toLowerCase();
  if (/\bq[1-4]\b/.test(lower)) return "quarterly";
  if (/\bh[12]\b/.test(lower)) return "semiannual";
  return null;
}

export function suggestMatches(haystackText, queue, limit = 3) {
  const haystack = new Set(tokens(haystackText));
  if (haystack.size === 0) return [];
  const scored = [];
  for (const control of queue.controls ?? []) {
    for (const item of control.unmet_evidence_items ?? []) {
      const itemTokens = new Set(tokens(item.title));
      if (itemTokens.size === 0) continue;
      let hits = 0;
      for (const token of itemTokens) {
        if (haystack.has(token)) hits += 1;
      }
      if (hits === 0) continue;
      scored.push({
        control_id: control.control_id,
        evidence_item_id: item.id,
        evidence_item_title: item.title,
        score: Math.round((hits / itemTokens.size) * 100) / 100,
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

function listInputs(dir) {
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
      if (entry.isSymbolicLink() || entry.name.startsWith(".")) continue;
      const full = join(current, entry.name);
      if (entry.isDirectory()) stack.push(full);
      else if (entry.isFile()) out.push(full);
    }
  }
  // Sorted, so the result never depends on directory iteration order. Do not remove.
  return out.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

export function scanInputs(repo, reviewsDir, queue) {
  const absolute = join(repo, reviewsDir);
  const files = existsSync(absolute) ? listInputs(absolute) : [];
  return files.map((full) => {
    const rel = relative(repo, full).split(sep).join("/");
    const name = basename(full);
    const extension = extname(name).toLowerCase();
    const bytes = readFileSync(full);
    let text = "";
    let readable = true;
    try {
      text = bytes.toString("utf8");
      // A NUL byte means this is not a text export, so a line count would be meaningless.
      // Written as an escape on purpose: a raw control character makes grep treat the whole
      // file as binary and skip it silently, and this repository's secret scan is a grep.
      if (text.includes("\u0000")) readable = false;
    } catch {
      readable = false;
    }

    const performedOn = name.match(DATE_RE)?.[1] ?? null;
    const cadence = suggestCadence(name);
    const problems = [];
    if (!readable) {
      problems.push(
        "not a text export, so the record count could not be derived; set records_reviewed by hand"
      );
    }
    if (performedOn === null) {
      problems.push("no date in the filename; set performed_on by hand");
    }
    if (cadence === null) {
      problems.push("no cadence in the filename; set cadence by hand");
    }

    return {
      file: rel,
      sha256: createHash("sha256").update(bytes).digest("hex"),
      size_bytes: statSync(full).size,
      key: slugKey(basename(name, extension)),
      records_counted: readable ? countRecords(text, extension) : null,
      performed_on: performedOn,
      suggested_kind: suggestKind(name),
      suggested_cadence: cadence,
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

// --- minimal deterministic YAML emitter (same shape as the other pieces' collectors) ------------
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
  const reviews = derived.inputs.map((input) => {
    const best = input.suggested_matches[0];
    const cadence = input.suggested_cadence;
    const performedOn = input.performed_on;
    // The suggested expiry is the midpoint of the window the cadence allows, so the validator's
    // cadence check passes if the reviewer keeps it and is easy to reason about if they do not.
    const window = cadence ? VOCAB.cadence_days[cadence] : null;
    const expiresAt =
      performedOn && window
        ? addDays(performedOn, Math.round((window[0] + window[1]) / 2))
        : "TODO-YYYY-MM-DD";

    return {
      key: input.key,
      kind: input.suggested_kind ? input.suggested_kind.kind : "TODO_set_the_review_kind",
      title: input.key,
      cadence: cadence ?? "TODO_set_the_cadence",
      performed_on: performedOn ?? "TODO-YYYY-MM-DD",
      input: {
        file: input.file,
        sha256: input.sha256,
        size_bytes: input.size_bytes,
        records_reviewed: input.records_counted ?? 0,
        produced_by: "TODO: say where this export came from, in your own words",
      },
      outcome: {
        confirmed: input.records_counted ?? 0,
        exceptions: 0,
      },
      exceptions: [],
      refs: [`${input.file}:1`],
      control_mappings: best
        ? [{ control_id: best.control_id, evidence_item_ids: [best.evidence_item_id] }]
        : [],
      interpretation: {
        owner: "TODO@example.com",
        decided_at: "TODO-YYYY-MM-DD",
        expires_at: expiresAt,
        rationale: best
          ? `TODO: say what you actually checked, and why this review satisfies "${best.evidence_item_title}" (match score ${best.score})`
          : "TODO: no queue item matched this export — say which expectation it satisfies and what you checked",
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
    reviews,
  };
}

const SKELETON_HEADER = `# .noru/review-signoff.yml — generated by ${GENERATED_BY}
#
# queue_snapshot is what YOUR Noru organization said was due: expectations with nothing linked, and
# sign-offs that have expired or are about to. It is not shipped by this plugin and it is not
# editable guesswork: re-run :scan to refresh it.
#
# The collector can hash an export and count its rows. It cannot review anything. Every TODO below,
# and the confirmed/exception split, is yours. The validator enforces:
#   * control_mappings may only reference controls and evidence items present in queue_snapshot
#   * confirmed + exceptions must reconcile with records_reviewed
#   * every exception needs a disposition and a named owner
#   * interpretation.expires_at is mandatory and must match the declared cadence
#   * you cannot sign off a review before the day it was performed
#   * needs_review: true blocks the push
#
# Run:  python3 <plugin>/scripts/validate_manifest.py .noru/review-signoff.yml
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

  const queuePath = join(opts.repo, ".noru", ".cache", "review-queue.json");
  if (!existsSync(queuePath)) {
    process.stderr.write(
      `error: no queue at ${queuePath}\n` +
        "hint: this piece works Noru's queue, so :scan asks Noru first. The skill writes this file " +
        "from getOrganizationControls + getControlContext + getEvidenceForControl before running " +
        "the collector.\n"
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

  const reviewsDir = opts.reviews ?? DEFAULT_REVIEWS_DIR;
  const inputs = scanInputs(opts.repo, reviewsDir, queue);
  const controls = queue.controls ?? [];
  const unmet = controls.reduce((sum, c) => sum + (c.unmet_evidence_items ?? []).length, 0);
  const expiring = controls.reduce((sum, c) => sum + (c.expiring_evidence ?? []).length, 0);
  const derived = {
    piece: PIECE,
    generated_by: GENERATED_BY,
    reviews_dir: reviewsDir,
    queue_controls: controls.length,
    queue_unmet_items: unmet,
    queue_expiring_evidence: expiring,
    inputs,
  };

  const provenance = repoProvenance(opts.repo);
  const digest = digestOf(derived);
  const manifestPath = join(opts.repo, ".noru", "review-signoff.yml");
  const derivedPath = join(opts.repo, ".noru", ".cache", "review-signoff.derived.json");

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

  const flagged = inputs.filter((i) => i.problems.length > 0);
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
      queue_expiring_evidence: expiring,
      inputs: inputs.length,
      flagged: flagged.length,
      unmatched: inputs.filter((i) => i.suggested_matches.length === 0).length,
      records: inputs.reduce((n, i) => n + (i.records_counted ?? 0), 0),
    },
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    const lines = [
      `queue: ${derived.queue_controls} control(s), ${unmet} unmet expectation(s), ` +
        `${expiring} sign-off(s) expiring or expired`,
      `exports in ${reviewsDir}: ${inputs.length}`,
    ];
    for (const input of inputs) {
      const best = input.suggested_matches[0];
      lines.push(
        `  ${input.problems.length > 0 ? "!" : "-"} ${input.file}` +
          `  [${input.suggested_kind ? input.suggested_kind.kind : "kind unknown"}, ` +
          `${input.suggested_cadence ?? "cadence unknown"}, ` +
          `${input.records_counted ?? "?"} record(s)]` +
          (best
            ? `  ->  ${best.control_id} / ${best.evidence_item_id} "${best.evidence_item_title}" (score ${best.score})`
            : "  ->  no queue item matched this export")
      );
      for (const problem of input.problems) lines.push(`      ${problem}`);
    }
    lines.push(`derived facts: ${summary.derived_facts}`);
    if (wroteSkeleton) lines.push(`wrote skeleton: ${summary.manifest}`);
    if (drift) {
      lines.push("DRIFT: the manifest does not match the exports and queue as they are now");
    }
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
