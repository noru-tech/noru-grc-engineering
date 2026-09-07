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
import { basename, dirname, extname, join, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { toFideslang } from "./lib/fides.mjs";

export const PIECE = "privacy-datamap";
export const VERSION = "0.8.0";
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
// Parsers. Each returns collections: [{ name, line, fields: [{ name, line, shape }] }].
//
// These read *structure*, never meaning. That a column called `email` exists on line 12 is a parse
// and the collector will stand behind it; what `email` means is a judgement and lives below.

export function normalizeShape(value) {
  return String(value ?? "")
    .trim()
    .replace(/,$/, "")
    .replace(/\s+/g, " ");
}

export function normalizeSqlShape(value) {
  const input = normalizeShape(value).replace(/;$/, "");
  let out = "";
  let quote = null;
  for (let i = 0; i < input.length; i += 1) {
    const ch = input[i];
    if (quote !== null) {
      out += ch;
      if (ch === quote && input[i + 1] === quote) {
        out += input[i + 1];
        i += 1;
      } else if (ch === quote) {
        quote = null;
      }
    } else if (ch === "'" || ch === '"') {
      quote = ch;
      out += ch;
    } else {
      out += ch.toLowerCase();
    }
  }
  return out;
}

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
        if (first && !SQL_SKIP.test(body)) {
          fields.push({
            name: first[1],
            line: j + 1,
            // SQL type and constraint keywords are case-insensitive. Formatting them differently
            // must not turn an unchanged column into a material privacy delta.
            shape: normalizeSqlShape(body.slice(first[0].length)),
          });
        }
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
      const field = body.match(/^([A-Za-z_][A-Za-z0-9_]*)\s+(.+)$/);
      if (field) {
        fields.push({ name: field[1], line: j + 1, shape: normalizeShape(field[2]) });
      }
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
      if (field) {
        const declaration = lines[j].replace(/\s+#.*$/, "").trim().split("=").slice(1).join("=");
        fields.push({ name: field[1], line: j + 1, shape: normalizeShape(declaration) });
      }
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
        /^(?:(repeated|optional|required)\s+)?([A-Za-z_][A-Za-z0-9_.<>, ]*)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\d+)\s*;/
      );
      if (field) {
        fields.push({
          name: field[3],
          line: j + 1,
          shape: normalizeShape(`${field[1] ?? ""} ${field[2]} = ${field[4]}`),
        });
      }
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
      const field = body.match(
        /^([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*:\s*([A-Za-z_][A-Za-z0-9_\[\]!]*)/
      );
      if (field) fields.push({ name: field[1], line: j + 1, shape: normalizeShape(field[2]) });
    }
    if (fields.length > 0) out.push({ name: match[1], line: i + 1, fields });
  }
  return out;
}

function skipQuoted(text, index) {
  const quote = text[index];
  for (let i = index + 1; i < text.length; i += 1) {
    if (text[i] === "\\") i += 1;
    else if (text[i] === quote) return i + 1;
  }
  return text.length;
}

