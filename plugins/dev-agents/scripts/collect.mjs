#!/usr/bin/env node
// Deterministic, offline collector for the dev-agents piece (contract requirement 2).
//
// Node built-ins only. Opens no socket. Same input in, byte-identical derived output —
// scripts/contract_test.py runs this twice and diffs the result.
//
// What this piece inventories is the development process's own suppliers: the coding agents that
// wrote changes here, and the MCP servers and plugins the repository tells those agents to load.
// That is deliberately a different question from ai-inventory's, which is about the AI the
// *product* calls. Two sources, both local:
//
//   1. an agent-change-control manifest at .noru/.cache/acc-manifest.json — the JSON
//      `acc scan --format json` writes, AI Change Provenance 0.3 or 0.4. Its actor registry says
//      which actors are agents and which vendor builds each one; its changes say who the effective
//      author was. From those: each agent, its vendor, and how many changes it authored in the
//      window. Never anything per person (see OPERATORS below).
//   2. the repository's committed agent configuration — the files that declare MCP servers and
//      plugins for Claude Code, Cursor, VS Code and Gemini CLI. Each format is cited to the tool's
//      own public documentation where it is read.
//
// A configuration file can hold a credential: an `env` block, an Authorization header, a token in
// a URL's query string or user-info, an `--api-key=` argument. None of that is ever read into a
// fact. A stdio server is recorded by the *name* of its command only, a remote one by its host
// only, and env, headers, args, cwd and full URLs are never copied anywhere.
//
// Usage: node collect.mjs [--repo=<path>] [--check] [--operator-counts] [--output=json|text] [--quiet]
// Exit codes: 0 ok, 1 drift against the manifest (--check), an unusable acc manifest, or a
//             missing acc manifest a committed manifest was built from; 2 usage/IO error.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, realpathSync, writeFileSync,
} from "node:fs";
import { basename, dirname, join, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const PIECE = "dev-agents";
export const VERSION = "0.14.0";
const GENERATED_BY = `${PIECE}@${VERSION}`;

const ACC_REL = ".noru/.cache/acc-manifest.json";
// Per-person counts live here and nowhere else. See OPERATORS below.
export const OPERATORS_REL = ".noru/.cache/dev-agents.operators.json";

const VOCAB = JSON.parse(
  readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), "..", "references", "vocabulary.json"),
    "utf8",
  ),
);

const SKIP_DIRS = new Set([
  ".git", "node_modules", "dist", "build", "out", ".next", ".turbo", "coverage",
  "vendor", "target", ".venv", "venv", "__pycache__", ".noru",
]);
const USAGE =
  "usage: collect.mjs [--repo=<path>] [--check] [--operator-counts] [--output=json|text] [--quiet]\n";

function parseArgs(argv) {
  const opts = { repo: process.cwd(), check: false, operators: false, json: false, quiet: false };
  for (const arg of argv) {
    if (arg.startsWith("--repo=")) opts.repo = arg.slice(7);
    else if (arg === "--check") opts.check = true;
    else if (arg === "--operator-counts") opts.operators = true;
    else if (arg === "--output=json") opts.json = true;
    else if (arg === "--output=text") opts.json = false;
    else if (arg === "--quiet") opts.quiet = true;
    else if (arg === "-h" || arg === "--help") return { help: true };
    else return { error: `unknown option '${arg}'` };
  }
  return opts;
}

// --------------------------------------------------------------------------------------------- //
// Which files are in scope: what git tracks, wherever there is a git to ask, as every collector
// here decides it. A developer's own untracked .cursor/mcp.json is their configuration, not the
// repository's, and CI — which checks out tracked files only — would never see it anyway.
const BY_PATH = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

function isSkipped(rel) {
  const parts = rel.split("/");
  for (let i = 0; i < parts.length - 1; i += 1) if (SKIP_DIRS.has(parts[i])) return true;
  return false;
}

