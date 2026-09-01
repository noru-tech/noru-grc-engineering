#!/usr/bin/env node
// Deterministic, offline collector for the audit-pack piece (contract requirement 2).
//
// Node built-ins only. Opens no socket. This is the one piece that mostly ASSEMBLES rather than
// discovers, so what it reads is three local things:
//
//   .noru/.cache/audit-queue.json   the scope of the pack as Noru returned it: the framework, its
//                                   controls, what the framework expects of each, what is already
//                                   linked and whether a testing procedure exists. Written by the
//                                   skill from getOrganizationFrameworks + getOrganizationControls
//                                   + getControlContext + getEvidenceForControl + getEvidenceItems.
//   .noru/artifacts/                the files an auditor asks for that never reach a server-side
//                                   integration — the exports, the reports, the certificates.
//   .noru/*.yml                      the other pieces' committed manifests, so the pack can say
//                                   which reviewed inputs produced what is in Noru rather than only
//                                   what the register happens to say today.
//
// It does three things with them: works out the gap per control, draws a reproducible sample from
// every population it can read, and — once a validated manifest exists — renders the bundle a human
// actually hands over, under .noru/audit-pack/.
//
// The sample is drawn from the population file's own digest, so it needs no clock and no random
// source: the same file always draws the same sample, and an auditor holding the file can redraw it
// from the method, the seed and the size recorded in the manifest.
//
// Usage:
//   node collect.mjs [--repo=<path>] [--artifacts=<dir>] [--check] [--output=json|text] [--quiet]
// Exit codes: 0 ok, 1 queue missing or manifest drifted (--check), 2 usage/IO error.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, statSync, writeFileSync,
} from "node:fs";
import { basename, dirname, extname, join, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const PIECE = "audit-pack";
export const VERSION = "0.4.0";
const GENERATED_BY = `${PIECE}@${VERSION}`;

const HERE = fileURLToPath(new URL(".", import.meta.url));
const VOCAB = JSON.parse(readFileSync(join(HERE, "..", "references", "vocabulary.json"), "utf8"));

const DEFAULT_ARTIFACTS_DIR = ".noru/artifacts";
const BUNDLE_DIR = ".noru/audit-pack";
// A population large enough to be a database dump is not an export somebody sampled by hand.
const MAX_POPULATION_BYTES = 20_000_000;

const USAGE =
  "usage: collect.mjs [--repo=<path>] [--artifacts=<dir>] [--check] [--output=json|text] [--quiet]\n";

function parseArgs(argv) {
  const opts = { repo: process.cwd(), artifacts: null, check: false, json: false, quiet: false };
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

function listFiles(dir) {
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

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

export function slugKey(text) {
  const slug = String(text)
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[^a-z0-9]+/, "")
    .replace(/-+$/, "");
  return slug === "" ? "workpaper" : slug;
}

// --- populations and the sample ------------------------------------------------------------------
/**
 * A reference that finds each row again in the export. The first column where there is one, because
 * that is what an auditor reads back to you; a positional fallback otherwise, and a positional
 * suffix wherever the first column repeats.
 */
export function rowKeys(text, extension) {
  const separator = extension === ".tsv" ? "\t" : ",";
  const lines = text.split(/\r?\n/).filter((line) => line.trim() !== "");
  const body = lines.slice(1);
  const counts = new Map();
  for (const line of body) {
    const first = line.split(separator)[0].trim().replace(/^"|"$/g, "");
    counts.set(first, (counts.get(first) ?? 0) + 1);
  }
  return body.map((line, index) => {
    const first = line.split(separator)[0].trim().replace(/^"|"$/g, "");
    if (first === "") return `row-${index + 1}`;
    return counts.get(first) > 1 ? `${first}#${index + 1}` : first;
  });
}

/**
 * The sample, drawn from a seed and nothing else. Ordering by sha256(seed|key) is stable, uniform
 * enough for a sample, and — the point — reproducible by anybody holding the population file.
 */
export function drawSample(keys, seed, size) {
  return keys
    .map((key) => ({ key, order: sha256(`${seed}|${key}`) }))
    .sort((a, b) => (a.order < b.order ? -1 : a.order > b.order ? 1 : 0))
    .slice(0, Math.min(size, keys.length))
    .map((row) => row.key);
}

export function minimumSampleFor(populationSize) {
  for (const band of VOCAB.minimum_sample) {
    if (band.up_to === null || populationSize <= band.up_to) {
      return Math.min(band.minimum, populationSize);
    }
  }
  return populationSize;
}

// --- the local half ------------------------------------------------------------------------------
export function scanArtifacts(repo, artifactsDir) {
  const absolute = join(repo, artifactsDir);
  if (!existsSync(absolute)) return [];
  return listFiles(absolute).map((full) => {
    const rel = relative(repo, full).split(sep).join("/");
    const bytes = readFileSync(full);
    const extension = extname(full).toLowerCase();
    const artifact = {
      file: rel,
      sha256: sha256(bytes),
      size_bytes: statSync(full).size,
      population: null,
    };
    if (!VOCAB.population_extensions.includes(extension)) return artifact;
    if (artifact.size_bytes > MAX_POPULATION_BYTES) return artifact;

    let text;
    try {
      text = bytes.toString("utf8");
    } catch {
      return artifact;
    }
    // A NUL byte means this is not a delimited export. Written as an escape on purpose: a raw
    // control character makes grep treat the whole file as binary and skip it, and this
    // repository's secret scan is a grep.
    if (text.includes("\u0000")) return artifact;

    const keys = rowKeys(text, extension);
    if (keys.length === 0) return artifact;
    const seed = artifact.sha256.slice(0, 32);
    const minimum = minimumSampleFor(keys.length);
    const size = Math.min(Math.max(VOCAB.default_sample_size, minimum), keys.length);
    artifact.population = {
      size: keys.length,
      seed,
      minimum_sample: minimum,
      suggested_sample_size: size,
      suggested_sample: drawSample(keys, seed, size),
    };
    return artifact;
  });
}

/**
 * The other pieces' committed manifests. Read as text and matched on the `piece:` key rather than
 * parsed: the collector has no YAML loader and does not need one to say which files these are and
 * what their bytes were.
 */
export function scanUpstreamManifests(repo) {
  const noru = join(repo, ".noru");
  if (!existsSync(noru)) return [];
  const out = [];
  let entries;
  try {
    entries = readdirSync(noru, { withFileTypes: true });
  } catch {
    return [];
  }
  for (const entry of entries) {
    if (!entry.isFile() || !/\.ya?ml$/.test(entry.name)) continue;
    if (entry.name === `${PIECE}.yml` || entry.name === `${PIECE}.yaml`) continue;
    const full = join(noru, entry.name);
    const bytes = readFileSync(full);
    const match = bytes.toString("utf8").match(/^piece:\s*([a-z0-9-]+)\s*$/m);
    if (match === null) continue;
    out.push({
      piece: match[1],
      file: relative(repo, full).split(sep).join("/"),
      sha256: sha256(bytes),
    });
  }
  return out.sort((a, b) => (a.file < b.file ? -1 : a.file > b.file ? 1 : 0));
}

// --- the Noru half, as the queue returned it ------------------------------------------------------
export function analyseControls(queue) {
  return (queue.controls ?? []).map((control) => {
    const expected = control.expected_evidence_items ?? [];
    const linked = control.linked_evidence ?? [];
    const satisfied = new Set(
      linked.map((row) => row.evidence_item_id).filter((id) => typeof id === "string" && id !== "")
    );
    const unmet = expected.filter((item) => !satisfied.has(item.id)).map((item) => item.id);
    const expired = linked
      .filter((row) => String(row.status ?? "") === "expired")
      .map((row) => row.evidence_id);
    return {
      control_id: control.control_id,
      name: control.name ?? null,
      status: control.status ?? null,
      coverage: control.coverage ?? null,
      testing_guidance_available: control.testing_guidance_available === true,
      expected: expected.length,
      linked: linked.length,
      unmet_evidence_items: unmet.sort(),
      expired_evidence: expired.sort(),
    };
  });
}

export function collectFacts(repo, artifactsDir, queue) {
  const artifacts = scanArtifacts(repo, artifactsDir);
  const controls = analyseControls(queue);
  return {
    piece: PIECE,
    generated_by: GENERATED_BY,
    artifacts_dir: artifactsDir,
    framework_id: queue.framework_id ?? null,
    window: queue.window ?? null,
    controls,
    artifacts,
    upstream_manifests: scanUpstreamManifests(repo),
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

// --- minimal deterministic YAML emitter (same shape as the other pieces' collectors) --------------
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

export function buildSkeleton(derived, provenance, queue) {
  const window = queue.window ?? { from: "TODO-YYYY-MM-DD", to: "TODO-YYYY-MM-DD" };
  const windowEnd = /^\d{4}-\d{2}-\d{2}$/.test(String(window.to)) ? window.to : null;
  // Half the horizon an effective conclusion is allowed, so the suggestion is inside the rule
  // rather than on its edge.
  const expires =
    windowEnd === null
      ? "TODO-YYYY-MM-DD"
      : addDays(windowEnd, Math.round(VOCAB.assurance_days.effective / 2));

  const workpapers = derived.controls.map((control) => ({
    key: slugKey(control.control_id),
    control_id: control.control_id,
    evidence_item_ids: control.unmet_evidence_items,
    scope:
      "TODO: say what you tested and how, in your own words. Read the procedure Noru serves for " +
      "this control; do not paste it here.",
    inspected: [
      {
        kind: "artifact",
        reference: "TODO: a path under the artifacts directory, or an evidence id from the queue",
        note: "TODO: what you looked at in it",
      },
    ],
    exceptions: [],
    conclusion: "not_tested",
    refs: [`${derived.artifacts_dir}/TODO:1`],
    interpretation: {
      owner: "TODO@example.com",
      decided_at: windowEnd ?? "TODO-YYYY-MM-DD",
      expires_at: expires,
      rationale:
        "TODO: say what you concluded about this control over the window, and what it rests on. " +
        "A conclusion of not_tested is a legitimate answer and a better one than a conclusion " +
        "nobody drew.",
    },
    needs_review: true,
  }));

  return {
    version: VERSION,
    piece: PIECE,
    source: { ...provenance, derived_digest: digestOf(derived) },
    pack: {
      key: slugKey(`${queue.framework_id ?? "pack"}-${window.from}-${window.to}`),
      title: `TODO: what to call this pack`,
      window: { from: window.from, to: window.to },
      prepared_by: "TODO@example.com",
    },
    queue_snapshot: {
      fetched_at: queue.fetched_at,
      via: queue.via,
      framework_id: queue.framework_id,
      framework_name: queue.framework_name,
      controls: queue.controls,
    },
    inputs: {
      artifacts: derived.artifacts.map((a) => ({
        file: a.file,
        sha256: a.sha256,
        size_bytes: a.size_bytes,
      })),
      manifests: derived.upstream_manifests,
    },
    workpapers,
  };
}

const SKELETON_HEADER = `# .noru/audit-pack.yml — generated by ${GENERATED_BY}
#
# queue_snapshot is the scope of the pack as YOUR Noru organization returned it: the framework, its
# controls, what it expects of each and what is already linked. It is not shipped by this plugin —
# re-run :scan to refresh it. The framework's testing procedure is deliberately NOT copied here;
# read it from Noru and write what you actually did in \`scope\`.
#
# The collector can assemble. It cannot test anything, and it cannot conclude. Every TODO below is
# yours. The validator enforces:
#   * every control_id and evidence_item_id must be in the queue snapshot
#   * an artifact or manifest you inspected must be one this scan digested, at that digest
#   * a sample must meet the minimum for its population size, and must be redrawable
#   * interpretation.expires_at is mandatory and is measured from the END of the audit window
#   * a deficient or untested control gets a short horizon, not a year
#   * needs_review: true blocks the push
#
# Run:  python3 <plugin>/scripts/validate_manifest.py .noru/audit-pack.yml \\
#         --emit-parsed=.noru/.cache/audit-pack.parsed.json
# then re-run :scan — that is when the bundle under .noru/audit-pack/ becomes the thing you hand over.
`;

// --- the bundle ----------------------------------------------------------------------------------
function fence(text) {
  // The pack is a document a person reads. Nothing from the manifest is executed or interpreted;
  // it is quoted, and a manifest that contains markdown does not get to restructure the pack.
  return String(text ?? "").replace(/\r?\n/g, " ").trim();
}

function bundleIndex(derived, provenance, manifest) {
  const lines = [
    `# Audit pack — ${manifest ? fence(manifest.pack.title ?? manifest.pack.key) : "not yet reviewed"}`,
    "",
    `Framework: ${derived.framework_id ?? "(not set)"}`,
    `Window: ${derived.window ? `${derived.window.from} to ${derived.window.to}` : "(not set)"}`,
    `Repository: ${provenance.slug} @ ${provenance.commit_sha} (${provenance.branch})`,
    `Assembled by: ${GENERATED_BY}`,
    "",
  ];

  if (manifest === null) {
    lines.push(
      "## This pack has not been reviewed yet",
      "",
      "`.noru/audit-pack.yml` has not been filled in and validated, so there are no conclusions to",
      "hand over. What follows is the scope and the inputs only.",
      ""
    );
  } else {
    lines.push(
      `Prepared by: ${fence(manifest.pack.prepared_by)}`,
      `Reviewed by: ${manifest.pack.reviewed_by ? fence(manifest.pack.reviewed_by) : "(nobody)"}`,
      ""
    );
  }

  lines.push("## Controls in scope", "", "| Control | Expected | Linked | Unmet | Conclusion |", "|---|---|---|---|---|");
  const conclusions = new Map(
    (manifest?.workpapers ?? []).map((w) => [w.control_id, w.conclusion])
  );
  for (const control of derived.controls) {
    lines.push(
      `| ${control.control_id} | ${control.expected} | ${control.linked} | ` +
        `${control.unmet_evidence_items.length} | ${conclusions.get(control.control_id) ?? "—"} |`
    );
  }

  lines.push("", "## Artifacts", "", "| File | sha256 | Bytes |", "|---|---|---|");
  for (const artifact of derived.artifacts) {
    lines.push(`| ${artifact.file} | \`${artifact.sha256}\` | ${artifact.size_bytes} |`);
  }
  if (derived.artifacts.length === 0) lines.push("| (none) | | |");

  lines.push("", "## Reviewed inputs already in Noru", "", "| Piece | Manifest | sha256 |", "|---|---|---|");
  for (const upstream of derived.upstream_manifests) {
    lines.push(`| ${upstream.piece} | ${upstream.file} | \`${upstream.sha256}\` |`);
  }
  if (derived.upstream_manifests.length === 0) lines.push("| (none) | | |");

  lines.push(
    "",
    "---",
    "",
    "This pack is a point-in-time export for handover. It is regenerated from Noru and from the",
    "files above every time `:scan` runs, and nothing reads it back — the register is Noru's.",
    ""
  );
  return lines.join("\n");
}

function workpaperDocument(workpaper, derived, provenance) {
  const control = derived.controls.find((c) => c.control_id === workpaper.control_id) ?? null;
  const exceptions = workpaper.exceptions ?? [];
  const lines = [
    `# Workpaper — ${fence(workpaper.control_id)}`,
    "",
    `Control: ${fence(control?.name ?? workpaper.control_id)}`,
    `Status in Noru: ${control?.status ?? "(unknown)"}${control?.coverage === null || control === null ? "" : `, coverage ${control.coverage}`}`,
    `Testing procedure available from Noru: ${control?.testing_guidance_available ? "yes" : "no"}`,
    "",
    "## What was tested",
    "",
    fence(workpaper.scope),
    "",
    "## What was inspected",
    "",
    ...(workpaper.inspected ?? []).map(
      (row) =>
        `- **${row.kind}** \`${fence(row.reference)}\`` +
        (row.sha256 ? ` (sha256 \`${row.sha256}\`)` : "") +
        (row.note ? ` — ${fence(row.note)}` : "")
    ),
    "",
  ];

  if (workpaper.population && workpaper.sample) {
    lines.push(
      "## Sampling",
      "",
      `Population: \`${fence(workpaper.population.file)}\` — ${workpaper.population.size} item(s), sha256 \`${workpaper.population.sha256}\``,
      `Method: ${workpaper.sample.method}`,
      `Seed: \`${fence(workpaper.sample.seed ?? "(none)")}\``,
      `Size: ${workpaper.sample.size}`,
      "",
      "Redraw it: order the population by `sha256(seed + \"|\" + row reference)` ascending and take",
      "the first `size` rows. The drawn sample is listed in `sampling/" + slugKey(workpaper.key) + ".csv`.",
      ""
    );
  }

  lines.push(
    `## Exceptions (${exceptions.length})`,
    "",
    ...(exceptions.length > 0
      ? exceptions.map(
          (e) =>
            `- \`${fence(e.reference)}\` — ${fence(e.description)} ` +
            `[${e.disposition}, owner ${fence(e.owner)}${e.resolved_on ? `, resolved ${e.resolved_on}` : ""}]`
        )
      : ["(none)"]),
    "",
    "## Conclusion",
    "",
    `**${workpaper.conclusion}**`,
    "",
    fence(workpaper.interpretation.rationale),
    "",
    `Concluded by: ${fence(workpaper.interpretation.owner)} on ${workpaper.interpretation.decided_at}`,
    `Stands until: ${workpaper.interpretation.expires_at}`,
    "",
    "Read from:",
    ...(workpaper.refs ?? []).map((r) => `- \`${r}\``),
    "",
    `Source: ${provenance.slug} @ ${provenance.commit_sha} (${provenance.branch})`,
    ""
  );
  return lines.join("\n");
}

function sampleCsv(workpaper) {
  const rows = ["reference"];
  for (const key of workpaper.sample.drawn) rows.push(`"${String(key).replace(/"/g, '""')}"`);
  return `${rows.join("\n")}\n`;
}

/**
 * Render the pack. Only from a manifest that VALIDATED against this same repository state — a pack
 * built from an unvalidated file is a pack whose conclusions nothing checked, and it would be handed
 * to an auditor exactly like a real one.
 */
export function renderBundle(repo, derived, provenance, manifest) {
  const root = join(repo, BUNDLE_DIR);
  const written = [];
  const write = (rel, body) => {
    const full = join(root, rel);
    mkdirSync(dirname(full), { recursive: true });
    writeFileSync(full, body, "utf8");
    written.push(`${BUNDLE_DIR}/${rel}`);
  };

  mkdirSync(root, { recursive: true });
  write("index.md", bundleIndex(derived, provenance, manifest));
  for (const workpaper of manifest?.workpapers ?? []) {
    write(`workpapers/${slugKey(workpaper.key)}.md`, workpaperDocument(workpaper, derived, provenance));
    if (workpaper.sample) write(`sampling/${slugKey(workpaper.key)}.csv`, sampleCsv(workpaper));
  }
  return written.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

function readManifestDigest(manifestPath) {
  if (!existsSync(manifestPath)) return null;
  const m = readFileSync(manifestPath, "utf8").match(/derived_digest:\s*"?([0-9a-f]{64})"?/);
  return m ? m[1] : "";
}

/** The validated manifest, but only if it is about the repository as it is right now. */
function readParsedManifest(repo, digest) {
  const path = join(repo, ".noru", ".cache", "audit-pack.parsed.json");
  if (!existsSync(path)) return null;
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8"));
    return parsed?.source?.derived_digest === digest ? parsed : null;
  } catch {
    return null;
  }
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

  const queuePath = join(opts.repo, ".noru", ".cache", "audit-queue.json");
  if (!existsSync(queuePath)) {
    process.stderr.write(
      `error: no queue at ${queuePath}\n` +
        "hint: this piece assembles what Noru already holds, so :scan asks Noru first. The skill " +
        "writes this file from getOrganizationFrameworks + getOrganizationControls + " +
        "getControlContext + getEvidenceForControl + getEvidenceItems before running the " +
        "collector. There is no pack without a scope.\n"
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

  const artifactsDir = opts.artifacts ?? DEFAULT_ARTIFACTS_DIR;
  const derived = collectFacts(opts.repo, artifactsDir, queue);
  const provenance = repoProvenance(opts.repo);
  const digest = digestOf(derived);
  const manifestPath = join(opts.repo, ".noru", "audit-pack.yml");
  const derivedPath = join(opts.repo, ".noru", ".cache", "audit-pack.derived.json");

  let wroteSkeleton = false;
  let drift = false;
  let bundle = [];
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
    // --check is the CI path: it answers a question and writes nothing a build would have to clean
    // up. A pack is a deliverable, not a side effect of a gate.
    if (!opts.check) {
      bundle = renderBundle(opts.repo, derived, provenance, readParsedManifest(opts.repo, digest));
    }
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    return 2;
  }

  const unmet = derived.controls.reduce((n, c) => n + c.unmet_evidence_items.length, 0);
  const expired = derived.controls.reduce((n, c) => n + c.expired_evidence.length, 0);
  const populations = derived.artifacts.filter((a) => a.population !== null);
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
    bundle,
    counts: {
      controls: derived.controls.length,
      unmet_evidence_items: unmet,
      expired_evidence: expired,
      artifacts: derived.artifacts.length,
      populations: populations.length,
      upstream_manifests: derived.upstream_manifests.length,
    },
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    const lines = [
      `framework: ${derived.framework_id ?? "(not set)"}` +
        (derived.window ? `, window ${derived.window.from} to ${derived.window.to}` : ""),
      `controls in scope: ${derived.controls.length} (${unmet} unmet expectation(s), ` +
        `${expired} expired record(s))`,
      `artifacts in ${artifactsDir}: ${derived.artifacts.length}` +
        (populations.length > 0 ? `, ${populations.length} of them samplable` : ""),
      `reviewed inputs already in Noru: ${derived.upstream_manifests.length}`,
    ];
    for (const artifact of populations) {
      lines.push(
        `  - ${artifact.file}: ${artifact.population.size} row(s), ` +
          `minimum sample ${artifact.population.minimum_sample}, ` +
          `suggested ${artifact.population.suggested_sample_size} (seed ${artifact.population.seed.slice(0, 12)})`
      );
    }
    lines.push(`derived facts: ${summary.derived_facts}`);
    if (wroteSkeleton) lines.push(`wrote skeleton: ${summary.manifest}`);
    if (bundle.length > 0) lines.push(`pack: ${bundle.length} file(s) under ${BUNDLE_DIR}/`);
    if (drift) lines.push("DRIFT: the manifest does not match the scope and inputs as they are now");
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