function matchingDelimiter(text, start, open, close) {
  let depth = 0;
  for (let i = start; i < text.length; i += 1) {
    if (text[i] === '"' || text[i] === "'" || text[i] === "`") {
      i = skipQuoted(text, i) - 1;
    } else if (text.startsWith("//", i)) {
      const end = text.indexOf("\n", i + 2);
      i = end < 0 ? text.length : end;
    } else if (text.startsWith("/*", i)) {
      const end = text.indexOf("*/", i + 2);
      i = end < 0 ? text.length : end + 1;
    } else if (text[i] === open) {
      depth += 1;
    } else if (text[i] === close) {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  return -1;
}

function lineNumberAt(text, index) {
  return text.slice(0, index).split("\n").length;
}

function skipJsTrivia(text, index, limit = text.length) {
  let cursor = index;
  while (cursor < limit) {
    if (/\s/.test(text[cursor])) {
      cursor += 1;
    } else if (text.startsWith("//", cursor)) {
      const end = text.indexOf("\n", cursor + 2);
      cursor = end < 0 || end >= limit ? limit : end + 1;
    } else if (text.startsWith("/*", cursor)) {
      const end = text.indexOf("*/", cursor + 2);
      cursor = end < 0 || end + 2 >= limit ? limit : end + 2;
    } else {
      break;
    }
  }
  return cursor;
}

function splitTopLevelProperties(text, offset) {
  const entries = [];
  let start = 0;
  let round = 0;
  let square = 0;
  let curly = 0;
  let angle = 0;
  for (let i = 0; i <= text.length; i += 1) {
    const ch = text[i];
    if (ch === '"' || ch === "'" || ch === "`") {
      i = skipQuoted(text, i) - 1;
      continue;
    }
    if (text.startsWith("//", i)) {
      const end = text.indexOf("\n", i + 2);
      i = end < 0 ? text.length : end;
      continue;
    }
    if (text.startsWith("/*", i)) {
      const end = text.indexOf("*/", i + 2);
      i = end < 0 ? text.length : end + 1;
      continue;
    }
    if (ch === "(") round += 1;
    else if (ch === ")") round -= 1;
    else if (ch === "[") square += 1;
    else if (ch === "]") square -= 1;
    else if (ch === "{") curly += 1;
    else if (ch === "}") curly -= 1;
    // Drizzle commonly uses `$type<Record<string, unknown>>()`. Without tracking that generic,
    // its comma looks like the end of a column and both fragments disappear from extraction.
    else if (ch === "<" && /[A-Za-z0-9_$.)\]]/.test(text[i - 1] ?? "")) angle += 1;
    else if (ch === ">" && angle > 0) angle -= 1;
    if (
      (ch === "," || i === text.length)
      && round === 0 && square === 0 && curly === 0 && angle === 0
    ) {
      // A comma commonly precedes an inline comment about the field that just ended. That comment
      // is therefore leading trivia for the next slice; retaining it makes a valid next property
      // fail the property regex and silently drops every field in a run of commented declarations.
      const entryStart = skipJsTrivia(text, start, i);
      if (entryStart < i) {
        entries.push({ text: text.slice(entryStart, i).trim(), index: offset + entryStart });
      }
      start = i + 1;
    }
  }
  return entries;
}

function findDrizzleCalls(text) {
  const calls = [];
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === '"' || text[i] === "'" || text[i] === "`") {
      i = skipQuoted(text, i) - 1;
      continue;
    }
    if (text.startsWith("//", i)) {
      const end = text.indexOf("\n", i + 2);
      i = end < 0 ? text.length : end;
      continue;
    }
    if (text.startsWith("/*", i)) {
      const end = text.indexOf("*/", i + 2);
      i = end < 0 ? text.length : end + 1;
      continue;
    }
    const match = text.slice(i).match(/^(pgTable|mysqlTable|sqliteTable)\s*\(/);
    const previous = text[i - 1] ?? "";
    if (match && !/[A-Za-z0-9_$]/.test(previous)) {
      calls.push({ index: i });
      i += match[0].length - 2;
    }
  }
  return calls;
}

