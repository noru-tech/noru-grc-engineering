#!/usr/bin/env node
// Deterministic, offline collector for the threat-model piece (contract requirement 2).
//
// Node built-ins only. Opens no socket. It reads three kinds of local thing:
//
//   *.otm.json                          committed threat models in the Open Threat Model format
//                                       (https://github.com/iriusrisk/OpenThreatModel, CC BY-SA
//                                       4.0): trust zones, components, dataflows, threats and
//                                       mitigations, each with a stable id.
//   .noru/.cache/threat-queue.json      what Noru already holds, written by the skill: the risks this
//                                       piece filed before, and the controls a risk may be linked to.
//   .noru/.cache/<piece>.derived.json   what other pieces found in the same repository, when they
//                                       have run: AI providers and vector stores (ai-inventory),
//                                       datasets and systems (privacy-datamap), and resources with a
//                                       finding (iac-scan). Each one no component of the model names
//                                       is reported as an undeclared component.
//
// One risk per threat on each component or dataflow it is attached to: "SQL injection" on the API
// and on the admin console are two risks with two owners, not one. A risk is keyed on the model's
// project, the element and the threat, never on a position in the file, so reordering the model
// keeps every risk's identity; its citations (JSON pointers into the file) move with it.
//
// Only OTM's JSON form is read: a collector with no dependencies has no YAML parser. A model kept in
// YAML is converted once and committed as JSON; the README says how.
//
// Usage:
//   node collect.mjs [--repo=<path>] [--check] [--output=json|text] [--quiet]
// Exit codes: 0 ok, 1 queue missing, a model invalid, or manifest drifted (--check), 2 usage/IO error.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, realpathSync, writeFileSync,
} from "node:fs";
import { basename, join, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const PIECE = "threat-model";
export const VERSION = "0.17.0";
const GENERATED_BY = `${PIECE}@${VERSION}`;

const HERE = fileURLToPath(new URL(".", import.meta.url));
const VOCAB = JSON.parse(readFileSync(join(HERE, "..", "references", "vocabulary.json"), "utf8"));

const QUEUE_REL = ".noru/.cache/threat-queue.json";
const MODEL_RE = /\.otm\.json$/;
const SKIP_DIRS = new Set([
  ".git", "node_modules", "dist", "build", "out", ".next", ".turbo", "coverage",
  "vendor", "target", ".venv", "venv", "__pycache__", ".noru", ".terraform",
]);
const MAX_MODEL_BYTES = 8 * 1024 * 1024;
const TEXT_MAX = 400;

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

class ModelError extends Error {}

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

/** The commit date of the scanned state: when the model was observed. No clock is read. */
export function observedOn(repo) {
  const value = gitValue(repo, ["log", "-1", "--format=%cs"], "");
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
}

const BY_PATH = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

function skipped(rel) {
  const parts = rel.split("/");
  for (let i = 0; i < parts.length - 1; i += 1) if (SKIP_DIRS.has(parts[i])) return true;
  return false;
}

/** Tracked `*.otm.json` files, or every one on disk where there is no git to ask. */
export function modelFiles(repo) {
  try {
    const raw = execFileSync("git", ["-C", repo, "ls-files", "-z"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      maxBuffer: 64 * 1024 * 1024,
    });
    const paths = raw.split("\0").filter((p) => p !== "");
    if (paths.length > 0) {
      return paths
        .filter((p) => MODEL_RE.test(p) && !skipped(p))
        .filter((p) => {
          try {
            return lstatSync(join(repo, p)).isFile();
          } catch {
            return false;
          }
        })
        .sort(BY_PATH);
    }
  } catch {
    // No git: walk the tree instead.
  }
  const out = [];
  const stack = [repo];
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
      } else if (entry.isFile() && MODEL_RE.test(entry.name)) {
        out.push(relative(repo, full).split(sep).join("/"));
      }
    }
  }
  return out.sort(BY_PATH);
}

