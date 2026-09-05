#!/usr/bin/env node
// Deterministic, offline collector for the governance-records piece (contract requirement 2).
//
// Node built-ins only. Opens no socket. It reads two local things:
//
//   .noru/.cache/governance-queue.json   what Noru said was unmet, written by the skill from
//                                        getOrganizationControls + getControlContext. This file is
//                                        the queue (contract requirement 9); the collector never
//                                        invents an entry in it.
//   governance/                          the minutes, scope statements, audit plans, reports,
//                                        findings and corrective action plans a human wrote.
//
// What it extracts is the part of a governance document an auditor actually asks about: when it
// happened, who was in the room, what was decided, what actions were assigned and to whom — each
// with the line it came from. Everything it extracts is a suggestion carrying a citation; the
// human confirms it and signs the interpretation block.
//
// Usage:
//   node collect.mjs [--repo=<path>] [--records=<dir>] [--check] [--output=json|text] [--quiet]
// Exit codes: 0 ok, 1 queue missing or manifest drifted (--check), 2 usage/IO error.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, statSync, writeFileSync,
} from "node:fs";
import { basename, extname, join, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const PIECE = "governance-records";
export const VERSION = "0.7.1";
const GENERATED_BY = `${PIECE}@${VERSION}`;

const HERE = fileURLToPath(new URL(".", import.meta.url));
const VOCAB = JSON.parse(readFileSync(join(HERE, "..", "references", "vocabulary.json"), "utf8"));

const DEFAULT_RECORDS_DIR = "governance";
const READABLE_EXTENSIONS = new Set([".md", ".markdown", ".txt"]);

// Words that carry no discriminating signal when matching a document to a catalogue title or to a
// record kind.
const STOPWORDS = new Set([
  "the", "of", "and", "a", "an", "for", "to", "in", "on", "records", "record",
  "evidence", "document", "documents", "final", "signed", "copy", "v1", "v2",
]);

// Section headings this collector understands, normalised to lowercase. Anything else is ignored
// rather than guessed at.
const SECTION_ALIASES = {
  participants: ["attendees", "attendance", "participants", "present", "in attendance"],
  decisions: ["decisions", "resolutions", "conclusions", "decisions taken", "outcome", "outcomes"],
  actions: ["actions", "action items", "actions arising", "follow-up", "follow up", "next steps"],
};

const DATE_FIELDS = {
  occurred_on: ["held on", "held", "date", "meeting date", "issued", "issued on", "effective from"],
  approved_on: ["approved on", "approved", "approval date", "signed on"],
  next_review_due: ["next review", "next review due", "review due", "next review date"],
};
const APPROVER_FIELDS = ["approved by", "approver", "signed by", "chair", "chaired by"];

const DATE_RE = /(\d{4}-\d{2}-\d{2})/;
const HEADING_RE = /^(#{1,6})\s+(.*\S)\s*$/;
const LIST_ITEM_RE = /^[-*+]\s+(.*\S)\s*$/;
const FIELD_RE = /^\**\s*([A-Za-z][A-Za-z /-]*?)\s*\**\s*:\s*(.+?)\s*$/;

const USAGE =
  "usage: collect.mjs [--repo=<path>] [--records=<dir>] [--check] [--output=json|text] [--quiet]\n";

function parseArgs(argv) {
  const opts = { repo: process.cwd(), records: null, check: false, json: false, quiet: false };
  for (const arg of argv) {
    if (arg.startsWith("--repo=")) opts.repo = arg.slice(7);
    else if (arg.startsWith("--records=")) opts.records = arg.slice(10);
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
  return slug === "" ? "record" : slug;
}

function sectionFor(heading) {
  const normalized = heading.toLowerCase().replace(/[^a-z ]+/g, " ").replace(/\s+/g, " ").trim();
  for (const [section, aliases] of Object.entries(SECTION_ALIASES)) {
    if (aliases.includes(normalized)) return section;
  }
  return null;
}

function fieldFor(label) {
  const normalized = label.toLowerCase().replace(/\s+/g, " ").trim();
  for (const [field, aliases] of Object.entries(DATE_FIELDS)) {
    if (aliases.includes(normalized)) return { kind: "date", field };
  }
  if (APPROVER_FIELDS.includes(normalized)) return { kind: "person", field: "approved_by" };
  return null;
}

/**
 * "Alice Andersson (Chair)", "Alice Andersson — Chair", "Alice Andersson, Chair".
 * The name is what matters; the role is a bonus. Anything unparseable stays the whole line, because
 * dropping a participant silently would be worse than keeping an untidy one.
 */
export function parseParticipant(text) {
  let name = text.trim();
  let role = null;

  const parenthesised = name.match(/^(.+?)\s*\(([^()]+)\)\s*$/);
  if (parenthesised) {
    name = parenthesised[1].trim();
    role = parenthesised[2].trim();
  } else {
    const separated = name.match(/^(.+?)\s+(?:[—–]|-{1,2})\s+(.+)$/);
    if (separated) {
      name = separated[1].trim();
      role = separated[2].trim();
    } else {
      const comma = name.match(/^([^,]+),\s*(.+)$/);
      if (comma) {
        name = comma[1].trim();
        role = comma[2].trim();
      }
    }
  }

  const haystack = `${name} ${role ?? ""}`.toLowerCase();
  let attendance = "present";
  if (haystack.includes("apolog")) attendance = "apologies";
  else if (haystack.includes("delegate") || haystack.includes("deputy")) attendance = "delegate";

  // "(apologies)" is an attendance marker, not a job title. Keeping it in `role` would put a word
  // that reads like a position in front of an auditor.
  if (role !== null && /^(apologies|apology|absent|delegate|deputy)$/i.test(role.trim())) {
    role = null;
  }

  return { name, role, attendance };
}

/**
 * "Re-run the access review for the finance tenant (owner: Bo Berg, due: 2026-09-30)".
 * An action nobody owns is not an action, so the owner is extracted but never invented: when the
 * document does not name one, the field comes back null and the validator makes the human fill it.
 */
export function parseAction(text) {
  const owner = text.match(/owner\s*:\s*([^,;)]+)/i);
  const due = text.match(/due\s*(?:by|on)?\s*:\s*(\d{4}-\d{2}-\d{2})/i);
  const status = text.match(/status\s*:\s*([a-z_]+)/i);

  let description = text;
  // Strip a trailing metadata group, parenthesised or dash-separated, once.
  description = description.replace(/\s*\((?:[^()]*(?:owner|due|status)\s*:[^()]*)\)\s*$/i, "");
  description = description.replace(/\s+[—–-]{1,2}\s*(?:owner|due|status)\s*:.*$/i, "");

  const statusValue = status ? status[1].toLowerCase() : null;
  return {
    description: description.trim(),
    owner: owner ? owner[1].trim() : null,
    due_on: due ? due[1] : null,
    status: statusValue && VOCAB.action_status.includes(statusValue) ? statusValue : null,
  };
}

/**
 * Score a document against the bundled record kinds. This is a vocabulary of document SHAPES, not
 * framework content — what a control needs still comes from the queue, never from here.
 */
export function suggestKind(fileName, title) {
  const haystack = new Set([...tokens(fileName), ...tokens(title)]);
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

/**
 * Filename-and-title to unmet-expectation matching, scored against the queue's own words. Ties
 * break on (control_id, item id) so two runs never disagree.
 */
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

/** Everything this collector can honestly read out of one governance document, with line numbers. */
export function parseDocument(text, fileName) {
  const lines = text.split(/\r?\n/);
  const out = {
    title: null,
    occurred_on: null,
    approved_on: null,
    approved_by: null,
    next_review_due: null,
    participants: [],
    decisions: [],
    actions: [],
  };

  let section = null;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const lineNumber = i + 1;
    const trimmed = line.trim();
    if (trimmed === "") continue;

    const heading = trimmed.match(HEADING_RE);
    if (heading) {
      if (out.title === null) out.title = { value: heading[2], line: lineNumber };
      section = sectionFor(heading[2]);
      continue;
    }

    const field = trimmed.match(FIELD_RE);
    if (field) {
      const target = fieldFor(field[1]);
      if (target?.kind === "date") {
        const date = field[2].match(DATE_RE);
        if (date && out[target.field] === null) {
          out[target.field] = { value: date[1], line: lineNumber };
        }
        continue;
      }
      if (target?.kind === "person" && out.approved_by === null) {
        out.approved_by = { value: field[2].trim(), line: lineNumber };
        continue;
      }
    }

    const listItem = trimmed.match(LIST_ITEM_RE);
    if (listItem && section !== null) {
      const value = listItem[1];
      if (section === "participants") {
        out.participants.push({ ...parseParticipant(value), line: lineNumber });
      } else if (section === "decisions") {
        out.decisions.push({ text: value, line: lineNumber });
      } else {
        out.actions.push({ ...parseAction(value), line: lineNumber });
      }
    }
  }

  if (out.title === null) out.title = { value: basename(fileName, extname(fileName)), line: 1 };
  if (out.occurred_on === null) {
    // A date in the filename is the last resort, and it is flagged as such so a reviewer can see
    // where it came from.
    const fromName = basename(fileName).match(DATE_RE);
    if (fromName) out.occurred_on = { value: fromName[1], line: 1, from: "filename" };
  }
  return out;
}

function listDocuments(dir) {
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
      else if (entry.isFile() && READABLE_EXTENSIONS.has(extname(entry.name).toLowerCase())) {
        out.push(full);
      }
    }
  }
  // Sorted, so the result never depends on directory iteration order. Do not remove.
  return out.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

export function scanDocuments(repo, recordsDir, queue) {
  const absolute = join(repo, recordsDir);
  const files = existsSync(absolute) ? listDocuments(absolute) : [];
  return files.map((full) => {
    const rel = relative(repo, full).split(sep).join("/");
    const raw = readFileSync(full, "utf8");
    const parsed = parseDocument(raw, rel);
    const kind = suggestKind(basename(full), parsed.title.value);
    const problems = [];
    if (parsed.occurred_on === null) {
      problems.push(
        "no date found — add a 'Held on: YYYY-MM-DD' or 'Issued: YYYY-MM-DD' line, or put the " +
          "date in the filename"
      );
    }
    if (kind === null) {
      problems.push("filename and title match no known record kind; set `kind` by hand");
    } else if (VOCAB.meeting_kinds.includes(kind.kind) && parsed.participants.length === 0) {
      problems.push(
        `looks like ${kind.kind} but no attendees section was found; minutes with nobody in them ` +
          "assert nothing an auditor can test"
      );
    }
    return {
      file: rel,
      sha256: createHash("sha256").update(readFileSync(full)).digest("hex"),
      size_bytes: statSync(full).size,
      key: slugKey(basename(full, extname(full))),
      parsed,
      suggested_kind: kind,
      suggested_matches: suggestMatches(`${basename(full)} ${parsed.title.value}`, queue),
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

function refsFor(doc) {
  const refs = [`${doc.file}:${doc.parsed.title.line}`];
  for (const field of ["occurred_on", "approved_on", "approved_by", "next_review_due"]) {
    if (doc.parsed[field]) refs.push(`${doc.file}:${doc.parsed[field].line}`);
  }
  for (const participant of doc.parsed.participants) refs.push(`${doc.file}:${participant.line}`);
  for (const decision of doc.parsed.decisions) refs.push(`${doc.file}:${decision.line}`);
  for (const action of doc.parsed.actions) refs.push(`${doc.file}:${action.line}`);
  // Unique, and sorted by line number so a reviewer reads them in document order.
  return [...new Set(refs)].sort((a, b) => {
    const la = Number(a.slice(a.lastIndexOf(":") + 1));
    const lb = Number(b.slice(b.lastIndexOf(":") + 1));
    return la - lb;
  });
}

export function buildSkeleton(derived, provenance, queue) {
  const records = derived.documents.map((doc) => {
    const best = doc.suggested_matches[0];
    return {
      key: doc.key,
      kind: doc.suggested_kind ? doc.suggested_kind.kind : "TODO_set_the_record_kind",
      title: doc.parsed.title.value,
      occurred_on: doc.parsed.occurred_on ? doc.parsed.occurred_on.value : "TODO-YYYY-MM-DD",
      ...(doc.parsed.approved_on ? { approved_on: doc.parsed.approved_on.value } : {}),
      ...(doc.parsed.approved_by ? { approved_by: doc.parsed.approved_by.value } : {}),
      ...(doc.parsed.next_review_due
        ? { next_review_due: doc.parsed.next_review_due.value }
        : {}),
      document: { file: doc.file, sha256: doc.sha256, size_bytes: doc.size_bytes },
      participants: doc.parsed.participants.map((p) => ({
        name: p.name,
        ...(p.role ? { role: p.role } : {}),
        attendance: p.attendance,
      })),
      decisions: doc.parsed.decisions.map((d) => d.text),
      actions: doc.parsed.actions.map((a) => ({
        description: a.description,
        owner: a.owner ?? "TODO: name the person who owns this action",
        ...(a.due_on ? { due_on: a.due_on } : {}),
        ...(a.status ? { status: a.status } : {}),
      })),
      refs: refsFor(doc),
      control_mappings: best
        ? [{ control_id: best.control_id, evidence_item_ids: [best.evidence_item_id] }]
        : [],
      interpretation: {
        owner: "TODO@example.com",
        decided_at: "TODO-YYYY-MM-DD",
        rationale: best
          ? `TODO: confirm this record satisfies "${best.evidence_item_title}" (match score ${best.score}) and say why`
          : "TODO: no queue item matched this document — say which expectation it satisfies and why",
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
    records,
  };
}

const SKELETON_HEADER = `# .noru/governance-records.yml — generated by ${GENERATED_BY}
#
# queue_snapshot is what YOUR Noru organization said was unmet. It is not shipped by this plugin
# and it is not editable guesswork: re-run :scan to refresh it.
#
# Everything below the queue was read out of the documents in your governance directory, and every
# TODO is a decision a person has to make and sign for. The validator enforces:
#   * control_mappings may only reference controls and evidence items present in queue_snapshot
#   * minutes-shaped records must name the people who were in the room
#   * every action must name an owner
#   * a record with no interpretation.expires_at must carry next_review_due instead
#   * needs_review: true blocks the push
#
# Run:  python3 <plugin>/scripts/validate_manifest.py .noru/governance-records.yml
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

  const queuePath = join(opts.repo, ".noru", ".cache", "governance-queue.json");
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

  const recordsDir = opts.records ?? DEFAULT_RECORDS_DIR;
  const documents = scanDocuments(opts.repo, recordsDir, queue);
  const unmet = (queue.controls ?? []).reduce(
    (sum, c) => sum + (c.unmet_evidence_items ?? []).length,
    0
  );
  const derived = {
    piece: PIECE,
    generated_by: GENERATED_BY,
    records_dir: recordsDir,
    queue_controls: (queue.controls ?? []).length,
    queue_unmet_items: unmet,
    documents,
  };

  const provenance = repoProvenance(opts.repo);
  const digest = digestOf(derived);
  const manifestPath = join(opts.repo, ".noru", "governance-records.yml");
  const derivedPath = join(opts.repo, ".noru", ".cache", "governance-records.derived.json");

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

  const flagged = documents.filter((d) => d.problems.length > 0);
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
      documents: documents.length,
      flagged: flagged.length,
      unmatched: documents.filter((d) => d.suggested_matches.length === 0).length,
      participants: documents.reduce((n, d) => n + d.parsed.participants.length, 0),
      actions: documents.reduce((n, d) => n + d.parsed.actions.length, 0),
    },
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    const lines = [
      `queue: ${derived.queue_controls} control(s), ${unmet} unmet expectation(s)`,
      `documents in ${recordsDir}: ${documents.length}`,
    ];
    for (const doc of documents) {
      const best = doc.suggested_matches[0];
      lines.push(
        `  ${doc.problems.length > 0 ? "!" : "-"} ${doc.file}` +
          `  [${doc.suggested_kind ? doc.suggested_kind.kind : "kind unknown"}]` +
          (best
            ? `  ->  ${best.control_id} / ${best.evidence_item_id} "${best.evidence_item_title}" (score ${best.score})`
            : "  ->  no queue item matched this document")
      );
      for (const problem of doc.problems) lines.push(`      ${problem}`);
    }
    lines.push(`derived facts: ${summary.derived_facts}`);
    if (wroteSkeleton) lines.push(`wrote skeleton: ${summary.manifest}`);
    if (drift) {
      lines.push("DRIFT: the manifest does not match the documents and queue as they are now");
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
