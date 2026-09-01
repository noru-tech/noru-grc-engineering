#!/usr/bin/env node
// Deterministic, offline collector for the privacy-datamap piece (contract requirement 2).
//
// Node built-ins only. Opens no socket. Same repository state in, byte-identical derived output —
// scripts/contract_test.py runs this twice and diffs the result, so a timestamp or an unsorted
// directory listing anywhere in here will fail the build.
//
// Usage: node collect.mjs [--repo=<path>] [--check] [--output=json|text] [--quiet]
// Exit codes: 0 ok, 1 drift against the manifest (--check), 2 usage/IO error.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, realpathSync, writeFileSync,
} from "node:fs";
import { basename, join, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { toFideslang } from "./lib/fides.mjs";

export const PIECE = "privacy-datamap";
export const VERSION = "0.4.0";
const GENERATED_BY = `${PIECE}@${VERSION}`;

const HERE = fileURLToPath(new URL(".", import.meta.url));
const TABLE = JSON.parse(
  readFileSync(join(HERE, "..", "references", "classification.json"), "utf8"),
);
// A schema file is small. Anything past this is generated data or a checked-in dump, and
// reading it would cost more than it could ever tell us.
const MAX_BYTES = 1_000_000;

// Directories that are in the repository but are not the repository: a checked-in vendor/ or dist/
// describes a dependency's schema or a build's output, not anything this codebase decided to store.
// This is a second filter on top of git's answer below, not the primary one — a denylist can only
// ever name the directories its author has already seen.
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

// --------------------------------------------------------------------------------------------- //
// Which files are in scope. git decides, wherever there is a git to ask.
//
// `:diff` and CI mode both compare a committed manifest against a fresh scan, and CI scans an
// `actions/checkout` — tracked files, and nothing else. A developer scans a working tree, which
// holds whatever else they keep in it: scratch checkouts, worktrees, unpacked archives, generated
// fixtures. Walking the working tree therefore produces drift nobody can resolve, because the
// manifest can match one of those two environments or the other and never both — and every extra
// dataset it adds is keyed off a path that is not in the repository at all.
//
// `git ls-files` is the set CI checks out, and it honours .gitignore, .git/info/exclude and the
// user's global excludesfile without this collector reimplementing any of them. It also settles
// two questions a denylist leaves open, and both answers are deliberate:
//
//   * a tracked file that some ignore rule also matches is IN SCOPE. It is in the checkout, so it
//     belongs in the map — what git tracks is the definition here, not what git would ignore.
//   * a sparse checkout lists index entries that are not on disk. Those are dropped below, with
//     symlinks and submodule gitlinks, because a file this collector cannot open is not a file it
//     can describe.
const BY_PATH = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

function isSkipped(rel) {
  const parts = rel.split("/");
  // The basename is never a directory, so it is never a skip: `dist` as a filename stays in scope.
  for (let i = 0; i < parts.length - 1; i += 1) if (SKIP_DIRS.has(parts[i])) return true;
  return false;
}

function trackedFiles(repo) {
  let raw;
  try {
    raw = execFileSync("git", ["-C", repo, "ls-files", "-z"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      // The default is 1 MiB. A file *list* passes that on a large repository without being large
      // in any other sense, and the throw would land in the catch below — silently downgrading
      // exactly the repositories this matters most on. -z also turns off path quoting, so a
      // non-ASCII filename arrives as itself rather than as an escape.
      maxBuffer: 64 * 1024 * 1024,
    });
  } catch {
    return null;
  }
  // A Set because an unmerged path is listed once per conflict stage.
  const out = new Set();
  for (const rel of raw.split("\0")) {
    if (rel === "" || isSkipped(rel)) continue;
    let stat;
    try {
      stat = lstatSync(join(repo, rel));
    } catch {
      continue;
    }
    // lstat does not follow, so isFile() is already false for a symlink — excluded here for the
    // same reason walk() excludes one: its target is either in the list already or outside the
    // repository, and neither is a file worth mapping twice.
    if (stat.isFile()) out.add(rel);
  }
  return [...out].sort(BY_PATH);
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
  return out.sort(BY_PATH);
}

/**
 * The files to read, and how they were chosen — the second half being the part that has to be
 * reported. A scan of an exported tarball and a scan of a checkout are both legitimate and they do
 * not see the same repository, so which one happened is a fact about the map.
 */
export function listFiles(repo) {
  const tracked = trackedFiles(repo);
  // An empty list is not the same answer as no answer. A directory inside a work tree but not
  // tracked by it — an unpacked archive, a scratch copy, a repository whose first commit has not
  // happened yet — gets an empty, *successful* `ls-files`, and scanning nothing at all is the one
  // result this collector must never produce quietly.
  if (tracked && tracked.length > 0) return { files: tracked, enumeratedBy: "git" };
  return { files: walk(repo), enumeratedBy: "walk" };
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

// --------------------------------------------------------------------------------------------- //
// Parsers. Each returns collections: [{ name, line, fields: [{ name, line }] }].
//
// These read *structure*, never meaning. That a column called `email` exists on line 12 is a parse
// and the collector will stand behind it; what `email` means is a judgement and lives below.

const SQL_SKIP = /^(primary|foreign|unique|constraint|key|index|check|partition|using|with|like|exclude)\b/i;
const SQL_CREATE = /create\s+table\s+(?:if\s+not\s+exists\s+)?[`"[]?([A-Za-z0-9_.]+)[`"\]]?\s*\(/i;

export function parseSqlDdl(text) {
  const lines = text.split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i += 1) {
    const match = lines[i].match(SQL_CREATE);
    if (!match) continue;
    const name = match[1].split(".").pop();
    const startLine = i + 1;
    const fields = [];
    let depth = 0;
    for (let j = i; j < lines.length; j += 1) {
      // Strip the comment before counting parens, so a `--` comment cannot close the table early.
      const line = lines[j].replace(/--.*$/, "");
      const depthBefore = depth;
      for (const ch of line) {
        if (ch === "(") depth += 1;
        else if (ch === ")") depth -= 1;
      }
      // A column sits inside the CREATE TABLE parens, so the line must already be at depth >= 1
      // before it is read. That is what keeps the CREATE line itself and the closing `);` out.
      if (j > i && depthBefore >= 1) {
        const body = line.trim().replace(/^[`"[]/, "");
        const first = body.match(/^([A-Za-z_][A-Za-z0-9_]*)/);
        if (first && !SQL_SKIP.test(body)) fields.push({ name: first[1], line: j + 1 });
      }
      if (j > i && depth <= 0) {
        i = j;
        break;
      }
    }
    if (fields.length > 0) out.push({ name, line: startLine, fields });
  }
  return out;
}

export function parsePrisma(text) {
  const lines = text.split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i += 1) {
    const match = lines[i].match(/^\s*model\s+([A-Za-z0-9_]+)\s*\{/);
    if (!match) continue;
    const fields = [];
    for (let j = i + 1; j < lines.length && !/^\s*\}/.test(lines[j]); j += 1) {
      const body = lines[j].replace(/\/\/.*$/, "").trim();
      if (body === "" || body.startsWith("@@")) continue;
      const field = body.match(/^([A-Za-z_][A-Za-z0-9_]*)\s+[A-Za-z_[]/);
      if (field) fields.push({ name: field[1], line: j + 1 });
    }
    if (fields.length > 0) out.push({ name: match[1], line: i + 1, fields });
  }
  return out;
}

// Django (models.Model) and SQLAlchemy (Column(...)) both declare a column as a class attribute
// assigned from a call. Requiring the call is what keeps ordinary attributes out.
const PY_COLUMN = /^\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[^=]+)?=\s*(?:[A-Za-z_][A-Za-z0-9_.]*\.)?(Column|mapped_column|[A-Za-z]*Field|relationship)\s*\(/;

export function parsePythonOrm(text) {
  const lines = text.split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i += 1) {
    const match = lines[i].match(/^class\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*:/);
    if (!match) continue;
    const bases = match[2];
    if (!/models\.Model|Base\b|db\.Model|SQLModel|DeclarativeBase/.test(bases)) continue;
    const fields = [];
    for (let j = i + 1; j < lines.length && !/^\S/.test(lines[j]); j += 1) {
      const field = lines[j].match(PY_COLUMN);
      if (field) fields.push({ name: field[1], line: j + 1 });
    }
    if (fields.length > 0) out.push({ name: match[1], line: i + 1, fields });
  }
  return out;
}

export function parseProto(text) {
  const lines = text.split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i += 1) {
    const match = lines[i].match(/^\s*message\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{/);
    if (!match) continue;
    const fields = [];
    for (let j = i + 1; j < lines.length && !/^\s*\}/.test(lines[j]); j += 1) {
      const body = lines[j].replace(/\/\/.*$/, "").trim();
      const field = body.match(
        /^(?:repeated\s+|optional\s+|required\s+)?[A-Za-z_][A-Za-z0-9_.<>, ]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\d+\s*;/
      );
      if (field) fields.push({ name: field[1], line: j + 1 });
    }
    if (fields.length > 0) out.push({ name: match[1], line: i + 1, fields });
  }
  return out;
}

export function parseGraphql(text) {
  const lines = text.split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i += 1) {
    const match = lines[i].match(/^\s*(?:type|input)\s+([A-Za-z_][A-Za-z0-9_]*)[^{]*\{/);
    if (!match) continue;
    const fields = [];
    for (let j = i + 1; j < lines.length && !/^\s*\}/.test(lines[j]); j += 1) {
      const body = lines[j].replace(/#.*$/, "").trim();
      const field = body.match(/^([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*:\s*[A-Za-z[]/);
      if (field) fields.push({ name: field[1], line: j + 1 });
    }
    if (fields.length > 0) out.push({ name: match[1], line: i + 1, fields });
  }
  return out;
}

const PARSERS = [
  { kind: "sql_ddl", parse: parseSqlDdl, match: (p) => p.endsWith(".sql") },
  { kind: "prisma", parse: parsePrisma, match: (p) => p.endsWith(".prisma") },
  { kind: "python_orm", parse: parsePythonOrm, match: (p) => p.endsWith(".py") },
  { kind: "protobuf", parse: parseProto, match: (p) => p.endsWith(".proto") },
  {
    kind: "graphql",
    parse: parseGraphql,
    match: (p) => p.endsWith(".graphql") || p.endsWith(".gql") || p.endsWith(".graphqls"),
  },
];

// --------------------------------------------------------------------------------------------- //
// Coverage: what this collector could NOT read.
//
// An empty data map and a repository with no personal data in it produce the same manifest, and
// only one of them is good news. Five formats are parsed above; a repository whose schema lives in
// Mongoose or ActiveRecord produces nothing at all, and every check downstream then passes on an
// empty set. That is the most dangerous failure mode this piece has, because it is silent.
//
// So the collector looks for the shapes it knows it cannot parse and reports them. This is a
// deterministic text match on a marker that means "a schema is defined here" — never an attempt to
// read the schema, which is the whole point: the honest output is "there is one here and I cannot
// see inside it", and a human decides what that means.
const UNPARSED_MARKERS = [
  { format: "typeorm", exts: [".ts", ".js"], marker: /^\s*@Entity\s*\(/m },
  { format: "mongoose", exts: [".ts", ".js"], marker: /new\s+(?:mongoose\.)?Schema\s*\(/ },
  { format: "sequelize", exts: [".ts", ".js"], marker: /DataTypes\.[A-Z]/ },
  // `pgTable(` is a table declaration and nothing else, which is what earns it a place on this
  // list where `z.object(` below is refused one: a Drizzle *Table call is persistence by
  // definition, not a shape that might happen to be stored.
  { format: "drizzle", exts: [".ts", ".js"], marker: /\b(?:pg|mysql|sqlite)Table\s*\(/ },
  { format: "activerecord", exts: [".rb"], marker: /^\s*create_table\s+[:'"]/m },
  { format: "ecto", exts: [".ex"], marker: /^\s*use\s+Ecto\.Schema\b/m },
  { format: "gorm", exts: [".go"], marker: /`[^`]*\bgorm:"/ },
  { format: "openapi", exts: [".yaml", ".yml"], marker: /^(?:openapi|swagger):\s*["']?\d/m },
];

// Deliberately NOT markers, after running this against a real repository:
//
//   * JSON Schema — `"$schema": ".../json-schema.org/..."` appears in every JSON Schema document,
//     including the ones that describe a manifest format rather than anything stored. This
//     repository's own contract/ directory produced ten candidates, none of which holds a byte of
//     personal data. A check that fires on every repository with a schema directory is a check
//     somebody turns off, and then it catches nothing at all.
//   * Zod (`z.object(`) — overwhelmingly request and response validation rather than persistence,
//     and the marker cannot tell the two apart.
//
// Both are still on the "not read yet" list in the piece README, because the *parser* gap is real
// even where the *marker* would cost more than it is worth. The rule this line draws: a marker
// earns its place when it means "a stored record is defined here", not merely "a shape is
// described here".

function findUnparsedCandidates(repo, files, parsedFiles) {
  const out = [];
  for (const rel of files) {
    if (parsedFiles.has(rel)) continue;
    const applicable = UNPARSED_MARKERS.filter((m) => m.exts.some((e) => rel.endsWith(e)));
    if (applicable.length === 0) continue;
    let text;
    try {
      const raw = readFileSync(join(repo, rel));
      if (raw.length > MAX_BYTES) continue;
      text = raw.toString("utf8");
    } catch {
      continue;
    }
    for (const { format, marker } of applicable) {
      const match = marker.exec(text);
      if (!match) continue;
      // The line the marker sits on, so the report cites a place and not just a filename.
      const line = text.slice(0, match.index).split("\n").length;
      out.push({ format, ref: `${rel}:${line}` });
    }
  }
  // Sorted for the same reason walk() is: the derived facts must not depend on traversal order.
  return out.sort((a, b) =>
    a.ref < b.ref ? -1 : a.ref > b.ref ? 1 : a.format < b.format ? -1 : a.format > b.format ? 1 : 0,
  );
}

// --------------------------------------------------------------------------------------------- //
// Classification. The only judgement the collector makes is "this name means the same thing in
// every schema it appears in", and it makes it by lookup, not inference. Everything else is raised.

export function normalizeFieldName(name) {
  return String(name)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

export function classifyField(name, table) {
  const key = normalizeFieldName(name);
  const hit = table.exact[key];
  if (hit) {
    return {
      data_categories: [hit],
      needs_review: false,
      matched_on: key,
      special_category: table.special_categories.includes(hit),
    };
  }
  if (table.operational.includes(key)) {
    return { data_categories: [], needs_review: false, operational: true };
  }
  return { data_categories: [], needs_review: true, reason: table.maybe_pii.includes(key)
    ? "the name looks personal but its category depends on what this table is for"
    : "no exact match in the bundled classification table" };
}

export function fidesKeyFor(path) {
  const key = path
    .replace(/\.[A-Za-z0-9]+$/, "")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
  return key === "" ? "repository" : key;
}

// --------------------------------------------------------------------------------------------- //
// Systems. A service is a directory that declares itself one; the repository root is the fallback,
// because a repository with no package manifest anywhere is still one deployable thing.

const SERVICE_MANIFESTS = new Set([
  "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "composer.json", "Gemfile",
  "build.gradle", "pom.xml",
]);

export function discoverServices(files) {
  const roots = new Set();
  for (const rel of files) {
    const parts = rel.split("/");
    if (SERVICE_MANIFESTS.has(parts[parts.length - 1])) roots.add(parts.slice(0, -1).join("/"));
  }
  if (roots.size === 0) roots.add("");
  return [...roots].sort();
}

export function collectFacts(repo) {
  const { files, enumeratedBy } = listFiles(repo);
  const datasets = [];
  const parsedFiles = new Set();
  const parsedByKind = {};
  let fieldCount = 0;
  let classified = 0;
  let needsReview = 0;
  const specialRefs = [];

  for (const rel of files) {
    const parser = PARSERS.find((p) => p.match(rel));
    if (!parser) continue;
    let text;
    try {
      const raw = readFileSync(join(repo, rel));
      if (raw.length > MAX_BYTES) continue;
      text = raw.toString("utf8");
    } catch {
      continue;
    }
    const parsed = parser.parse(text);
    if (parsed.length === 0) continue;
    parsedFiles.add(rel);
    parsedByKind[parser.kind] = (parsedByKind[parser.kind] ?? 0) + 1;

    const collections = parsed.map((collection) => ({
      name: collection.name,
      ref: `${rel}:${collection.line}`,
      fields: collection.fields.map((field) => {
        const verdict = classifyField(field.name, TABLE);
        fieldCount += 1;
        if (verdict.needs_review) needsReview += 1;
        else if (verdict.data_categories.length > 0) classified += 1;
        if (verdict.special_category) specialRefs.push(`${rel}:${field.line}`);
        return { name: field.name, ref: `${rel}:${field.line}`, ...verdict };
      }),
    }));

    datasets.push({
      fides_key: fidesKeyFor(rel),
      name: rel,
      source_kind: parser.kind,
      ref: `${rel}:1`,
      collections,
    });
  }

  const systems = discoverServices(files).map((root) => ({
    fides_key: fidesKeyFor(root === "" ? "repository" : root),
    name: root === "" ? "repository" : root,
    ref: root === "" ? "." : root,
    dataset_references: datasets
      .filter((d) => (root === "" ? true : d.name.startsWith(`${root}/`)))
      .map((d) => d.fides_key)
      .sort(),
  }));

  return {
    piece: PIECE,
    generated_by: GENERATED_BY,
    files_scanned: files.length,
    datasets,
    systems,
    counts: {
      datasets: datasets.length,
      collections: datasets.reduce((n, d) => n + d.collections.length, 0),
      fields: fieldCount,
      classified,
      needs_review: needsReview,
    },
    // Article 9 and Article 10 data, listed separately because it carries the most risk and is the
    // thing a reviewer must not have to go looking for.
    special_category_refs: specialRefs.sort(),
    // What this scan could and could not read. Consumed by scripts/ci_check.py, which fails a
    // build where nothing was parsed and something was found that should have been — an empty map
    // must never be reportable as a clean one.
    coverage: {
      // Which files this scan could even see. A `walk` means the file list is whatever is on disk
      // rather than whatever is committed, so a scan here and a scan in CI can legitimately
      // disagree — and a reader comparing two manifests needs to know that before blaming one.
      //
      // It sits under `coverage` so it is out of the digest: the same file set enumerated two
      // different ways is the same repository, and must not read as drift.
      enumerated_by: enumeratedBy,
      files_parsed: parsedFiles.size,
      parsed_by_kind: Object.fromEntries(Object.entries(parsedByKind).sort()),
      unparsed_candidates: findUnparsedCandidates(repo, files, parsedFiles),
    },
  };
}


/**
 * The structural anchor (contract/README.md, requirement 8). A digest of the collection's field
 * NAMES — not their categories — so that resolving a classification does not invalidate the
 * signature, but adding, removing or renaming a column does.
 *
 * This is what lets this piece anchor its expiry on `decided_at` honestly. Elsewhere that anchor
 * quietly rewards signing late; here it cannot, because the thing the claim is about is pinned by
 * digest rather than by date. A signature cannot outlive the structure it was given for.
 *
 * The validator recomputes this from the manifest, so the two implementations have to agree. They
 * are kept deliberately trivial for that reason: sorted dotted names, newline-joined, sha256.
 */
export function structureDigest(fields) {
  const names = [];
  const walkFields = (list, prefix) => {
    for (const field of list ?? []) {
      names.push(prefix + field.name);
      if (field.fields) walkFields(field.fields, `${prefix}${field.name}.`);
    }
  };
  walkFields(fields, "");
  return createHash("sha256").update(names.sort().join("\n")).digest("hex");
}

export function digestOf(derived) {
  // `generated_by` is deliberately NOT hashed. This digest answers one question — has the
  // repository changed since the manifest was written? — and the version of the tool that read it
  // is not a fact about the repository.
  //
  // Hashing it made a plugin upgrade indistinguishable from a schema change: every committed
  // manifest reported drift on the next run and CI mode failed with exit 3, for repositories where
  // nothing had moved. It stays in the derived file, and in the manifest, as provenance.
  //
  // `coverage` is excluded for the same reason and a sharper one: the manifest does not record it,
  // so a newly-appeared Mongoose file would produce a drift that re-running :scan could never
  // clear. Coverage is reported to CI from the derived facts directly, where it can be acted on.
  const { generated_by, coverage, ...facts } = derived;
  void generated_by;
  void coverage;
  return createHash("sha256").update(JSON.stringify(facts, null, 0)).digest("hex");
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
    dataset: derived.datasets.map((dataset) => ({
      fides_key: dataset.fides_key,
      name: dataset.name,
      collections: dataset.collections.map((collection) => ({
        name: collection.name,
        refs: [collection.ref],
        structure_digest: structureDigest(collection.fields),
        // The collection is the claim unit: one owner signs for "these are the categories in this
        // table". Per-field attribution would mean five hundred blocks on a five-hundred-column
        // schema, which is a form nobody fills in.
        needs_review: true,
        fields: collection.fields.map((field) => {
          const out = {
            name: field.name,
            data_categories: field.data_categories,
            refs: [field.ref],
          };
          if (field.needs_review) out.needs_review = true;
          return out;
        }),
      })),
    })),
    system: derived.systems.map((system) => ({
      fides_key: system.fides_key,
      name: system.name,
      system_type: "Application",
      dataset_references: system.dataset_references,
      privacy_declarations: [
        {
          name: "",
          data_use: "",
          data_subjects: [],
          data_categories: [],
          refs: [system.ref],
          needs_review: true,
        },
      ],
    })),
  };
}

const HEADER = `# .noru/privacy-datamap.yml — generated by ${GENERATED_BY}
#
# This is a STARTING POINT, not a data map. The collector found the structure and classified the
# field names it recognises with certainty; everything it could not resolve is marked
# needs_review: true, and a manifest carrying one cannot be pushed.
#
# What a person has to do, and sign for:
#   * resolve every needs_review field to a data category, or delete the field if it holds none
#   * name the purpose, data_use and data_subjects for each system's privacy declarations
#   * add an interpretation block to each collection and each declaration: who decided, when,
#     until when, and why
#
# What the validator enforces:
#   * every data_categories / data_use / data_subjects value is a real Fideslang key
#   * refs[] cites the repository lines (file:line) that produced each claim
#   * interpretation.owner is a person, not a team alias
#   * needs_review: true blocks the push
`;


/**
 * The validated manifest — but only if it is about the repository as it stands right now.
 *
 * The digest comparison is the whole gate. A `.fides/datamap.yml` rendered from a manifest that no
 * longer matches the code is a document that looks authoritative and describes a schema that has
 * moved on, which is worse than not producing one: nobody re-reads a file that already exists.
 */
export function readParsedManifest(repo, digest) {
  const path = join(repo, ".noru", ".cache", `${PIECE}.parsed.json`);
  if (!existsSync(path)) return null;
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8"));
    return parsed?.source?.derived_digest === digest ? parsed : null;
  } catch {
    return null;
  }
}

const FIDES_HEADER = `# .fides/datamap.yml — rendered by ${GENERATED_BY}
#
# GENERATED. Edit .noru/${PIECE}.yml instead: this file is overwritten on every scan that finds a
# validated manifest, and it will not warn you, because it cannot tell your edit from its own
# output.
#
# This is the same content that :push sends to Noru, with the review bookkeeping removed — the
# citations, the interpretation blocks, the needs_review flags and the structure digests. What is
# left is plain Fideslang, for \`fides push\` and anything else that reads a Fides manifest.
`;

/** Render the Fides-CLI-shaped export. Declared in piece.json under outputs[]. */
export function renderFides(repo, parsed) {
  const path = join(repo, ".fides", "datamap.yml");
  mkdirSync(join(repo, ".fides"), { recursive: true });
  writeFileSync(path, FIDES_HEADER + toYaml(toFideslang(parsed)), "utf8");
  return path;
}

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
  let rendered = null;
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
    // Only ever from a manifest that validated against this exact repository state. No validated
    // manifest yet is the ordinary case on a first scan, and is not an error: there is simply
    // nothing to render until a human has resolved the review flags and the validator has passed.
    const parsed = readParsedManifest(opts.repo, digest);
    if (parsed && !opts.check) {
      rendered = relative(opts.repo, renderFides(opts.repo, parsed)).split(sep).join("/");
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
    enumerated_by: derived.coverage.enumerated_by,
    wrote_skeleton: wroteSkeleton,
    rendered: rendered,
    provenance,
    counts: { files_scanned: derived.files_scanned, ...derived.counts },
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    process.stdout.write(
      [
        derived.coverage.enumerated_by === "git"
          ? `scanned ${derived.files_scanned} tracked file(s) in ${opts.repo}`
          : `scanned ${derived.files_scanned} file(s) in ${opts.repo} — no tracked file list here, ` +
            "so everything on disk was read and a scan in CI may not agree",
        `${derived.counts.datasets} dataset(s), ${derived.counts.collections} collection(s), ` +
          `${derived.counts.fields} field(s)`,
        `classified: ${derived.counts.classified}, needs review: ${derived.counts.needs_review}`,
        derived.special_category_refs.length > 0
          ? `special-category data at: ${derived.special_category_refs.join(", ")}`
          : "",
        `derived facts: ${summary.derived_facts}`,
        wroteSkeleton ? `wrote skeleton: ${summary.manifest}` : "",
        rendered ? `rendered: ${rendered}` : "",
        drift ? "DRIFT: the manifest does not match the repository as it is now" : "",
      ]
        .filter(Boolean)
        .join("\n") + "\n"
    );
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