/** Lowercase, with every run of other characters as one `-`: how names are compared. */
export function normalized(value) {
  return String(value ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function sha(text) {
  return createHash("sha256").update(text).digest("hex");
}

function clip(text) {
  const value = String(text ?? "").replace(/\s+/g, " ").trim();
  return value.length > TEXT_MAX ? `${value.slice(0, TEXT_MAX - 1)}…` : value;
}

function score(value) {
  return typeof value === "number" && value >= 0 && value <= 100 ? value : null;
}

/**
 * The identity a risk keeps: the threat, and a digest of the project, the element it is attached
 * to and the threat. Never a position in the file.
 */
export function riskKey(projectId, kind, targetId, threatId) {
  const digest = sha([projectId, kind, targetId, threatId].join("#"));
  return `${normalized(threatId).slice(0, 48) || "threat"}.${digest.slice(0, 12)}`;
}

function required(cond, file, message) {
  if (!cond) throw new ModelError(`${file}: ${message}`);
}

/** One OTM model's risks and components, checked for the fields this piece relies on. */
export function readModel(file, text) {
  let doc;
  try {
    doc = JSON.parse(text);
  } catch (error) {
    throw new ModelError(`${file} is not JSON (${error.message}); this piece reads OTM's JSON form`);
  }
  required(doc && typeof doc === "object", file, "is not an OTM document");
  required(typeof doc.otmVersion === "string", file, "has no otmVersion");
  const project = doc.project ?? {};
  required(typeof project.id === "string" && project.id !== "", file, "has no project.id");
  required(typeof project.name === "string", file, "has no project.name");

  const threats = new Map();
  for (const [i, t] of (doc.threats ?? []).entries()) {
    required(typeof t?.id === "string" && t.id !== "", file, `threats[${i}] has no id`);
    required(!threats.has(t.id), file, `threat id '${t.id}' is defined twice`);
    threats.set(t.id, { ...t, pointer: `/threats/${i}` });
  }
  const mitigations = new Map();
  for (const [i, m] of (doc.mitigations ?? []).entries()) {
    required(typeof m?.id === "string" && m.id !== "", file, `mitigations[${i}] has no id`);
    mitigations.set(m.id, m);
  }
  const zones = new Map((doc.trustZones ?? []).map((z) => [z.id, z]));

  const components = [];
  const risks = [];
  const elements = [
    ...(doc.components ?? []).map((c, i) => ({ kind: "component", el: c, pointer: `/components/${i}` })),
    ...(doc.dataflows ?? []).map((d, i) => ({ kind: "dataflow", el: d, pointer: `/dataflows/${i}` })),
  ];
  const seen = new Set();
  for (const { kind, el, pointer } of elements) {
    required(typeof el?.id === "string" && el.id !== "", file, `${pointer} has no id`);
    required(!seen.has(`${kind}:${el.id}`), file, `${kind} id '${el.id}' is defined twice`);
    seen.add(`${kind}:${el.id}`);
    if (kind === "component") {
      const zone = el.parent?.trustZone ?? null;
      components.push({
        id: el.id,
        name: String(el.name ?? el.id),
        type: el.type ?? null,
        trust_zone: zone === null ? null : String(zones.get(zone)?.name ?? zone),
        tokens: [el.id, el.name, ...(Array.isArray(el.tags) ? el.tags : [])]
          .filter((v) => typeof v === "string")
          .map(String),
      });
    }
    for (const [j, ref] of (el.threats ?? []).entries()) {
      const threat = threats.get(ref?.threat);
      required(
        threat !== undefined,
        file,
        `${pointer}/threats/${j} references threat '${ref?.threat}', which the model does not define`
      );
      risks.push({
        key: riskKey(project.id, kind, el.id, threat.id),
        model: file,
        project: project.id,
        target: { kind, id: el.id, name: String(el.name ?? el.id) },
        threat: {
          id: threat.id,
          name: clip(threat.name ?? threat.id),
          description: clip(threat.description ?? ""),
          categories: (threat.categories ?? []).filter((c) => typeof c === "string").sort(),
          cwes: (threat.cwes ?? []).filter((c) => typeof c === "string").sort(),
        },
        state: typeof ref.state === "string" ? ref.state : null,
        likelihood_score: score(threat.risk?.likelihood),
        impact_score: score(threat.risk?.impact),
        mitigations: (ref.mitigations ?? [])
          .filter((m) => typeof m?.mitigation === "string")
          .map((m) => ({
            id: m.mitigation,
            name: clip(mitigations.get(m.mitigation)?.name ?? m.mitigation),
            state: typeof m.state === "string" ? m.state : null,
          }))
          .sort((a, b) => BY_PATH(a.id, b.id)),
        refs: [`${file}#${pointer}/threats/${j}`, `${file}#${threat.pointer}`],
      });
    }
  }
  return {
    model: {
      file,
      otm_version: doc.otmVersion,
      project: { id: project.id, name: String(project.name) },
      digest: `sha256:${sha(text)}`,
    },
    components,
    risks,
  };
}

/** Components other pieces found, from their derived facts when those exist. */
export function readSignals(repo) {
  const sources = [];
  const signals = [];
  const read = (piece) => {
    const path = join(repo, ".noru", ".cache", `${piece}.derived.json`);
    if (!existsSync(path)) return null;
    try {
      const value = JSON.parse(readFileSync(path, "utf8"));
      sources.push(piece);
      return value;
    } catch {
      return null;
    }
  };
  const ai = read("ai-inventory");
  for (const p of ai?.providers ?? []) {
    if (p?.key) signals.push({ kind: "provider", key: String(p.key), source: "ai-inventory", refs: (p.refs ?? []).slice(0, 5) });
  }
  for (const v of ai?.vector_stores ?? []) {
    const key = typeof v === "string" ? v : v?.key;
    if (key) signals.push({ kind: "vector_store", key: String(key), source: "ai-inventory", refs: (v?.refs ?? []).slice(0, 5) });
  }
  const datamap = read("privacy-datamap");
  for (const d of datamap?.datasets ?? []) {
    if (d?.fides_key) signals.push({ kind: "dataset", key: String(d.fides_key), name: d.name ?? null, source: "privacy-datamap", refs: (d.refs ?? [d.ref]).filter(Boolean).slice(0, 5) });
  }
  for (const s of datamap?.systems ?? []) {
    if (s?.fides_key) signals.push({ kind: "system", key: String(s.fides_key), name: s.name ?? null, source: "privacy-datamap", refs: (s.refs ?? [s.ref]).filter(Boolean).slice(0, 5) });
  }
  const iac = read("iac-scan");
  const resources = new Map();
  for (const f of iac?.findings ?? []) {
    if (f?.resource && !resources.has(f.resource)) resources.set(f.resource, f.ref);
  }
  for (const [resource, ref] of resources) {
    signals.push({ kind: "resource", key: String(resource), source: "iac-scan", refs: ref ? [ref] : [] });
  }
  // One entry per kind and key, whichever source saw it first.
  const unique = new Map();
  for (const s of signals) {
    const id = `${s.kind}:${s.key}`;
    if (!unique.has(id)) unique.set(id, s);
  }
  return {
    sources: sources.sort(),
    signals: [...unique.values()].sort((a, b) => BY_PATH(`${a.kind}:${a.key}`, `${b.kind}:${b.key}`)),
  };
}

/**
 * Whether a component of the model names a signal: its id, name or a tag equals the signal's key
 * or name once normalized, or a tag reads `signal:KIND:KEY` exactly. Nothing fuzzier: a guess that
 * a component is the dataset would hide the one that is missing.
 */
export function declaredBy(signal, components) {
  const wanted = new Set([normalized(signal.key), normalized(signal.name)].filter((v) => v !== ""));
  const explicit = `signal:${signal.kind}:${signal.key}`.toLowerCase();
  const hit = components.find((c) =>
    c.tokens.some((t) => t.toLowerCase() === explicit || wanted.has(normalized(t)))
  );
  return hit ? hit.id : null;
}

export function collectFacts(repo, queue) {
  const files = modelFiles(repo);
  const models = [];
  const components = [];
  const risks = [];
  for (const file of files) {
    let text;
    try {
      const full = join(repo, file);
      if (lstatSync(full).size > MAX_MODEL_BYTES) throw new ModelError(`${file} is larger than ${MAX_MODEL_BYTES} bytes`);
      text = readFileSync(full, "utf8");
    } catch (error) {
      if (error instanceof ModelError) throw error;
      throw new ModelError(`cannot read ${file} (${error.code ?? error.message})`);
    }
    const read = readModel(file, text);
    models.push(read.model);
    components.push(...read.components.map((c) => ({ ...c, model: file })));
    risks.push(...read.risks);
  }
  const keys = new Set();
  for (const r of risks) {
    if (keys.has(r.key)) {
      throw new ModelError(
        `two models attach threat '${r.threat.id}' to ${r.target.kind} '${r.target.id}' in project '${r.project}'; give the projects distinct ids`
      );
    }
    keys.add(r.key);
  }
  risks.sort((a, b) => BY_PATH(a.key, b.key));
  const { sources, signals } = readSignals(repo);
  const judged = signals.map((s) => ({ ...s, declared_by: declaredBy(s, components) }));
  const open = queue.open_risks ?? [];
  const produced = new Set(risks.map((r) => r.key));
  return {
    piece: PIECE,
    generated_by: GENERATED_BY,
    models,
    components: components
      .map(({ tokens, ...c }) => {
        void tokens;
        return c;
      })
      .sort((a, b) => BY_PATH(`${a.model}#${a.id}`, `${b.model}#${b.id}`)),
    risks,
    signal_sources: sources,
    signals: judged.filter((s) => s.declared_by !== null),
    undeclared: judged.filter((s) => s.declared_by === null).map(({ declared_by, ...s }) => {
      void declared_by;
      return s;
    }),
    queue_open_risks: open.length,
    queue_no_longer_modelled: open
      .map((row) => String(row.external_id ?? ""))
      .filter((id) => id.startsWith("threat-model:") && !produced.has(id.split(":").slice(2).join(":")))
      .sort(),
  };
}

export function digestOf(derived) {
  // The collector's version is provenance, not a fact about the repository.
  const { generated_by, ...facts } = derived;
  void generated_by;
  return createHash("sha256").update(JSON.stringify(facts, null, 0)).digest("hex");
}

// --- minimal deterministic YAML emitter (same shape as the other pieces' collectors) -------------
const PLAIN_SAFE = /^[A-Za-z0-9_][A-Za-z0-9 _./@-]*$/;
const NUMERIC = /^[-+]?(\.[0-9]+|[0-9][0-9_]*(\.[0-9_]*)?)([eE][-+]?[0-9]+)?$/;

function yamlScalar(value) {
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  const s = String(value);
  if (
    s === "" || !PLAIN_SAFE.test(s) || NUMERIC.test(s) ||
    /^(true|false|null|yes|no|on|off|y|n|~)$/i.test(s)
  ) {
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

/** A 0-100 score as the band the skeleton proposes, or TODO when the model gives none. */
export function band(value, names) {
  if (value === null) return "TODO";
  const i = VOCAB.score_bands.findIndex((upper) => value <= upper);
  return names[i === -1 ? names.length - 1 : i];
}

function interpretation(observed, status, rationale) {
  const horizon = VOCAB.status_horizon_days[status] ?? 180;
  return {
    owner: "TODO@example.com",
    decided_at: observed ?? "TODO-YYYY-MM-DD",
    expires_at: observed === null ? "TODO-YYYY-MM-DD" : addDays(observed, Math.round(horizon / 2)),
    rationale,
  };
}

export function buildSkeleton(derived, provenance, queue, observed) {
  const observedOnValue = observed ?? "TODO-YYYY-MM-DD";
  const risks = derived.risks.map((r) => {
    // A threat the model marks mitigated is proposed as monitored; anything else as identified.
    const status = r.state === "mitigated" ? "monitoring" : "identified";
    return {
      key: r.key,
      model: r.model,
      target: r.target,
      threat: { id: r.threat.id, name: r.threat.name },
      otm: { state: r.state, likelihood: r.likelihood_score, impact: r.impact_score },
      mitigations: r.mitigations,
      title: `${r.threat.name} (${r.target.name})`,
      category: "security",
      likelihood: band(r.likelihood_score, VOCAB.likelihood),
      impact: band(r.impact_score, VOCAB.impact),
      status,
      controls: [],
      observed_on: observedOnValue,
      refs: r.refs,
      interpretation: interpretation(
        observed,
        status,
        "TODO: read the threat in the model, say whether it applies as modelled, how likely and how bad " +
          "it is here, and what is being done about it."
      ),
      needs_review: true,
    };
  });
  const undeclared = derived.undeclared.map((s) => ({
    kind: s.kind,
    key: s.key,
    source: s.source,
    refs: s.refs,
    decision: "TODO",
    interpretation: interpretation(
      observed,
      "out_of_scope",
      "TODO: add this to the threat model as a component (then re-scan), or say why it is out of its scope."
    ),
    needs_review: true,
  }));
  return {
    version: VERSION,
    piece: PIECE,
    source: { ...provenance, derived_digest: digestOf(derived) },
    models: derived.models,
    queue_snapshot: {
      fetched_at: queue.fetched_at,
      via: queue.via,
      open_risks: queue.open_risks ?? [],
      controls: queue.controls ?? [],
    },
    risks,
    undeclared,
  };
}

const SKELETON_HEADER = `# .noru/threat-model.yml — generated by ${GENERATED_BY}
#
# models lists the Open Threat Model files read; risks has one entry per threat on each component or
# dataflow it is attached to. queue_snapshot is what YOUR Noru organization already holds: the risks
# this piece filed before and the controls a risk may be linked to. Re-run :scan to refresh it.
#
# The model says which threats someone thought of. It cannot say whether each applies as modelled,
# how likely or how bad it is here, or who answers for it. Every TODO below is yours.
# The validator enforces:
#   * controls may only name controls present in queue_snapshot
#   * interpretation.expires_at is mandatory and is measured from observed_on
#   * accepting a risk still expires, and needs a real rationale
#   * every undeclared component is either added to the model or decided out of scope
#   * needs_review: true blocks the push
#
# Run:  python3 <plugin>/scripts/validate_manifest.py .noru/threat-model.yml
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
  const queuePath = join(opts.repo, QUEUE_REL);
  if (!existsSync(queuePath)) {
    process.stderr.write(
      `error: no queue at ${queuePath}\n` +
        "hint: this piece works Noru's queue, so :scan asks Noru first. The skill writes this file " +
        "from getOrganizationRisks and getOrganizationControls before running the collector.\n"
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

  let derived;
  try {
    derived = collectFacts(opts.repo, queue);
  } catch (error) {
    if (!(error instanceof ModelError)) throw error;
    process.stderr.write(`error: ${error.message}\n`);
    return 1;
  }
  const provenance = repoProvenance(opts.repo);
  const digest = digestOf(derived);
  const manifestPath = join(opts.repo, ".noru", "threat-model.yml");
  const derivedPath = join(opts.repo, ".noru", ".cache", "threat-model.derived.json");

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
          SKELETON_HEADER + toYaml(buildSkeleton(derived, provenance, queue, observedOn(opts.repo))),
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
      models: derived.models.length,
      components: derived.components.length,
      risks: derived.risks.length,
      signal_sources: derived.signal_sources,
      undeclared: derived.undeclared.length,
      queue_open_risks: derived.queue_open_risks,
      no_longer_modelled: derived.queue_no_longer_modelled.length,
    },
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    const lines = [
      `models: ${derived.models.length} (${derived.models.map((m) => m.file).join(", ") || "none — commit a *.otm.json"})`,
      `risks: ${derived.risks.length} threat(s) on ${derived.components.length} component(s) and their dataflows`,
    ];
    if (derived.signal_sources.length > 0) {
      lines.push(`cross-checked against: ${derived.signal_sources.join(", ")}`);
    }
    for (const s of derived.undeclared) {
      lines.push(`  UNDECLARED ${s.kind} ${s.key} (from ${s.source}): no component of the model names it`);
    }
    if (derived.queue_no_longer_modelled.length > 0) {
      lines.push(
        `${derived.queue_no_longer_modelled.length} risk(s) filed before are no longer in the model — :diff will plan to resolve them`
      );
    }
    lines.push(`derived facts: ${summary.derived_facts}`);
    if (wroteSkeleton) lines.push(`wrote skeleton: ${summary.manifest}`);
    if (drift) lines.push("DRIFT: the manifest does not match the model as it is now");
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