function trackedFiles(repo) {
  let raw;
  try {
    raw = execFileSync("git", ["-C", repo, "ls-files", "-z"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      maxBuffer: 64 * 1024 * 1024,
    });
  } catch {
    return null;
  }
  const paths = raw.split("\0").filter((rel) => rel !== "");
  if (paths.length === 0) return null;
  const out = new Set();
  for (const rel of paths) {
    if (isSkipped(rel)) continue;
    let stat;
    try {
      stat = lstatSync(join(repo, rel));
    } catch {
      continue;
    }
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
  return out.sort(BY_PATH);
}

export function listFiles(repo) {
  const tracked = trackedFiles(repo);
  if (tracked) return { files: tracked, enumeratedBy: "git" };
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
// Small, careful readers. Everything a configuration file says is untrusted: it is reduced to a
// fixed vocabulary or a narrow pattern before it becomes a fact, and anything that does not fit is
// recorded as unrecognised rather than copied.

function day(value) {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value) ? value.slice(0, 10) : null;
}

/** Strip // and block comments and trailing commas outside strings, then parse. */
export function parseJsonc(text) {
  let out = "";
  let i = 0;
  let inString = false;
  while (i < text.length) {
    const ch = text[i];
    if (inString) {
      out += ch;
      if (ch === "\\") {
        out += text[i + 1] ?? "";
        i += 2;
        continue;
      }
      if (ch === '"') inString = false;
      i += 1;
      continue;
    }
    if (ch === '"') {
      inString = true;
      out += ch;
      i += 1;
    } else if (ch === "/" && text[i + 1] === "/") {
      while (i < text.length && text[i] !== "\n") i += 1;
    } else if (ch === "/" && text[i + 1] === "*") {
      i += 2;
      while (i < text.length && !(text[i] === "*" && text[i + 1] === "/")) {
        // Keep the line count, so a citation after a block comment still points at its line.
        if (text[i] === "\n") out += "\n";
        i += 1;
      }
      i += 2;
    } else {
      out += ch;
      i += 1;
    }
  }
  return JSON.parse(out.replace(/,(\s*[}\]])/g, "$1"));
}

function lineAt(text, index) {
  return index < 0 ? 1 : text.slice(0, index).split("\n").length;
}

/** The line of `"name":` inside the object that `container` opens, for a citation. */
function lineOfKey(text, container, name) {
  const start = container ? text.search(new RegExp(`"${container}"\\s*:`)) : 0;
  const needle = new RegExp(`${escapeRe(JSON.stringify(name))}\\s*:`, "g");
  needle.lastIndex = Math.max(start, 0);
  const hit = needle.exec(text);
  return lineAt(text, hit ? hit.index : start);
}

function escapeRe(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 ._@/+-]{0,99}$/;
const PART_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const COMMAND_RE = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;
const HOST_RE = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$|^\[[0-9a-f:.]+\]$/;
const REPO_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/;
export const VARIABLE = "(variable)";
export const UNRECOGNISED = "(unrecognised)";

/**
 * The name of the program a stdio server runs — `npx`, `uvx`, `docker`, `node` — and nothing
 * else. Never a directory (which can name a person's home), never an argument (which can hold a
 * key). A command built from a variable is recorded as one, not expanded.
 */
export function commandName(command) {
  if (typeof command !== "string" || command.trim() === "") return UNRECOGNISED;
  if (/[$%{}]/.test(command)) return VARIABLE;
  const base = command.trim().split(/[\\/]/).pop();
  return COMMAND_RE.test(base) ? base : UNRECOGNISED;
}

/**
 * The host of a remote server's URL and nothing else: no scheme, no port, no path, no query, and
 * above all no user-info. `https://user:token@mcp.example.com/sse?key=…` becomes `mcp.example.com`.
 */
export function hostOf(url) {
  if (typeof url !== "string" || url.trim() === "") return UNRECOGNISED;
  if (/[$%{}]/.test(url)) return VARIABLE;
  let parsed;
  try {
    parsed = new URL(url.trim());
  } catch {
    return UNRECOGNISED;
  }
  const host = parsed.hostname.toLowerCase();
  return HOST_RE.test(host) ? host : UNRECOGNISED;
}

function keyPart(value) {
  return String(value).toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+/, "") || "x";
}

// --------------------------------------------------------------------------------------------- //
// Source 1: the agent-change-control manifest.
//
// The fields read here are the ones AI Change Provenance defines for its actor registry and its
// changes (specification sections 2 and 5): `events.actors[id].kind` and `.vendor`, a change's
// `author.actor_id` (its effective author) and `reviews[].actor_id`, and `events.window`. They
// have the same shape in 0.3 and 0.4.

