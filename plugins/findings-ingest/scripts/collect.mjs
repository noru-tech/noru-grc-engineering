#!/usr/bin/env node
// Deterministic, offline collector for the findings-ingest piece (contract requirement 2).
//
// Node built-ins only. Opens no socket. It reads three local things:
//
//   .noru/findings-ingest.inputs.json   which scanner reports to read, committed. Every input says
//                                       what format it is, what class of scanner wrote it, and that
//                                       its results do NOT reach a forge alert store.
//   the reports it names                SARIF 2.1.0, or OSV-Scanner JSON, written by your CI before
//                                       this runs. Usually under .noru/.cache/, never committed.
//   .noru/.cache/findings-queue.json    what this piece already has open in Noru, plus the
//                                       organization's own assets, risks and treatment due dates,
//                                       written by the skill. The open set is the half of the queue a
//                                       repository cannot know: a result that stopped appearing has a
//                                       finding somewhere that should be closed.
//
// Why the forge alert store matters: a result a scanner uploads to the forge's own alert store
// (code scanning, dependency alerts, a merge request security report) is already visible to
// anything that imports that store. If such an import feeds the same Noru organization, ingesting
// the same report here files every result twice, under two sources that never reconcile. This piece
// is for the results nothing else imports, so an input that says otherwise is refused.
//
// A finding is a POINTER, never a copy. No result message, snippet or matched text is ever read
// into the derived facts or the manifest: a secret scanner's message can contain the secret, and a
// collector that quoted it would commit it and then publish it. Titles come from the rule's own
// metadata; the citation is `file:line`.
//
// Identity: a finding is keyed on the input, the rule, the file and the logical location (for
// SARIF), or on the input, the advisory and the package (for OSV). Never on a line number, and
// never on a fingerprint the scanner computed from the line's content, because both change when the
// code around a problem moves and neither change means the problem did.
//
// Usage:
//   node collect.mjs [--repo=<path>] [--check] [--output=json|text] [--quiet]
// Exit codes: 0 ok, 1 inputs/queue missing, an input refused or unreadable, or manifest drifted
// (--check), 2 usage/IO error.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync, mkdirSync, readFileSync, realpathSync, statSync, writeFileSync,
} from "node:fs";
import { basename, isAbsolute, join, posix, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const PIECE = "findings-ingest";
export const VERSION = "0.15.0";
const GENERATED_BY = `${PIECE}@${VERSION}`;

// The `source` this piece's findings carry in Noru. A finding is keyed on (source, externalId), so
// this string is part of every identity the piece owns and changing it orphans everything it wrote.
export const FINDING_SOURCE = "findings-ingest";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const VOCAB = JSON.parse(readFileSync(join(HERE, "..", "references", "vocabulary.json"), "utf8"));

const INPUTS_REL = ".noru/findings-ingest.inputs.json";
const QUEUE_REL = ".noru/.cache/findings-queue.json";
const INPUT_ID_RE = /^[a-z0-9][a-z0-9-]*$/;
// A report large enough to be this is a report nobody is going to review finding by finding.
const MAX_REPORT_BYTES = 64 * 1024 * 1024;
// Enough citations to find every place, few enough that one noisy rule cannot bury the manifest.
const MAX_REFS = 20;
const SEVERITY_RANK = { critical: 0, high: 1, medium: 2, low: 3 };
const TITLE_MAX = 200;

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

class InputError extends Error {}

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
 * The day the results were observed: the commit date of the state that was scanned, not the day the
 * scanner happened to run. A report's own timestamps are left alone on purpose — reading them would
 * make two runs over one commit disagree, and the commit is what the scanner was looking at.
 */
export function observedOn(repo) {
  const value = gitValue(repo, ["log", "-1", "--format=%cs"], "");
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
}

// --- the inputs file --------------------------------------------------------------------------- //

/**
 * The declared inputs, checked before any report is opened. A refusal here is a refusal of the
 * whole run: half an ingest would close the other half's findings.
 */
export function readInputs(repo) {
  const path = join(repo, INPUTS_REL);
  if (!existsSync(path)) {
    throw new InputError(
      `no inputs file at ${path}\n` +
        "hint: commit .noru/findings-ingest.inputs.json naming each scanner report to read, its " +
        "format (sarif or osv), its class, and `reaches_forge_alert_store: false`"
    );
  }
  let doc;
  try {
    doc = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new InputError(`${path} is not readable JSON (${error.message})`);
  }
  const rows = doc?.inputs;
  if (!Array.isArray(rows) || rows.length === 0) {
    throw new InputError(`${INPUTS_REL} declares no inputs — name at least one report`);
  }
  const seen = new Set();
  return rows.map((row, i) => {
    const where = `${INPUTS_REL} inputs[${i}]`;
    if (row === null || typeof row !== "object") throw new InputError(`${where} is not an object`);
    const { id, path: rel, format } = row;
    const klass = row.class;
    if (typeof id !== "string" || !INPUT_ID_RE.test(id)) {
      throw new InputError(`${where}.id '${id}' must be lowercase letters, digits and '-'`);
    }
    if (seen.has(id)) throw new InputError(`${where}.id '${id}' is declared twice`);
    seen.add(id);
    if (!VOCAB.format.includes(format)) {
      throw new InputError(`${where}.format '${format}' is not one of ${VOCAB.format.join(", ")}`);
    }
    if (!VOCAB.class.includes(klass)) {
      throw new InputError(`${where}.class '${klass}' is not one of ${VOCAB.class.join(", ")}`);
    }
    if (row.reaches_forge_alert_store !== false) {
      throw new InputError(
        `${where} ('${id}') does not declare \`reaches_forge_alert_store: false\`. A report that ` +
          "is uploaded to a forge alert store is already visible to whatever imports that store, " +
          "and ingesting it here as well would file every one of its results twice. Ingest only " +
          "the results nothing else brings into Noru"
      );
    }
    if (typeof rel !== "string" || rel === "" || isAbsolute(rel) || rel.split("/").includes("..")) {
      throw new InputError(`${where}.path '${rel}' must be a path inside the repository`);
    }
    return { id, path: rel, format, class: klass, reaches_forge_alert_store: false };
  });
}

// --- paths ------------------------------------------------------------------------------------- //

function isRepoFile(repo, rel) {
  try {
    return statSync(join(repo, rel)).isFile();
  } catch {
    return false;
  }
}

/**
 * A path a scanner reported, as a file in this repository, or null when it is not one. Scanners
 * write absolute paths of wherever they ran, so an absolute path is matched against this checkout
 * first and then by its longest suffix that exists here: the same report produced on a CI runner and
 * read on a laptop has to cite the same file. A relative path is read from the repository root, so a
 * scanner run from a subdirectory has to be run from the root instead.
 */
export function toRepoPath(repo, raw) {
  if (typeof raw !== "string" || raw === "") return null;
  let value = raw;
  if (value.startsWith("file://")) {
    try {
      value = fileURLToPath(value);
    } catch {
      return null;
    }
  } else if (/%[0-9A-Fa-f]{2}/.test(value)) {
    try {
      value = decodeURIComponent(value);
    } catch {
      // Not percent-encoding after all; use it as written.
    }
  }
  value = value.split("\\").join("/");
  if (!value.startsWith("/") && !/^[A-Za-z]:\//.test(value)) {
    const clean = posix.normalize(value).replace(/^\.\//, "");
    if (clean === "." || clean === ".." || clean.startsWith("../")) return null;
    return isRepoFile(repo, clean) ? clean : null;
  }
  let root = repo;
  try {
    root = realpathSync(repo);
  } catch {
    // Keep the path as given.
  }
  const rootPosix = root.split(sep).join("/");
  if (value.startsWith(`${rootPosix}/`)) {
    const inside = value.slice(rootPosix.length + 1);
    return isRepoFile(repo, inside) ? inside : null;
  }
  const parts = value.split("/").filter((part) => part !== "");
  for (let i = 1; i < parts.length; i += 1) {
    const candidate = parts.slice(i).join("/");
    if (isRepoFile(repo, candidate)) return candidate;
  }
  return null;
}

// --- identity ---------------------------------------------------------------------------------- //

function slugPart(value, max) {
  const text = String(value ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[^a-z0-9]+/, "")
    .replace(/-+$/g, "");
  return (text || "x").slice(0, max).replace(/[._-]+$/, "") || "x";
}

/**
 * The identity a finding keeps across scans. The readable half is the input and the rule; the
 * digest is over the exact strings, so two rules that normalize to one slug stay two findings.
 */
export function findingKey(inputId, rule, ...identity) {
  const digest = createHash("sha256")
    .update([inputId, rule, ...identity.map((part) => part ?? "")].join("#"))
    .digest("hex");
  return `${inputId}.${slugPart(rule, 60)}.${digest.slice(0, 12)}`;
}

export function severityFromScore(raw) {
  const score = typeof raw === "number" ? raw : Number.parseFloat(String(raw ?? ""));
  if (!Number.isFinite(score) || score <= 0) return null;
  for (const band of VOCAB.score_bands) if (score >= band.from) return band.severity;
  return null;
}

function clip(text) {
  const value = String(text ?? "").replace(/\s+/g, " ").trim();
  return value.length > TITLE_MAX ? `${value.slice(0, TITLE_MAX - 1)}…` : value;
}

function moreSevere(a, b) {
  if (a === null) return b;
  if (b === null) return a;
  return SEVERITY_RANK[a] <= SEVERITY_RANK[b] ? a : b;
}

// --- SARIF 2.1.0 -------------------------------------------------------------------------------- //

function ruleOf(run, result) {
  const rules = run?.tool?.driver?.rules ?? [];
  if (Number.isInteger(result.ruleIndex) && rules[result.ruleIndex]) return rules[result.ruleIndex];
  const id = result.ruleId ?? result.rule?.id;
  return rules.find((rule) => rule?.id === id) ?? null;
}

function locationOf(run, result) {
  const location = (result.locations ?? [])[0] ?? {};
  const physical = location.physicalLocation ?? {};
  let uri = physical.artifactLocation?.uri;
  const index = physical.artifactLocation?.index;
  if (!uri && Number.isInteger(index)) uri = run?.artifacts?.[index]?.location?.uri;
  const logical = (location.logicalLocations ?? [])[0] ?? {};
  const line = physical.region?.startLine;
  return {
    uri: typeof uri === "string" ? uri : null,
    line: Number.isInteger(line) && line > 0 ? line : null,
    logical: logical.fullyQualifiedName ?? logical.name ?? null,
  };
}

function suppressedInSource(result) {
  const list = result.suppressions;
  if (!Array.isArray(list) || list.length === 0) return false;
  // SARIF: a suppression with no status, or status "accepted", is in force; "underReview" and
  // "rejected" are not.
  return list.some((s) => s?.status === undefined || s?.status === "accepted");
}

export function readSarif(repo, input, doc) {
  if (!doc || !Array.isArray(doc.runs)) {
    throw new InputError(`${input.path} is not SARIF: it has no \`runs\` array`);
  }
  const findings = [];
  const tools = new Set();
  const skipped = { not_a_failure: 0, absent_from_baseline: 0, uncited: 0, outside_repository: 0 };
  for (const run of doc.runs) {
    const driver = run?.tool?.driver ?? {};
    const tool = typeof driver.name === "string" && driver.name !== "" ? driver.name : "unknown";
    tools.add(`${tool}${driver.version ? ` ${driver.version}` : driver.semanticVersion ? ` ${driver.semanticVersion}` : ""}`);
    for (const result of run?.results ?? []) {
      if (!result || typeof result !== "object") continue;
      if ((result.kind ?? "fail") !== "fail") {
        skipped.not_a_failure += 1;
        continue;
      }
      if (result.baselineState === "absent") {
        skipped.absent_from_baseline += 1;
        continue;
      }
      const rule = ruleOf(run, result);
      const ruleId = result.ruleId ?? result.rule?.id ?? rule?.id;
      const where = locationOf(run, result);
      if (!ruleId || where.uri === null) {
        skipped.uncited += 1;
        continue;
      }
      const file = toRepoPath(repo, where.uri);
      if (file === null) {
        skipped.outside_repository += 1;
        continue;
      }
      const score = rule?.properties?.["security-severity"];
      const level = result.level ?? rule?.defaultConfiguration?.level ?? "warning";
      findings.push({
        key: findingKey(input.id, ruleId, tool, file, where.logical),
        input: input.id,
        tool,
        rule: ruleId,
        class: input.class,
        category: VOCAB.class_category[input.class],
        scanner_severity: severityFromScore(score) ?? VOCAB.sarif_level[level] ?? null,
        // The rule's own description, written by whoever wrote the rule — never the result's
        // message, which a secret scanner fills with what it found.
        title: clip(rule?.shortDescription?.text || rule?.name || ruleId),
        ...(typeof rule?.helpUri === "string" ? { help: rule.helpUri } : {}),
        file,
        location: where.logical,
        refs: [where.line === null ? file : `${file}:${where.line}`],
        suppressed_in_source: suppressedInSource(result),
      });
    }
  }
  return { findings, tools: [...tools].sort(), skipped };
}

// --- OSV-Scanner JSON --------------------------------------------------------------------------- //

function advisoryGroups(pkg) {
  const records = new Map((pkg.vulnerabilities ?? []).map((v) => [v?.id, v]));
  if (Array.isArray(pkg.groups) && pkg.groups.length > 0) {
    return pkg.groups.map((group) => ({ ids: group.ids ?? [], aliases: group.aliases ?? [], max: group.max_severity, records }));
  }
  return [...records.values()].map((v) => ({ ids: [v.id], aliases: v.aliases ?? [], max: undefined, records }));
}

export function readOsv(repo, input, doc) {
  if (!doc || !Array.isArray(doc.results)) {
    throw new InputError(`${input.path} is not OSV-Scanner JSON: it has no \`results\` array`);
  }
  const findings = [];
  const skipped = { not_a_failure: 0, absent_from_baseline: 0, uncited: 0, outside_repository: 0 };
  for (const result of doc.results) {
    const raw = result?.source?.path;
    const file = toRepoPath(repo, raw);
    for (const pkg of result?.packages ?? []) {
      const p = pkg?.package ?? {};
      for (const group of advisoryGroups(pkg)) {
        const ids = [...new Set(group.ids.filter((id) => typeof id === "string" && id !== ""))];
        if (ids.length === 0) continue;
        if (typeof raw !== "string" || raw === "") {
          skipped.uncited += 1;
          continue;
        }
        if (file === null) {
          skipped.outside_repository += 1;
          continue;
        }
        // The advisory a group is known by: one that is a record in its own right, so a new alias
        // published later does not change which id the finding carries.
        const recorded = ids.filter((id) => group.records.has(id)).sort();
        const primary = (recorded.length > 0 ? recorded : [...ids].sort())[0];
        const record = group.records.get(primary) ?? {};
        const dbSeverity = record.database_specific?.severity;
        const ecosystem = typeof p.ecosystem === "string" ? p.ecosystem : "";
        const name = typeof p.name === "string" ? p.name : "";
        findings.push({
          key: findingKey(input.id, primary, file, `${ecosystem}/${name}`),
          input: input.id,
          tool: "osv-scanner",
          rule: primary,
          class: input.class,
          category: VOCAB.class_category[input.class],
          scanner_severity:
            severityFromScore(group.max) ?? VOCAB.advisory_severity[String(dbSeverity ?? "").toUpperCase()] ?? null,
          title: clip(record.summary ? `${name}: ${record.summary}` : `${primary} in ${name}`),
          help: `https://osv.dev/vulnerability/${encodeURIComponent(primary)}`,
          file,
          location: null,
          package: { ecosystem, name, version: typeof p.version === "string" ? p.version : null },
          aliases: [...new Set([...ids, ...group.aliases])].filter((id) => id !== primary).sort(),
          refs: [file],
          suppressed_in_source: false,
        });
      }
    }
  }
  return { findings, tools: ["osv-scanner"], skipped };
}

// --- the whole collection ---------------------------------------------------------------------- //

function readReport(repo, input) {
  const full = join(repo, input.path);
  let size;
  try {
    size = statSync(full).size;
  } catch {
    // A missing report is not a report with no results. Treating it as one would close every finding
    // the input ever produced the first time a scanner step failed.
    throw new InputError(
      `input '${input.id}' names ${input.path}, which does not exist — run the scanner before the ` +
        "collector. A missing report is never read as an empty one"
    );
  }
  if (size > MAX_REPORT_BYTES) {
    throw new InputError(`input '${input.id}' at ${input.path} is larger than ${MAX_REPORT_BYTES} bytes`);
  }
  try {
    return JSON.parse(readFileSync(full, "utf8"));
  } catch (error) {
    throw new InputError(`input '${input.id}' at ${input.path} is not readable JSON (${error.message})`);
  }
}

function merge(into, finding) {
  const refs = [...new Set([...into.refs, ...finding.refs])].sort();
  into.refs = refs;
  into.scanner_severity = moreSevere(into.scanner_severity, finding.scanner_severity);
  // A result is suppressed only if every occurrence of it is; one live occurrence is a live finding.
  into.suppressed_in_source = into.suppressed_in_source && finding.suppressed_in_source;
}

export function collectFacts(repo, inputs, queue) {
  const byKey = new Map();
  const coverage = [];
  for (const input of inputs) {
    const doc = readReport(repo, input);
    const read = input.format === "sarif" ? readSarif(repo, input, doc) : readOsv(repo, input, doc);
    coverage.push({ input: input.id, tools: read.tools, results: read.findings.length, skipped: read.skipped });
    for (const finding of read.findings) {
      const existing = byKey.get(finding.key);
      if (existing) merge(existing, finding);
      else byKey.set(finding.key, finding);
    }
  }

  const findings = [...byKey.values()].map((finding) => {
    const refs = finding.refs;
    return {
      ...finding,
      refs: refs.slice(0, MAX_REFS),
      ...(refs.length > MAX_REFS ? { refs_total: refs.length } : {}),
    };
  });
  findings.sort((a, b) => {
    const rank = (SEVERITY_RANK[a.scanner_severity] ?? 9) - (SEVERITY_RANK[b.scanner_severity] ?? 9);
    if (rank !== 0) return rank;
    return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
  });

  const open = queue.open_findings ?? [];
  const produced = new Set(findings.map((f) => f.key));
  return {
    piece: PIECE,
    generated_by: GENERATED_BY,
    finding_source: FINDING_SOURCE,
    inputs,
    // Which tool versions wrote the reports, and what was read but not filed. Out of the digest: a
    // scanner upgrade that reports the same results is the same repository, and must not read as
    // drift.
    coverage: { inputs: coverage },
    queue_open_findings: open.length,
    // A finding Noru still holds open that no input reported this time. Surfaced in the scan output
    // so the reviewer meets it before the plan does.
    queue_no_longer_reproducing: open
      .map((row) => String(row.external_id ?? ""))
      .filter((id) => id !== "" && !produced.has(id.split(":").slice(1).join(":")))
      .sort(),
    findings,
  };
}

export function digestOf(derived) {
  // `generated_by` and `coverage` are deliberately NOT hashed: this digest answers whether what the
  // scanners found has changed since the manifest was written, and neither the version of this
  // collector nor the version of a scanner is a finding.
  const { generated_by, coverage, ...facts } = derived;
  void generated_by;
  void coverage;
  return createHash("sha256").update(JSON.stringify(facts, null, 0)).digest("hex");
}

// --- minimal deterministic YAML emitter (same shape as the other pieces' collectors) -------------
const PLAIN_SAFE = /^[A-Za-z0-9_][A-Za-z0-9 _./@-]*$/;
// A package version such as 1.0 is a float to a YAML loader. Unlike the other pieces, this one
// writes strings it did not choose, so anything a loader would read as a number is quoted.
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

export function buildSkeleton(derived, provenance, queue, observed) {
  const observedOnValue = observed ?? "TODO-YYYY-MM-DD";
  const findings = derived.findings.map((finding) => {
    // A result suppressed in the source is a decision somebody already made and never wrote down
    // anywhere Noru can see. It is proposed as accepted, so the validator makes them write it down.
    const status = finding.suppressed_in_source ? "accepted" : "open";
    const horizon = VOCAB.status_horizon_days[status];
    return {
      key: finding.key,
      input: finding.input,
      tool: finding.tool,
      rule: finding.rule,
      class: finding.class,
      severity: finding.scanner_severity ?? "TODO",
      category: finding.category,
      status,
      title: finding.title,
      file: finding.file,
      location: finding.location,
      ...(finding.package ? { package: finding.package } : {}),
      ...(finding.aliases && finding.aliases.length > 0 ? { aliases: finding.aliases } : {}),
      observed_on: observedOnValue,
      refs: finding.refs,
      ...(finding.suppressed_in_source ? { suppressed_in_source: true } : {}),
      interpretation: {
        owner: "TODO@example.com",
        decided_at: observedOnValue,
        expires_at: observed === null ? "TODO-YYYY-MM-DD" : addDays(observed, Math.round(horizon / 2)),
        rationale: finding.suppressed_in_source
          ? "TODO: this result is suppressed in the source. Say who decided to leave it, why it is " +
            "safe here, and what would make that stop being true."
          : "TODO: open the cited location, say whether this is real in this environment and what " +
            "happens next. If it is not real, set status to false_positive and say what makes it one.",
      },
      needs_review: true,
    };
  });

  return {
    version: VERSION,
    piece: PIECE,
    source: { ...provenance, derived_digest: digestOf(derived) },
    // No `links` block: there is nothing to link until somebody picks a risk or an asset from the
    // queue snapshot, and an empty flow mapping is the one shape the bundled YAML loader reads as a
    // string.
    inputs: derived.inputs,
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

const SKELETON_HEADER = `# .noru/findings-ingest.yml — generated by ${GENERATED_BY}
#
# inputs is what .noru/findings-ingest.inputs.json declared: each report, its format and class, and
# that its results do not reach a forge alert store. queue_snapshot is what YOUR Noru organization
# already holds: the findings this piece has open, the assets and risks a finding may be filed
# against, and any due date a risk's treatment carries. Neither is shipped by this plugin — re-run
# :scan to refresh them.
#
# A scanner can say which rule fired where. It cannot say whether the result is real in your
# environment, how bad it is here, or who is going to act on it. Every TODO below is yours.
#
# To file a whole class against one risk, or every finding against one asset, add a top-level block:
#   links:
#     asset_external_id: <an external_id from queue_snapshot.assets>
#     risk_by_class:
#       dependency: <an id from queue_snapshot.risks>
#
# The validator enforces:
#   * links.asset_external_id, links.risk_by_class, asset_external_id and risk_id may only name
#     things present in queue_snapshot
#   * interpretation.expires_at is mandatory and is measured from observed_on, not from decided_at
#   * accepting a finding or calling it a false positive still expires, and needs a real rationale
#   * a finding never carries a due date of its own: a deadline is the linked risk's treatment
#   * needs_review: true blocks the push
#
# No result message, snippet or matched text appears in this file by design: a secret scanner's
# message can hold the secret. Read the citation instead.
#
# Run:  python3 <plugin>/scripts/validate_manifest.py .noru/findings-ingest.yml
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
        "from getSecurityFindings + getOrganizationAssets + getOrganizationRisks (and " +
        "getRiskTreatments for due dates) before running the collector. Without it a re-scan " +
        "cannot tell which findings should now be closed.\n"
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
    derived = collectFacts(opts.repo, readInputs(opts.repo), queue);
  } catch (error) {
    if (!(error instanceof InputError)) throw error;
    process.stderr.write(`error: ${error.message}\n`);
    return 1;
  }
  const provenance = repoProvenance(opts.repo);
  const digest = digestOf(derived);
  const manifestPath = join(opts.repo, ".noru", "findings-ingest.yml");
  const derivedPath = join(opts.repo, ".noru", ".cache", "findings-ingest.derived.json");

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
    const label = finding.scanner_severity ?? "unrated";
    bySeverity[label] = (bySeverity[label] ?? 0) + 1;
  }
  const skipped = { not_a_failure: 0, absent_from_baseline: 0, uncited: 0, outside_repository: 0 };
  for (const row of derived.coverage.inputs) {
    for (const [reason, count] of Object.entries(row.skipped)) skipped[reason] += count;
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
      inputs: derived.inputs.length,
      findings: derived.findings.length,
      by_severity: bySeverity,
      suppressed_in_source: derived.findings.filter((f) => f.suppressed_in_source).length,
      skipped,
      queue_open_findings: derived.queue_open_findings,
      no_longer_reproducing: derived.queue_no_longer_reproducing.length,
    },
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    const perSeverity = [...VOCAB.severity, "unrated"]
      .filter((s) => bySeverity[s])
      .map((s) => `${bySeverity[s]} ${s}`)
      .join(", ");
    const lines = [
      ...derived.coverage.inputs.map(
        (row) => `input ${row.input}: ${row.results} result(s) from ${row.tools.join(", ") || "no tool"}`
      ),
      `findings: ${derived.findings.length}${perSeverity === "" ? "" : ` (${perSeverity})`}`,
    ];
    for (const finding of derived.findings) {
      lines.push(
        `  ${(finding.scanner_severity ?? "unrated").padEnd(8)} ${finding.rule}`,
        `      ${finding.refs[0]}${finding.location ? `  (${finding.location})` : ""}`
      );
    }
    const dropped = Object.entries(skipped).filter(([, n]) => n > 0);
    if (dropped.length > 0) {
      lines.push(`not filed: ${dropped.map(([reason, n]) => `${n} ${reason.replace(/_/g, " ")}`).join(", ")}`);
    }
    if (derived.queue_no_longer_reproducing.length > 0) {
      lines.push(
        `${derived.queue_no_longer_reproducing.length} finding(s) open in Noru were not reported ` +
          "this time — :diff will plan to close them"
      );
    }
    lines.push(`derived facts: ${summary.derived_facts}`);
    if (wroteSkeleton) lines.push(`wrote skeleton: ${summary.manifest}`);
    if (drift) lines.push("DRIFT: the manifest does not match what the scanners report now");
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