/** Parse common Drizzle table declarations and retain the locations of unsupported static gaps. */
function parseDrizzleWithCoverage(text) {
  const out = [];
  const gaps = [];
  for (const match of findDrizzleCalls(text)) {
    const open = text.indexOf("(", match.index);
    const close = matchingDelimiter(text, open, "(", ")");
    if (close < 0) {
      gaps.push(match.index);
      continue;
    }
    const args = text.slice(open + 1, close);
    const tableName = args.match(/^\s*(["'])([^"']+)\1\s*,/);
    if (!tableName) {
      gaps.push(match.index);
      continue;
    }
    const objectStartInArgs = args.indexOf("{", tableName[0].length);
    if (objectStartInArgs < 0) {
      gaps.push(match.index);
      continue;
    }
    const objectStart = open + 1 + objectStartInArgs;
    const objectEnd = matchingDelimiter(text, objectStart, "{", "}");
    if (objectEnd < 0 || objectEnd > close) {
      gaps.push(match.index);
      continue;
    }
    const body = text.slice(objectStart + 1, objectEnd);
    const fields = [];
    for (const entry of splitTopLevelProperties(body, objectStart + 1)) {
      const property = entry.text.match(/^(?:([A-Za-z_$][\w$]*)|["']([^"']+)["'])\s*:\s*([\s\S]+)$/);
      if (!property) {
        // A spread, shorthand or computed property may contain columns, but resolving it requires
        // executing or evaluating repository code. Surface the partial extraction as coverage.
        gaps.push(entry.index);
        continue;
      }
      const expression = property[3].trim();
      const builderCall = /^[A-Za-z_$][\w$.]*(?:\s*<[\s\S]*?>)?\s*\(/;
      if (!builderCall.test(expression)) {
        gaps.push(entry.index);
        continue;
      }
      const physical = expression.match(
        /^[A-Za-z_$][\w$.]*(?:\s*<[\s\S]*?>)?\s*\(\s*(["'])([^"']+)\1/,
      );
      fields.push({
        name: physical?.[2] || property[1] || property[2],
        line: lineNumberAt(text, entry.index),
        shape: normalizeShape(expression),
      });
    }
    if (fields.length > 0) {
      out.push({ name: tableName[2], line: lineNumberAt(text, match.index), fields });
    }
  }
  return { collections: out, gaps };
}

/** Parse common Drizzle table declarations without importing or executing repository code. */
export function parseDrizzle(text) {
  return parseDrizzleWithCoverage(text).collections;
}

const PARSERS = [
  { kind: "sql_ddl", parse: parseSqlDdl, match: (p) => p.endsWith(".sql") },
  {
    kind: "drizzle",
    parse: parseDrizzle,
    match: (p) => /\.[cm]?[jt]sx?$/.test(p),
  },
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
  { format: "drizzle", exts: [".ts", ".tsx", ".js", ".jsx"], marker: /\b(?:pg|mysql|sqlite)Table\s*\(/ },
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
    const applicable = UNPARSED_MARKERS.filter((marker) =>
      marker.exts.some((extension) => rel.endsWith(extension))
      && (!parsedFiles.has(rel) || marker.format === "drizzle")
    );
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
      if (format === "drizzle") {
        const calls = findDrizzleCalls(text);
        if (calls.length === 0) continue;
        const report = parseDrizzleWithCoverage(text);
        if (report.collections.length >= calls.length && report.gaps.length === 0) continue;
        const index = report.gaps[0] ?? calls[0].index;
        out.push({ format, ref: `${rel}:${lineNumberAt(text, index)}` });
        continue;
      }
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
  const parts = String(path).split("/");
  const leaf = parts.at(-1) ?? "";
  const extension = extname(leaf);
  if (!leaf.startsWith(".") && extension) parts[parts.length - 1] = leaf.slice(0, -extension.length);
  const key = parts.join("/")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
  return key === "" ? "repository" : key;
}

function canonicalDigest(value) {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

export function fieldEntityId(datasetKey, collectionName, fieldName) {
  return `${datasetKey}/${collectionName}/${fieldName}`;
}

// --------------------------------------------------------------------------------------------- //
// Normalization. Parsers produce file-shaped observations; these functions decide which of those
// observations describe the same logical datastore and what its current tables look like.

const BOUNDARY_DIRS = new Set(["migration", "migrations", "schema", "schemas", "model", "models"]);

function isMigrationPath(rel) {
  return rel.split("/").some((part) => /^(?:migration|migrations)$/i.test(part));
}

export function datastoreBoundary(rel) {
  const parts = rel.split("/");
  const file = parts.pop();
  const marker = parts.map((part) => BOUNDARY_DIRS.has(part.toLowerCase())).lastIndexOf(true);
  if (marker >= 0) return parts.slice(0, marker).join("/");
  if (/^(?:schema|models?)\.[^.]+$/i.test(file ?? "")) return parts.join("/");
  return parts.join("/");
}

function refsFor(rel, item) {
  return [`${rel}:${item.line}`];
}

function cloneCollections(collections, rel) {
  return collections.map((collection) => ({
    name: collection.name,
    refs: refsFor(rel, collection),
    fields: collection.fields.map((field) => ({
      name: field.name,
      shape: field.shape ?? "",
      refs: refsFor(rel, field),
    })),
  }));
}

function splitSqlStatements(text) {
  const out = [];
  let start = 0;
  let line = 1;
  let startLine = 1;
  let quote = null;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quote !== null) {
      if (ch === quote && text[i + 1] === quote) i += 1;
      else if (ch === quote) quote = null;
    } else if (ch === "'" || ch === '"' || ch === "`") {
      quote = ch;
    } else if (text.startsWith("--", i)) {
      const end = text.indexOf("\n", i + 2);
      i = end < 0 ? text.length : end - 1;
    } else if (text.startsWith("/*", i)) {
      const comment = text.indexOf("*/", i + 2);
      const end = comment < 0 ? text.length : comment + 2;
      line += text.slice(i, end).split("\n").length - 1;
      i = end - 1;
    } else if (ch === ";") {
      const statement = text.slice(start, i + 1).trim();
      if (statement) out.push({ text: statement, line: startLine });
      start = i + 1;
      startLine = line;
    }
    if (ch === "\n") {
      line += 1;
      if (text.slice(start, i + 1).trim() === "") startLine = line;
    }
  }
  const tail = text.slice(start).trim();
  if (tail) out.push({ text: tail, line: startLine });
  return out;
}

function migrationOperation(statement, rel) {
  const compact = statement.text.replace(/--.*$/gm, " ").replace(/\/\*[\s\S]*?\*\//g, " ").trim();
  const ref = `${rel}:${statement.line}`;
  if (/^create\s+table\b/i.test(compact)) {
    const parsed = parseSqlDdl(statement.text).map((collection) => ({
      name: collection.name,
      refs: [`${rel}:${statement.line + collection.line - 1}`],
      fields: collection.fields.map((field) => ({
        name: field.name,
        shape: field.shape ?? "",
        refs: [`${rel}:${statement.line + field.line - 1}`],
      })),
    }));
    return parsed.length === 1
      ? { kind: "create_table", table: parsed[0], ref }
      : { kind: "unsupported", ref };
  }
  if (/^alter\s+table\b/i.test(compact) && /,\s*(?:add|drop|rename|alter)\b/i.test(compact)) {
    return { kind: "unsupported", ref };
  }
  let match = compact.match(
    /^alter\s+table\s+(?:if\s+exists\s+)?[`"[]?([A-Za-z0-9_.]+)[`"\]]?\s+add\s+(?:column\s+)?(?:if\s+not\s+exists\s+)?(?!constraint\b)[`"[]?([A-Za-z_][A-Za-z0-9_]*)[`"\]]?\s+([\s\S]+?);?$/i,
  );
  if (match) {
    return {
      kind: "add_column",
      table: match[1].split(".").pop(),
      field: { name: match[2], shape: normalizeSqlShape(match[3]), refs: [ref] },
      ref,
    };
  }
  match = compact.match(/^alter\s+table\s+(?:if\s+exists\s+)?[`"[]?([A-Za-z0-9_.]+)[`"\]]?\s+drop\s+(?:column\s+)?(?:if\s+exists\s+)?[`"[]?([A-Za-z_][A-Za-z0-9_]*)[`"\]]?\s*;?$/i);
  if (match) return { kind: "drop_column", table: match[1].split(".").pop(), field: match[2], ref };
  match = compact.match(/^alter\s+table\s+[`"[]?([A-Za-z0-9_.]+)[`"\]]?\s+rename\s+column\s+[`"[]?([A-Za-z_][A-Za-z0-9_]*)[`"\]]?\s+to\s+[`"[]?([A-Za-z_][A-Za-z0-9_]*)[`"\]]?\s*;?$/i);
  if (match) return { kind: "rename_column", table: match[1].split(".").pop(), from: match[2], to: match[3], ref };
  match = compact.match(/^alter\s+table\s+[`"[]?([A-Za-z0-9_.]+)[`"\]]?\s+rename\s+to\s+[`"[]?([A-Za-z_][A-Za-z0-9_]*)[`"\]]?\s*;?$/i);
  if (match) return { kind: "rename_table", from: match[1].split(".").pop(), to: match[2], ref };
  match = compact.match(/^drop\s+table\s+(?:if\s+exists\s+)?[`"[]?([A-Za-z0-9_.]+)[`"\]]?(?:\s+(?:cascade|restrict))?\s*;?$/i);
  if (match) return { kind: "drop_table", table: match[1].split(".").pop(), ref };
  return /^(?:create|alter|drop)\s+(?:table|column)\b/i.test(compact)
    ? { kind: "unsupported", ref }
    : { kind: "non_structural", ref };
}

function replayMigrations(observations) {
  const tables = new Map();
  const operations = [];
  const gaps = [];
  for (const observation of observations.sort((a, b) => BY_PATH(a.path, b.path))) {
    for (const statement of splitSqlStatements(observation.text)) {
      const operation = migrationOperation(statement, observation.path);
      operations.push(operation);
      if (operation.kind === "unsupported") {
        gaps.push({
          format: "sql_migration",
          ref: operation.ref,
          reason: "unsupported structural migration statement",
        });
      }
      else if (operation.kind === "create_table") {
        if (tables.has(operation.table.name)) gaps.push({ format: "sql_migration", ref: operation.ref, reason: `table '${operation.table.name}' already exists during replay` });
        else tables.set(operation.table.name, operation.table);
      } else if (operation.kind === "add_column") {
        const table = tables.get(operation.table);
        if (!table || table.fields.some((field) => field.name === operation.field.name)) {
          gaps.push({
            format: "sql_migration", ref: operation.ref,
            reason: "ADD COLUMN could not be applied to the observed state",
          });
        }
        else table.fields.push(operation.field);
      } else if (operation.kind === "drop_column") {
        const table = tables.get(operation.table);
        const index = table?.fields.findIndex((field) => field.name === operation.field) ?? -1;
        if (!table || index < 0) gaps.push({ format: "sql_migration", ref: operation.ref, reason: "DROP COLUMN could not be applied to the observed state" });
        else table.fields.splice(index, 1);
      } else if (operation.kind === "rename_column") {
        const table = tables.get(operation.table);
        const field = table?.fields.find((item) => item.name === operation.from);
        if (!field || table.fields.some((item) => item.name === operation.to)) {
          gaps.push({
            format: "sql_migration", ref: operation.ref,
            reason: "RENAME COLUMN could not be applied to the observed state",
          });
        }
        else { field.name = operation.to; field.refs.push(operation.ref); }
      } else if (operation.kind === "rename_table") {
        const table = tables.get(operation.from);
        if (!table || tables.has(operation.to)) gaps.push({ format: "sql_migration", ref: operation.ref, reason: "RENAME TABLE could not be applied to the observed state" });
        else {
          tables.delete(operation.from);
          table.name = operation.to;
          table.refs.push(operation.ref);
          tables.set(operation.to, table);
        }
      } else if (operation.kind === "drop_table") {
        if (!tables.delete(operation.table)) gaps.push({ format: "sql_migration", ref: operation.ref, reason: "DROP TABLE could not be applied to the observed state" });
      }
    }
  }
  return { collections: gaps.length === 0 ? [...tables.values()] : [], operations, gaps };
}

function mergeCanonical(observations) {
  const tables = new Map();
  const gaps = [];
  for (const observation of observations) {
    for (const incoming of observation.collections) {
      if (!tables.has(incoming.name)) tables.set(incoming.name, { name: incoming.name, refs: [], fields: [] });
      const table = tables.get(incoming.name);
      table.refs.push(...incoming.refs);
      for (const field of incoming.fields) {
        const previous = table.fields.find((item) => item.name === field.name);
        if (!previous) table.fields.push({ ...field, refs: [...field.refs] });
        else if (previous.shape === field.shape) previous.refs.push(...field.refs);
        else gaps.push({ format: "schema_conflict", ref: field.refs[0], reason: `conflicting declarations for '${incoming.name}.${field.name}'` });
      }
    }
  }
  return { collections: gaps.length === 0 ? [...tables.values()] : [], gaps };
}

function normalizedDataset(boundary, collections, sourceKinds) {
  const datasetKey = fidesKeyFor(boundary || "repository");
  let fieldCount = 0;
  let classified = 0;
  let needsReview = 0;
  const specialRefs = [];
  const normalizedCollections = collections
    .sort((a, b) => BY_PATH(a.name, b.name))
    .map((collection) => {
      const fields = collection.fields.sort((a, b) => BY_PATH(a.name, b.name)).map((field) => {
        const verdict = classifyField(field.name, TABLE);
        fieldCount += 1;
        if (verdict.needs_review) needsReview += 1;
        else if (verdict.data_categories.length > 0) classified += 1;
        if (verdict.special_category) specialRefs.push(...field.refs);
        const semantic = { dataset: datasetKey, collection: collection.name, field: field.name, shape: field.shape ?? "" };
        return {
          name: field.name,
          ref: field.refs[0],
          refs: [...new Set(field.refs)].sort(BY_PATH),
          shape: field.shape ?? "",
          entity_id: fieldEntityId(datasetKey, collection.name, field.name),
          semantic_digest: canonicalDigest(semantic),
          ...verdict,
        };
      });
      return {
        name: collection.name,
        ref: collection.refs[0],
        refs: [...new Set(collection.refs)].sort(BY_PATH),
        entity_id: `${datasetKey}/${collection.name}`,
        semantic_digest: canonicalDigest(fields.map((field) => ({ name: field.name, shape: field.shape }))),
        fields,
      };
    });
  return {
    dataset: {
      fides_key: datasetKey,
      name: boundary || "repository",
      source_kind: sourceKinds.length === 1 ? sourceKinds[0] : "normalized",
      source_kinds: sourceKinds,
      ref: normalizedCollections[0]?.ref ?? `${boundary || "."}:1`,
      refs: [...new Set(normalizedCollections.flatMap((item) => item.refs))].sort(BY_PATH),
      collections: normalizedCollections,
    },
    counts: { fieldCount, classified, needsReview },
    specialRefs,
  };
}

const DEPLOYMENT_FILES = /^(?:Dockerfile(?:\..+)?|docker-compose\.ya?ml|compose\.ya?ml|serverless\.ya?ml|vercel\.json|fly\.toml|Procfile)$/i;
const WORKLOAD_MARKER = /^\s*kind:\s*(?:Deployment|StatefulSet|DaemonSet|CronJob|Job)\s*$/m;
const NON_RUNTIME_DIRS = new Set(["test", "tests", "fixture", "fixtures", "example", "examples"]);

function isNonRuntimePath(rel) {
  return rel.split("/").slice(0, -1).some((part) => NON_RUNTIME_DIRS.has(part.toLowerCase()));
}

function entrypointMatch(rel, text) {
  const patterns = [
    { exts: /\.[cm]?[jt]sx?$/, marker: /\b(?:listen\s*\(|createServer\s*\(|serve\s*\(|new\s+Worker\s*\()/ },
    { exts: /\.py$/, marker: /\b(?:uvicorn\.run\s*\(|(?:app|application)\.run\s*\(|Celery\s*\()/ },
    { exts: /\.rb$/, marker: /\b(?:run!|Sidekiq\.configure_server)/ },
    { exts: /\.go$/, marker: /\bhttp\.(?:ListenAndServe|Serve)\s*\(/ },
    { exts: /\.(?:java|kt)$/, marker: /\bSpringApplication\.run\s*\(/ },
  ];
  const pattern = patterns.find((candidate) => candidate.exts.test(rel));
  return pattern ? pattern.marker.exec(text) : null;
}

function safeText(repo, rel) {
  try {
    const raw = readFileSync(join(repo, rel));
    return raw.length <= MAX_BYTES ? raw.toString("utf8") : null;
  } catch { return null; }
}

/** Package metadata is consulted only when an executable script points to an existing entrypoint. */
export function discoverServices(repo, files) {
  const evidence = new Map();
  const fileSet = new Set(files);
  const packageRoots = new Set(
    files
      .filter((rel) =>
        /(?:^|\/)(?:package\.json|pyproject\.toml|go\.mod|Cargo\.toml|pom\.xml|build\.gradle)$/.test(rel)
      )
      .map((rel) => dirname(rel) === "." ? "" : dirname(rel).split(sep).join("/")),
  );
  const runtimeRoot = (rel) => {
    let candidate = dirname(rel) === "." ? "" : dirname(rel).split(sep).join("/");
    while (true) {
      if (packageRoots.has(candidate)) return candidate;
      if (candidate === "") return dirname(rel) === "." ? "" : dirname(rel).split(sep).join("/");
      const parent = dirname(candidate).split(sep).join("/");
      candidate = parent === "." ? "" : parent;
    }
  };
  const add = (root, ref, kind) => {
    if (!evidence.has(root)) evidence.set(root, []);
    evidence.get(root).push({ ref, kind });
  };
  for (const rel of files) {
    if (isNonRuntimePath(rel)) continue;
    const leaf = basename(rel);
    const root = dirname(rel) === "." ? "" : dirname(rel).split(sep).join("/");
    const text = safeText(repo, rel);
    if (DEPLOYMENT_FILES.test(leaf)) add(root, `${rel}:1`, "deployment");
    const workload = /\.ya?ml$/i.test(rel) && text ? WORKLOAD_MARKER.exec(text) : null;
    if (workload) add(root, `${rel}:${lineNumberAt(text, workload.index)}`, "workload");
    const entrypoint = text ? entrypointMatch(rel, text) : null;
    if (entrypoint) {
      add(runtimeRoot(rel), `${rel}:${lineNumberAt(text, entrypoint.index)}`, "entrypoint");
    }
    if (leaf === "package.json" && text) {
      try {
        const pkg = JSON.parse(text);
        const scripts = pkg?.scripts ?? {};
        for (const name of ["start", "serve", "worker", "deploy"]) {
          if (typeof scripts[name] !== "string") continue;
          const candidates = [
            pkg.main,
            pkg.bin,
            ...scripts[name].matchAll(/(?:^|\s)([^\s"']+\.(?:[cm]?[jt]sx?))(?:\s|$)/g),
          ].flatMap((item) =>
            typeof item === "string"
              ? [item]
              : item?.[1]
                ? [item[1]]
                : typeof item === "object" && item
                  ? Object.values(item).filter((value) => typeof value === "string")
                  : []
          );
          if (candidates.some((candidate) => {
            const path = candidate.replace(/^\.\//, "");
            return fileSet.has(root ? `${root}/${path}` : path);
          })) {
            add(root, `${rel}:1`, "executable_script");
            break;
          }
        }
      } catch { /* Invalid package metadata is not runtime evidence. */ }
    }
  }
  if (evidence.size === 0) {
    evidence.set("", [{ ref: files.length > 0 ? `${files[0]}:1` : ".:1", kind: "repository_fallback" }]);
  }
  return [...evidence.entries()]
    .sort(([a], [b]) => BY_PATH(a, b))
    .map(([root, refs]) => ({
      root,
      evidence: refs.sort((a, b) => BY_PATH(a.ref, b.ref)),
    }));
}

function assertUniqueKeys(kind, rows) {
  const seen = new Map();
  for (const row of rows) {
    const previous = seen.get(row.fides_key);
    if (previous) {
      throw new Error(
        `${kind} key collision '${row.fides_key}' for '${previous}' and '${row.name}'; `
        + "rename one boundary so their normalized keys differ",
      );
    }
    seen.set(row.fides_key, row.name);
  }
}

export function collectFacts(repo) {
  const { files, enumeratedBy } = listFiles(repo);
  const observations = [];
  const parsedFiles = new Set();
  const parsedByKind = {};
  for (const rel of files) {
    const parser = PARSERS.find((candidate) => candidate.match(rel));
    if (!parser) continue;
    const text = safeText(repo, rel);
    if (text === null) continue;
    const parsed = parser.parse(text);
    const migrationOnly = parser.kind === "sql_ddl" && isMigrationPath(rel);
    const hasMigrationDdl = migrationOnly && splitSqlStatements(text).some(
      (statement) => migrationOperation(statement, rel).kind !== "non_structural"
    );
    if (parsed.length === 0 && !hasMigrationDdl) continue;
    parsedFiles.add(rel);
    parsedByKind[parser.kind] = (parsedByKind[parser.kind] ?? 0) + 1;
    observations.push({
      path: rel,
      boundary: datastoreBoundary(rel),
      kind: parser.kind,
      role: migrationOnly ? "migration" : "canonical",
      collections: cloneCollections(parsed, rel),
      ...(migrationOnly ? { text } : {}),
    });
  }

  const groups = new Map();
  for (const observation of observations) {
    if (!groups.has(observation.boundary)) groups.set(observation.boundary, []);
    groups.get(observation.boundary).push(observation);
  }
  const datasets = [];
  const migrationGaps = [];
  const migrationOperations = [];
  const schemaConflicts = [];
  let fieldCount = 0;
  let classified = 0;
  let needsReview = 0;
  const specialRefs = [];
  for (const [boundary, group] of [...groups.entries()].sort(([a], [b]) => BY_PATH(a, b))) {
    const canonical = group.filter((item) => item.role === "canonical");
    const migration = group.filter((item) => item.role === "migration");
    const result = canonical.length > 0 ? mergeCanonical(canonical) : replayMigrations(migration);
    schemaConflicts.push(...(canonical.length > 0 ? result.gaps : []));
    if (migration.length > 0) {
      const replay = replayMigrations(migration);
      migrationGaps.push(...replay.gaps);
      migrationOperations.push(...replay.operations.map((operation) => ({
        boundary,
        ...operation,
      })));
      for (const observation of migration) delete observation.text;
    }
    if (result.collections.length === 0) continue;
    const sourceKinds = [...new Set(group.map((item) => item.kind))].sort(BY_PATH);
    const normalized = normalizedDataset(boundary, result.collections, sourceKinds);
    datasets.push(normalized.dataset);
    fieldCount += normalized.counts.fieldCount;
    classified += normalized.counts.classified;
    needsReview += normalized.counts.needsReview;
    specialRefs.push(...normalized.specialRefs);
  }
  assertUniqueKeys("dataset", datasets);

  const services = discoverServices(repo, files);
  const systems = services.map(({ root, evidence }) => ({
    fides_key: fidesKeyFor(root || "repository"),
    name: root || "repository",
    ref: evidence[0].ref,
    refs: evidence.map((item) => item.ref),
    runtime_evidence: evidence,
    dataset_references: datasets
      .filter((dataset) => root === "" || dataset.name === root || dataset.name.startsWith(`${root}/`))
      .map((dataset) => dataset.fides_key).sort(BY_PATH),
  }));
  assertUniqueKeys("system", systems);

  return {
    piece: PIECE,
    generated_by: GENERATED_BY,
    files_scanned: files.length,
    datasets,
    systems,
    observations,
    migration_operations: migrationOperations,
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
      migration_gaps: migrationGaps.sort((a, b) => BY_PATH(a.ref, b.ref)),
      schema_conflicts: schemaConflicts.sort((a, b) => BY_PATH(a.ref, b.ref)),
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
export function structureDigest(fields, nonPersonalFields = []) {
  const names = new Set(nonPersonalFields);
  const walkFields = (list, prefix) => {
    for (const field of list ?? []) {
      names.add(prefix + field.name);
      if (field.fields) walkFields(field.fields, `${prefix}${field.name}.`);
    }
  };
  walkFields(fields, "");
  return createHash("sha256").update([...names].sort().join("\n")).digest("hex");
}

/** Keep every newly observed field visible until a person accepts the collection decision. */
export function reviewCollectionFields(fields) {
  return (fields ?? []).map((field) => {
    const out = {
      name: field.name,
      data_categories: field.data_categories ?? [],
      refs: field.refs ?? [field.ref],
    };
    if (field.needs_review) out.needs_review = true;
    if (field.fields?.length > 0) out.fields = reviewCollectionFields(field.fields);
    return out;
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
  //
  // `coverage` is excluded for the same reason and a sharper one: the manifest does not record it,
  // so a newly-appeared Mongoose file would produce a drift that re-running :scan could never
  // clear. Coverage is reported to CI from the derived facts directly, where it can be acted on.
  // Raw observations retain the evidence needed to audit normalization, but historical migration
  // files that lose to a canonical schema are not part of the current logical topology.
  const { generated_by, coverage, observations, migration_operations, ...facts } = derived;
  void generated_by;
  void coverage;
  void observations;
  void migration_operations;
  return createHash("sha256").update(JSON.stringify(facts, null, 0)).digest("hex");
}

/**
 * The digest emitted before per-entity reconciliation existed. It is retained only as a migration
 * bridge: an already reviewed 0.5.x manifest can prove that it describes the current repository
 * and seed its first lock without sending every field back through an agent.
 */
export function legacyDigestOf(derived) {
  const legacy = JSON.parse(JSON.stringify(derived));
  for (const dataset of legacy.datasets ?? []) {
    for (const collection of dataset.collections ?? []) {
      delete collection.entity_id;
      delete collection.semantic_digest;
      for (const field of collection.fields ?? []) {
        delete field.shape;
        delete field.entity_id;
        delete field.semantic_digest;
      }
    }
  }
  return digestOf(legacy);
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
        refs: collection.refs ?? [collection.ref],
        structure_digest: structureDigest(collection.fields),
        // The collection is the claim unit. All new fields stay visible until an accountable owner
        // accepts the grouped decisions; accepted non-personal names compact on reconciliation.
        needs_review: true,
        fields: reviewCollectionFields(collection.fields),
        non_personal_fields: [],
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
          refs: system.refs ?? [system.ref],
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
#   * resolve every needs_review field to a data category, or move its name to
#     non_personal_fields if review confirms it holds none
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
# This is the same content that :push sends to Noru: privacy-relevant fields projected to plain
# Fideslang. Review bookkeeping and compact non-personal fields stay local; empty collections and
# datasets are removed and system dataset references are repaired.
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

  let derived;
  let provenance;
  let digest;
  let legacyDigest;
  try {
    derived = collectFacts(opts.repo);
    provenance = repoProvenance(opts.repo);
    digest = digestOf(derived);
    legacyDigest = legacyDigestOf(derived);
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    return 2;
  }
  const manifestPath = join(opts.repo, ".noru", "privacy-datamap.yml");
  const derivedPath = join(opts.repo, ".noru", ".cache", "privacy-datamap.derived.json");
  const scanStatePath = join(opts.repo, ".noru", ".cache", "privacy-datamap.scan.json");

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
    writeFileSync(
      scanStatePath,
      `${JSON.stringify({
        piece: PIECE,
        derived_digest: digest,
        legacy_derived_digest: legacyDigest,
        provenance,
      }, null, 2)}\n`,
      "utf8",
    );
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
    legacy_derived_digest: legacyDigest,
    drift,
    enumerated_by: derived.coverage.enumerated_by,
    wrote_skeleton: wroteSkeleton,
    rendered: rendered,
    provenance,
    counts: { files_scanned: derived.files_scanned, ...derived.counts },
    coverage_gaps: derived.coverage.unparsed_candidates.length
      + derived.coverage.migration_gaps.length + derived.coverage.schema_conflicts.length,
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
        summary.coverage_gaps > 0
          ? `coverage gaps: ${summary.coverage_gaps} (see ${summary.derived_facts})`
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