/**
 * OPERATORS. Who ran an agent, counted per person, is employee activity data. It is computed only
 * when someone asks for it (--operator-counts), and then written only to OPERATORS_REL under the
 * cache directory, which is never committed and never pushed. It never enters the derived facts,
 * the manifest, the plan or the calls — scripts/test_collectors.py asserts all four.
 */
export function operatorCounts(acc) {
  const counts = new Map();
  for (const change of acc.manifest.events.changes ?? []) {
    const author = change?.author?.actor_id;
    if (typeof author !== "string" || !author.startsWith("agent:")) continue;
    const operator = change?.agent_operator?.actor_id ?? "unknown";
    const row = counts.get(operator) ?? { operator, agent_changes: 0, by_agent: {} };
    const agent = author.slice("agent:".length);
    row.agent_changes += 1;
    row.by_agent[agent] = (row.by_agent[agent] ?? 0) + 1;
    counts.set(operator, row);
  }
  return [...counts.values()].sort((a, b) => BY_PATH(a.operator, b.operator));
}

export function readAcc(repo) {
  const path = join(repo, ACC_REL);
  if (!existsSync(path)) return null;
  const bytes = readFileSync(path);
  let manifest;
  try {
    manifest = JSON.parse(bytes.toString("utf8"));
  } catch {
    throw new AccError(`${ACC_REL} is not JSON. Write the acc manifest with --format json.`);
  }
  if (!manifest || typeof manifest !== "object" || typeof manifest.events !== "object" || manifest.events === null) {
    throw new AccError(`${ACC_REL} is not an agent-change-control manifest (no events)`);
  }
  if (!VOCAB.acp_versions.includes(manifest.version)) {
    throw new AccError(
      `${ACC_REL} is ACP ${manifest.version}; this piece reads ${VOCAB.acp_versions.join(" and ")}`,
    );
  }
  const actors = manifest.events.actors;
  const changes = manifest.events.changes;
  if (!actors || typeof actors !== "object" || Array.isArray(actors) || !Array.isArray(changes)) {
    throw new AccError(`${ACC_REL} has no actor registry or no change list under events`);
  }
  return {
    manifest,
    text: bytes.toString("utf8"),
    sha256: createHash("sha256").update(bytes).digest("hex"),
  };
}

export class AccError extends Error {}

function accFacts(acc) {
  const { manifest, text } = acc;
  const actors = manifest.events.actors;
  const changes = manifest.events.changes;
  const agents = new Map();
  const ensure = (id) => {
    if (!agents.has(id)) {
      const actor = actors[id];
      const vendor = typeof actor?.vendor === "string" && PART_RE.test(actor.vendor)
        ? actor.vendor.toLowerCase()
        : null;
      const name = id.slice("agent:".length);
      agents.set(id, {
        agent: PART_RE.test(name) ? name.toLowerCase() : UNRECOGNISED,
        actor_id: PART_RE.test(name) ? id : UNRECOGNISED,
        vendor,
        changes_authored: 0,
        reviews: 0,
        ref: `${ACC_REL}:${lineOfKey(text, "actors", id)}`,
      });
    }
    return agents.get(id);
  };
  for (const [id, actor] of Object.entries(actors)) {
    if (actor?.kind === "agent" && id.startsWith("agent:")) ensure(id);
  }
  let agentAuthored = 0;
  for (const change of changes) {
    const author = change?.author?.actor_id;
    if (typeof author === "string" && author.startsWith("agent:")) {
      ensure(author).changes_authored += 1;
      agentAuthored += 1;
    }
    for (const review of change?.reviews ?? []) {
      const who = review?.actor_id;
      if (typeof who === "string" && who.startsWith("agent:")) ensure(who).reviews += 1;
    }
  }
  const rows = [...agents.values()].sort((a, b) => BY_PATH(a.agent, b.agent));
  const vendors = new Map();
  for (const row of rows) {
    const key = row.vendor ?? "";
    const entry = vendors.get(key) ?? { vendor: row.vendor, agents: [], changes_authored: 0 };
    entry.agents.push(row.agent);
    entry.changes_authored += row.changes_authored;
    vendors.set(key, entry);
  }
  const window = manifest.events.window ?? {};
  return {
    acc: {
      input: "agent-change-control",
      acp_version: manifest.version,
      manifest_sha256: acc.sha256,
      window: {
        opens_on: day(window.from),
        closes_on: day(window.to),
        complete: window.complete === true,
      },
      changes: changes.length,
      agent_authored: agentAuthored,
      ref: `${ACC_REL}:${lineOfKey(text, "events", "window")}`,
    },
    agents: rows,
    vendors: [...vendors.values()].sort((a, b) => BY_PATH(a.vendor ?? "~", b.vendor ?? "~")),
  };
}

// --------------------------------------------------------------------------------------------- //
// Source 2: repository agent configuration. One reader per documented format.

/**
 * Claude Code project MCP servers, `.mcp.json`.
 * Format: https://code.claude.com/docs/en/mcp — a top-level `mcpServers` object keyed by server
 * name; a stdio entry has `command` (and `args`, `env`), and an entry with no `type` is read as
 * stdio; a remote entry has `type` (`http`, `sse`, `ws`, or `streamable-http` as an alias for
 * `http`) and `url` (and `headers`). "A JSON entry that has a `url` but no `type` is a
 * configuration error", so such an entry is reported, not inventoried.
 */
function claudeMcpServers(doc) {
  return serversFrom(doc?.mcpServers, (entry) => {
    const type = entry.type === "streamable-http" ? "http" : entry.type;
    if (type === undefined && entry.url !== undefined && entry.command === undefined) {
      return { problem: "has a `url` but no `type`, which Claude Code documents as a configuration error" };
    }
    if (type === undefined || type === "stdio") return { transport: "stdio", command: entry.command };
    if (["http", "sse", "ws"].includes(type)) return { transport: type, url: entry.url };
    return { problem: `has a \`type\` this piece does not recognise` };
  });
}

/**
 * Cursor project MCP servers, `.cursor/mcp.json`.
 * Format: https://cursor.com/docs/context/mcp — a top-level `mcpServers` object; a stdio server
 * has `command` (and `args`, `env`); a remote server has `url` (and `headers`) and is "HTTP or
 * SSE". The examples omit `type`, so a remote server whose transport is not stated is recorded as
 * `remote` rather than guessed.
 */
function cursorMcpServers(doc) {
  return serversFrom(doc?.mcpServers, (entry) => {
    if (entry.command !== undefined) return { transport: "stdio", command: entry.command };
    if (entry.url !== undefined) {
      const type = entry.type === "streamableHttp" ? "http" : entry.type;
      return { transport: ["http", "sse"].includes(type) ? type : "remote", url: entry.url };
    }
    return { problem: "names neither a `command` nor a `url`" };
  });
}

/**
 * VS Code workspace MCP servers, `.vscode/mcp.json`.
 * Format: https://code.visualstudio.com/docs/agents/reference/mcp-configuration — a top-level
 * `servers` object (and an optional `inputs` array, never read); a stdio server has `type:
 * "stdio"`, `command` (and `args`, `env`, `envFile`, `cwd`); an HTTP or SSE server has `type:
 * "http"` or `"sse"`, `url` (and `headers`). The documentation's own examples omit `type`, so a
 * missing one is inferred from `command` or `url` the same way.
 */
function vscodeMcpServers(doc) {
  return serversFrom(doc?.servers, (entry) => {
    if (entry.type === "stdio" || (entry.type === undefined && entry.command !== undefined)) {
      return { transport: "stdio", command: entry.command };
    }
    if (entry.type === "http" || entry.type === "sse") return { transport: entry.type, url: entry.url };
    if (entry.type === undefined && entry.url !== undefined) return { transport: "remote", url: entry.url };
    return { problem: "has no `type`, `command` or `url` this piece recognises" };
  });
}

/**
 * Gemini CLI project MCP servers, `.gemini/settings.json`.
 * Format: https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md — a
 * top-level `mcpServers` object; each server has one of `command` (stdio, with `args`, `env`,
 * `cwd`), `url` (an SSE endpoint) or `httpUrl` (streamable HTTP), and optionally `headers`.
 */
function geminiMcpServers(doc) {
  return serversFrom(doc?.mcpServers, (entry) => {
    if (entry.command !== undefined) return { transport: "stdio", command: entry.command };
    if (entry.httpUrl !== undefined) return { transport: "http", url: entry.httpUrl };
    if (entry.url !== undefined) return { transport: "sse", url: entry.url };
    return { problem: "names none of `command`, `url` or `httpUrl`" };
  });
}

function serversFrom(container, classify) {
  if (container === undefined) return [];
  if (!container || typeof container !== "object" || Array.isArray(container)) {
    return [{ name: null, problem: "the server list is not an object keyed by server name" }];
  }
  return Object.entries(container).map(([name, entry]) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
      return { name, problem: "is not an object" };
    }
    return { name, ...classify(entry) };
  });
}

/**
 * Claude Code plugins a project enables, `.claude/settings.json`.
 * Format: https://code.claude.com/docs/en/settings-reference — `enabledPlugins` is an "object
 * mapping `plugin-name@marketplace-name` to a Boolean"; `extraKnownMarketplaces` is an "object
 * mapping a marketplace name to an object with a `source` object", where `source.source` is the
 * kind (`github` with `repo`, `git` with `url`, and others). Only plugins set to true are
 * inventoried: a disabled plugin is not something the development process depends on.
 */
function claudePlugins(doc) {
  const plugins = [];
  const problems = [];
  const enabled = doc?.enabledPlugins;
  if (enabled !== undefined && (!enabled || typeof enabled !== "object" || Array.isArray(enabled))) {
    problems.push({ name: "enabledPlugins", problem: "is not an object" });
  }
  const marketplaces = doc?.extraKnownMarketplaces;
  const sourceOf = (name) => {
    const source = marketplaces?.[name]?.source;
    if (!source || typeof source !== "object") return {};
    const kind = typeof source.source === "string" && /^[a-z][a-z-]{0,30}$/.test(source.source)
      ? source.source
      : UNRECOGNISED;
    const out = { marketplace_source: kind };
    if (kind === "github") {
      out.marketplace_repo = typeof source.repo === "string" && REPO_RE.test(source.repo)
        ? source.repo
        : UNRECOGNISED;
    } else if (typeof source.url === "string") {
      out.marketplace_host = hostOf(source.url);
    }
    return out;
  };
  for (const [id, on] of Object.entries(enabled && typeof enabled === "object" ? enabled : {})) {
    if (on !== true) continue;
    const at = id.lastIndexOf("@");
    const plugin = at > 0 ? id.slice(0, at) : id;
    const marketplace = at > 0 ? id.slice(at + 1) : "";
    if (!PART_RE.test(plugin) || !PART_RE.test(marketplace)) {
      problems.push({ name: id, problem: "is not `plugin-name@marketplace-name`" });
      continue;
    }
    plugins.push({ id, plugin, marketplace, ...sourceOf(marketplace) });
  }
  return { plugins, problems };
}

const MCP_READERS = {
  ".mcp.json": { container: "mcpServers", read: claudeMcpServers },
  ".cursor/mcp.json": { container: "mcpServers", read: cursorMcpServers },
  ".vscode/mcp.json": { container: "servers", read: vscodeMcpServers },
  ".gemini/settings.json": { container: "mcpServers", read: geminiMcpServers },
};

function configFacts(repo, files) {
  const present = new Set(files);
  const configs = [];
  const servers = [];
  const plugins = [];
  const problems = [];
  for (const file of Object.keys(VOCAB.config_files).sort(BY_PATH)) {
    if (!present.has(file)) continue;
    const tool = VOCAB.config_files[file];
    const text = readFileSync(join(repo, file), "utf8");
    let doc;
    try {
      doc = parseJsonc(text);
    } catch {
      configs.push({ file, tool, status: "unreadable", ref: `${file}:1` });
      problems.push({ ref: `${file}:1`, message: `${file} is not readable JSON, so nothing it declares is in this inventory` });
      continue;
    }
    configs.push({ file, tool, status: "read", ref: `${file}:1` });

    const reader = MCP_READERS[file];
    if (reader) {
      for (const row of reader.read(doc)) {
        const ref = `${file}:${row.name === null ? 1 : lineOfKey(text, reader.container, row.name)}`;
        const name = typeof row.name === "string" && NAME_RE.test(row.name) ? row.name : UNRECOGNISED;
        if (row.problem) {
          problems.push({ ref, message: `server '${name}' in ${file} ${row.problem}` });
          continue;
        }
        servers.push({
          key: `${tool}.${keyPart(name)}`,
          file,
          tool,
          name,
          transport: row.transport,
          ...(row.transport === "stdio" ? { command: commandName(row.command) } : { host: hostOf(row.url) }),
          ref,
        });
      }
    }
    if (file === ".claude/settings.json") {
      const found = claudePlugins(doc);
      for (const p of found.problems) {
        const label = NAME_RE.test(p.name) ? p.name : UNRECOGNISED;
        problems.push({ ref: `${file}:${lineOfKey(text, null, p.name)}`, message: `plugin '${label}' in ${file} ${p.problem}` });
      }
      for (const p of found.plugins) {
        plugins.push({
          key: `${tool}.${keyPart(p.plugin)}.${keyPart(p.marketplace)}`,
          file,
          tool,
          plugin: p.plugin,
          marketplace: p.marketplace,
          ...pick(p, ["marketplace_source", "marketplace_repo", "marketplace_host"]),
          ref: `${file}:${lineOfKey(text, "enabledPlugins", p.id)}`,
        });
      }
    }
  }
  return {
    configs,
    mcp_servers: dedupeKeys(servers),
    plugins: dedupeKeys(plugins),
    problems: problems.sort((a, b) => BY_PATH(a.ref + a.message, b.ref + b.message)),
  };
}

/** Two names that differ only in case would share a key; the second gets a suffix, stably. */
function dedupeKeys(rows) {
  rows.sort((a, b) => BY_PATH(a.key, b.key) || BY_PATH(a.ref, b.ref));
  const seen = new Map();
  for (const row of rows) {
    const n = (seen.get(row.key) ?? 0) + 1;
    seen.set(row.key, n);
    if (n > 1) row.key = `${row.key}-${n}`;
  }
  return rows;
}

function pick(source, keys) {
  const out = {};
  for (const key of keys) {
    if (source?.[key] !== undefined && source?.[key] !== null) out[key] = source[key];
  }
  return out;
}

export function collectFacts(repo, acc = readAcc(repo)) {
  const { files, enumeratedBy } = listFiles(repo);
  const fromAcc = acc ? accFacts(acc) : { acc: null, agents: [], vendors: [] };
  const fromConfig = configFacts(repo, files);
  return {
    piece: PIECE,
    generated_by: GENERATED_BY,
    // How the file list was arrived at, not what was in it. Out of the digest, like every piece.
    coverage: { enumerated_by: enumeratedBy },
    acc: fromAcc.acc,
    agents: fromAcc.agents,
    vendors: fromAcc.vendors,
    ...fromConfig,
    counts: {
      agents: fromAcc.agents.length,
      agent_vendors: fromAcc.vendors.length,
      changes: fromAcc.acc?.changes ?? 0,
      agent_authored: fromAcc.acc?.agent_authored ?? 0,
      configs: fromConfig.configs.length,
      mcp_servers: fromConfig.mcp_servers.length,
      plugins: fromConfig.plugins.length,
      problems: fromConfig.problems.length,
    },
  };
}

export function digestOf(derived) {
  // `generated_by` is the tool's version, not a fact about the repository, and `coverage` is how
  // the file list was enumerated, not what was in it. Hashing either would report drift where
  // nothing moved.
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
  // A string that reads as a number (`0.4`) would load back as one, so it is quoted.
  if (
    s === "" || !PLAIN_SAFE.test(s) || /^(true|false|null|yes|no|on|off)$/i.test(s) ||
    /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(s)
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

function todoInterpretation(decided) {
  return {
    owner: "TODO_a.person@example.com",
    decided_at: decided ?? "TODO_YYYY-MM-DD",
    expires_at: "TODO_YYYY-MM-DD",
    rationale: "TODO: why this is the right supplier, in a sentence a reviewer can argue with.",
  };
}

/**
 * The supplier proposals. An agent's vendor is proposed from the acc vendor registry, by the name
 * in this plugin's vocabulary; an MCP server's or a plugin's publisher is NEVER guessed — not from
 * a hostname, not from a package scope, not from a marketplace's repository owner — and is left
 * TODO for a person. Every proposal carries needs_review: true either way.
 */
export function buildSkeleton(derived, provenance) {
  const acc = derived.acc;
  const skeleton = {
    version: VERSION,
    piece: PIECE,
    source: {
      ...provenance,
      ...(acc
        ? { input: acc.input, acp_version: acc.acp_version, acc_manifest_sha256: acc.manifest_sha256 }
        : {}),
      derived_digest: digestOf(derived),
    },
  };
  if (acc) {
    skeleton.window = {
      opens_on: acc.window.opens_on ?? "TODO_YYYY-MM-DD",
      closes_on: acc.window.closes_on ?? "TODO_YYYY-MM-DD",
      complete: acc.window.complete,
    };
    skeleton.changes = { total: acc.changes, agent_authored: acc.agent_authored, refs: [acc.ref] };
  }

  const suppliers = [];
  const agentSupplier = new Map();
  for (const vendor of derived.vendors) {
    const key = vendor.vendor ? `agent-vendor.${keyPart(vendor.vendor)}` : null;
    for (const agentName of vendor.agents) {
      agentSupplier.set(agentName, key ?? `agent.${keyPart(agentName)}`);
    }
    const agentRows = derived.agents.filter((a) => vendor.agents.includes(a.agent));
    const keys = key ? [key] : agentRows.map((a) => `agent.${keyPart(a.agent)}`);
    for (const supplierKey of keys) {
      const rows = key ? agentRows : agentRows.filter((a) => `agent.${keyPart(a.agent)}` === supplierKey);
      suppliers.push({
        key: supplierKey,
        vendor_name: (vendor.vendor && VOCAB.acc_vendor_names[vendor.vendor]) || "TODO",
        category: VOCAB.agent_vendor_category,
        refs: rows.map((a) => a.ref),
        needs_review: true,
        interpretation: todoInterpretation(acc?.window.closes_on),
      });
    }
  }

  skeleton.agents = derived.agents.map((a) => ({
    agent: a.agent,
    actor_id: a.actor_id,
    vendor: a.vendor,
    changes_authored: a.changes_authored,
    reviews: a.reviews,
    supplier: agentSupplier.get(a.agent),
    refs: [a.ref],
  }));
  skeleton.mcp_servers = derived.mcp_servers.map((s) => {
    const supplier = `mcp.${s.key}`;
    suppliers.push({
      key: supplier,
      vendor_name: "TODO",
      category: "TODO",
      refs: [s.ref],
      needs_review: true,
      interpretation: todoInterpretation(acc?.window.closes_on),
    });
    return { ...withoutRef(s), supplier, refs: [s.ref] };
  });
  skeleton.plugins = derived.plugins.map((p) => {
    const supplier = `plugin.${p.key}`;
    suppliers.push({
      key: supplier,
      vendor_name: "TODO",
      category: "TODO",
      refs: [p.ref],
      needs_review: true,
      interpretation: todoInterpretation(acc?.window.closes_on),
    });
    return { ...withoutRef(p), supplier, refs: [p.ref] };
  });
  if (derived.problems.length > 0) {
    skeleton.config_problems = derived.problems.map((p) => ({ message: p.message, refs: [p.ref] }));
  }
  skeleton.suppliers = suppliers;

  // Requirement 9: the control ids come from Noru, never from here. The skill fills this from
  // getOrganizationControls / getControlContext, and the validator refuses any mapping to an id
  // the snapshot did not offer.
  skeleton.queue_snapshot = {
    fetched_at: "TODO_YYYY-MM-DDTHH:MM:SSZ",
    via: ["getOrganizationControls", "getControlContext"],
    controls: [],
  };
  skeleton.control_mappings = [];

  const refs = [
    ...(acc ? [acc.ref] : []),
    ...derived.configs.map((c) => c.ref),
  ];
  skeleton.attestation = {
    refs,
    needs_review: true,
    interpretation: {
      ...todoInterpretation(acc?.window.closes_on),
      rationale:
        "TODO: who reviewed this inventory of the agents, MCP servers and plugins the development " +
        "process depends on, and what they checked.",
    },
  };
  return skeleton;
}

function withoutRef(row) {
  const { ref, ...rest } = row;
  void ref;
  return rest;
}

const HEADER = `# .noru/dev-agents.yml — generated by ${GENERATED_BY}
#
# The coding agents, MCP servers and plugins this repository's development process depends on,
# as suppliers. Not the AI the product calls: that is ai-inventory's.
#
# Every supplier below is a proposal. An agent's vendor is proposed from the acc vendor registry;
# an MCP server's or a plugin's publisher is left TODO, because a hostname or a package scope is
# not a publisher. A person decides each name and category, and signs for it. Every supplier and
# the attestation carry needs_review: true until then, and a manifest carrying one cannot be pushed.
#
# Nothing here is per person. Who ran which agent is never written to this file.
`;

function readManifestDigest(manifestPath) {
  if (!existsSync(manifestPath)) return null;
  const m = readFileSync(manifestPath, "utf8").match(/derived_digest:\s*"?([0-9a-f]{64})"?/);
  return m ? m[1] : "";
}

function manifestNamesAcc(manifestPath) {
  return existsSync(manifestPath) && /acc_manifest_sha256:/.test(readFileSync(manifestPath, "utf8"));
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

  const manifestPath = join(opts.repo, ".noru", "dev-agents.yml");
  let acc;
  try {
    acc = readAcc(opts.repo);
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    return error instanceof AccError ? 1 : 2;
  }
  if (acc === null && manifestNamesAcc(manifestPath)) {
    // The committed manifest was built from an acc manifest that is not here — the ordinary state
    // on a checkout where acc has not run. That is a missing input, not drift: comparing would
    // report every agent as gone.
    process.stderr.write(
      `error: no ${ACC_REL}, and .noru/dev-agents.yml was built from one. Run acc first:\n` +
        `  acc scan --format json -o ${ACC_REL}\n`,
    );
    return 1;
  }

  let derived;
  try {
    derived = collectFacts(opts.repo, acc);
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    return 2;
  }
  const provenance = repoProvenance(opts.repo);
  const digest = digestOf(derived);
  const derivedPath = join(opts.repo, ".noru", ".cache", "dev-agents.derived.json");
  const operatorsPath = join(opts.repo, OPERATORS_REL);

  let wroteSkeleton = false;
  let drift = false;
  let operatorRows = null;
  try {
    mkdirSync(join(opts.repo, ".noru", ".cache"), { recursive: true });
    writeFileSync(derivedPath, `${JSON.stringify(derived, null, 2)}\n`, "utf8");
    if (opts.operators && acc) {
      operatorRows = operatorCounts(acc);
      writeFileSync(
        operatorsPath,
        `${JSON.stringify(
          {
            _comment:
              "Per-person agent usage, written only because --operator-counts was passed. " +
              "Employee activity data: keep it out of git, and out of anything sent anywhere.",
            acc_manifest_sha256: acc.sha256,
            window: derived.acc.window,
            operators: operatorRows,
          },
          null,
          2,
        )}\n`,
        "utf8",
      );
    }
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
    enumerated_by: derived.coverage.enumerated_by,
    wrote_skeleton: wroteSkeleton,
    provenance,
    counts: derived.counts,
    // Where the per-person file went and how many rows it has — never its contents, because this
    // summary is what a CI log prints.
    ...(operatorRows
      ? { operator_counts: { written_to: OPERATORS_REL, operators: operatorRows.length } }
      : {}),
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    const c = derived.counts;
    process.stdout.write(
      [
        derived.acc
          ? `acc ${derived.acc.acp_version} manifest: ${c.agent_authored} of ${c.changes} change(s) ` +
            `agent-authored, ${c.agents} agent(s) from ${c.agent_vendors} vendor(s)`
          : `no ${ACC_REL}: agents are not in this inventory`,
        `agent configuration: ${c.configs} file(s), ${c.mcp_servers} MCP server(s), ${c.plugins} plugin(s)`,
        c.problems ? `configuration problems: ${c.problems} (listed in the manifest)` : "",
        `derived facts: ${summary.derived_facts}`,
        operatorRows ? `per-operator counts (not committed, not pushed): ${OPERATORS_REL}` : "",
        wroteSkeleton ? `wrote skeleton: ${summary.manifest}` : "",
        drift ? "DRIFT: the manifest does not match the repository as it is now" : "",
      ]
        .filter(Boolean)
        .join("\n") + "\n",
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
