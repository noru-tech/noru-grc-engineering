#!/usr/bin/env node
// The headless MCP client: what an interactive MCP host does for a piece, done by a CI job.
//
// Two subcommands, both driven by plugins/<piece>/piece.json and nothing else:
//
//   state  calls the read tools the piece declares under `state.reads` and writes
//          .noru/.cache/noru-state.json, with `connection` taken from getMcpCapabilities rather than
//          from configuration, so a plan binds to the organization the key actually reaches.
//   exec   executes the call list the piece's own :push emitted (.noru/.cache/<piece>.calls.json),
//          in order, stopping at the first failure, and writes a receipt
//          (.noru/.cache/<piece>.receipt.json). Nothing is reported as written without a receipt
//          that says every call succeeded.
//
// This is toolkit infrastructure, not a piece. A piece still never handles a credential: the key is
// read here, from the environment, at the point of the first request, and nowhere else.
//
// What `exec` refuses, before a single byte reaches the network:
//   * a plan that the piece's own freshness check rejects (manifest bytes, expiry, organization,
//     endpoint, repository, piece version, scopes) — the same check :push ran
//   * a call list that is not exactly the plan's non-skip operations, in order, with the same
//     arguments, or that names a tool the piece does not declare
//   * a plan bound to an endpoint other than the one this job is configured for. The endpoint comes
//     from NORU_API_URL in the job's environment, never from a file: a state or plan file that names
//     another host must not be able to steer the key there
// and after connecting, before the first write:
//   * an organization other than the one the plan was built against
//   * a connection that lacks a scope the plan needs, or does not offer a tool the plan calls
//
// Node built-ins only.
//
// Usage:
//   node scripts/mcp_exec.mjs state --piece=<name> [--repo=<path>] [--plugins=<dir>]
//                                    [--output=json|text] [--quiet]
//   node scripts/mcp_exec.mjs exec  --piece=<name> [--repo=<path>] [--plugins=<dir>]
//                                    [--output=json|text] [--quiet]
// Environment:
//   NORU_API_KEY   required for any request; read at the point of the first request, never stored
//   NORU_API_URL   optional origin, defaults to https://api.noru.tech; the MCP endpoint is
//                  <origin>/v1/mcp. https only, except a loopback host for tests
// Exit codes:
//   0 = state written, or every planned call succeeded (an empty call list is success)
//   1 = refused or failed; nothing after the failure was executed — read the receipt
//   2 = usage error

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const DEFAULT_BASE_URL = "https://api.noru.tech";
const MCP_PATH = "/v1/mcp";
const PROTOCOL_VERSION = "2025-06-18";
const REQUEST_TIMEOUT_MS = 60_000;
const MAX_RESPONSE_BYTES = 32 * 1024 * 1024;
const MAX_PAGES = 100;
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);

const USAGE =
  "usage: mcp_exec.mjs state|exec --piece=<name> [--repo=<path>] [--plugins=<dir>]\n" +
  "                    [--output=json|text] [--quiet]\n";

class Refusal extends Error {}
class UsageError extends Error {}

// --- small helpers ----------------------------------------------------------------------------

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function readJson(path, label) {
  if (!existsSync(path)) throw new Refusal(`${label} not found at ${path}`);
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Refusal(`${label} at ${path} is not readable JSON (${error.message})`);
  }
}

function writeJsonAtomic(path, value) {
  mkdirSync(dirname(path), { recursive: true });
  const temporary = `${path}.${process.pid}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  renameSync(temporary, path);
}

/** Order-insensitive for object keys, order-sensitive for arrays: the comparison a plan needs. */
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

function scopeGranted(granted, required) {
  if (granted.includes(required) || granted.includes("*")) return true;
  return granted.includes(`${required.split(":", 1)[0]}:*`);
}

function normalizedUrl(value) {
  try {
    return new URL(value).toString();
  } catch {
    return null;
  }
}

/**
 * The only endpoint this process will ever send the key to. It comes from the job's environment,
 * so a value written in a state, plan or calls file can be compared against it but never replace it.
 */
export function configuredEndpoint(env = process.env) {
  const raw = (env.NORU_API_URL ?? DEFAULT_BASE_URL).trim();
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new UsageError(`NORU_API_URL='${raw}' is not a URL`);
  }
  if (url.username || url.password || url.search || url.hash || !["", "/"].includes(url.pathname)) {
    throw new UsageError("NORU_API_URL must be an origin such as https://api.noru.tech, with no path");
  }
  const loopback = LOOPBACK_HOSTS.has(url.hostname);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) {
    throw new UsageError("NORU_API_URL must use https (plain http is accepted only for a loopback host)");
  }
  return `${url.origin}${MCP_PATH}`;
}

// --- redaction --------------------------------------------------------------------------------

let pieceRedact = (text) => text;

/** Scrub anything credential-shaped, and the live key itself, from text that may leave the process. */
export function scrub(value) {
  let text = typeof value === "string" ? value : JSON.stringify(value);
  const key = process.env.NORU_API_KEY;
  if (key && key.length >= 4) text = text.split(key).join("<redacted>");
  return pieceRedact(text);
}

// --- MCP over Streamable HTTP -----------------------------------------------------------------

function parseSse(text) {
  const messages = [];
  for (const block of text.split(/\r?\n\r?\n/)) {
    const data = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data) continue;
    try {
      messages.push(JSON.parse(data));
    } catch {
      // A keep-alive or a non-JSON event is not the response we are waiting for.
    }
  }
  return messages;
}

async function readBody(response) {
  const text = await response.text();
  if (Buffer.byteLength(text) > MAX_RESPONSE_BYTES) {
    throw new Refusal(`the MCP response exceeded ${MAX_RESPONSE_BYTES} bytes`);
  }
  return text;
}

export class McpClient {
  constructor(endpoint, key) {
    this.endpoint = endpoint;
    this.key = key;
    this.session = null;
    this.protocol = null;
    this.nextId = 1;
  }

  async post(body) {
    const headers = {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      authorization: `Bearer ${this.key}`,
    };
    if (this.session) headers["mcp-session-id"] = this.session;
    if (this.protocol) headers["mcp-protocol-version"] = this.protocol;
    let response;
    try {
      response = await fetch(this.endpoint, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        // A redirect would carry the request somewhere this job was not configured to send it.
        redirect: "error",
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
    } catch (error) {
      throw new Refusal(`cannot reach the MCP endpoint ${this.endpoint} (${error.cause?.message ?? error.message})`);
    }
    const session = response.headers.get("mcp-session-id");
    if (session && !this.session) this.session = session;
    return response;
  }

  async request(method, params) {
    const id = this.nextId++;
    const response = await this.post({ jsonrpc: "2.0", id, method, params });
    const text = await readBody(response);
    if (!response.ok) {
      throw new Refusal(`${method}: HTTP ${response.status} ${text.slice(0, 300)}`);
    }
    const type = response.headers.get("content-type") ?? "";
    let candidates;
    if (type.includes("text/event-stream")) {
      candidates = parseSse(text);
    } else {
      try {
        const parsed = JSON.parse(text);
        candidates = Array.isArray(parsed) ? parsed : [parsed];
      } catch {
        throw new Refusal(`${method}: the MCP endpoint did not answer with JSON`);
      }
    }
    const message = candidates.find((m) => m && m.id === id && ("result" in m || "error" in m));
    if (!message) throw new Refusal(`${method}: no JSON-RPC response with id ${id}`);
    if (message.error) {
      throw new Refusal(`${method}: ${message.error.message ?? "error"} (code ${message.error.code})`);
    }
    return message.result;
  }

  async notify(method, params = {}) {
    const response = await this.post({ jsonrpc: "2.0", method, params });
    await readBody(response);
    if (!response.ok) throw new Refusal(`${method}: HTTP ${response.status}`);
  }

  async initialize() {
    const result = await this.request("initialize", {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: "noru-grc-engineering", version: toolkitVersion() },
    });
    this.protocol = result?.protocolVersion ?? PROTOCOL_VERSION;
    await this.notify("notifications/initialized");
  }

  async callTool(name, args) {
    return this.request("tools/call", { name, arguments: args ?? {} });
  }

  async close() {
    if (!this.session) return;
    try {
      await fetch(this.endpoint, {
        method: "DELETE",
        headers: { authorization: `Bearer ${this.key}`, "mcp-session-id": this.session },
        redirect: "error",
        signal: AbortSignal.timeout(5_000),
      });
    } catch {
      // Ending a session is a courtesy; the server expires it either way.
    }
  }
}

function toolkitVersion() {
  try {
    return JSON.parse(readFileSync(join(ROOT, "plugins", "noru", ".codex-plugin", "plugin.json"), "utf8")).version;
  } catch {
    return "0.0.0";
  }
}

/** The JSON a tool returned. Tool output is untrusted data: decoded and compared, never obeyed. */
export function toolPayload(name, result) {
  const texts = (result?.content ?? []).filter((c) => c?.type === "text").map((c) => c.text);
  if (result?.isError) {
    throw new Refusal(`${name} returned an error: ${texts.join(" ").slice(0, 300) || "(no detail)"}`);
  }
  if (result && result.structuredContent !== undefined) return result.structuredContent;
  if (texts.length !== 1) {
    throw new Refusal(`${name} returned ${texts.length} text content items; expected exactly one`);
  }
  try {
    return JSON.parse(texts[0]);
  } catch {
    throw new Refusal(`${name} did not return JSON`);
  }
}

/** What getMcpCapabilities says about the connection the key actually reaches. */
async function readConnection(client, endpoint) {
  const caps = toolPayload("getMcpCapabilities", await client.callTool("getMcpCapabilities", {}));
  const organization = caps?.organization;
  const granted = caps?.connection?.grantedScopes;
  if (!organization?.id || !organization?.name || !Array.isArray(granted)) {
    throw new Refusal("getMcpCapabilities did not return an organization and granted scopes");
  }
  const names = (list) => new Set((Array.isArray(list) ? list : []).map((tool) => tool?.name));
  return {
    organization: { id: organization.id, name: organization.name },
    endpoint,
    scopes: [...new Set(granted.filter((s) => typeof s === "string"))].sort(),
    read: names(caps?.capabilities?.read),
    write: names(caps?.capabilities?.write),
  };
}

// --- context ----------------------------------------------------------------------------------

async function loadPiece(opts) {
  const dir = join(opts.plugins, opts.piece);
  const decl = readJson(join(dir, "piece.json"), "piece declaration");
  const library = join(dir, "scripts", "lib", "plan.mjs");
  if (!existsSync(library)) throw new Refusal(`the piece has no plan library at ${library}`);
  const lib = await import(pathToFileURL(library).href);
  pieceRedact = lib.redact;
  return { dir, decl, lib };
}

function requireKey() {
  // Read here, at the point of use, and handed to the client that sends it — never written anywhere.
  const key = process.env.NORU_API_KEY;
  if (!key) {
    throw new Refusal(
      "NORU_API_KEY is not set, so this job cannot reach Noru. Expected on a fork pull request.",
    );
  }
  return key;
}

// --- state ------------------------------------------------------------------------------------

export function validateStateDeclaration(state) {
  if (!state || state.headless !== true) {
    throw new Refusal(
      `this piece declares no headless state${state?.reason ? `: ${state.reason}` : ""}. ` +
        "Write .noru/.cache/noru-state.json in an authenticated MCP host and pass it with --state",
    );
  }
  const into = new Set();
  for (const read of state.reads ?? []) {
    if (!/^(get|list|find)[A-Z]/.test(read.tool ?? "")) {
      throw new Refusal(`state read '${read.tool}' is not named as a read tool`);
    }
    if (read.into === "connection" || read.into === "fetched_at" || into.has(read.into)) {
      throw new Refusal(`state read into '${read.into}' collides with another key`);
    }
    into.add(read.into);
  }
}

async function readAll(client, read) {
  if (!read.page) {
    const payload = toolPayload(read.tool, await client.callTool(read.tool, read.arguments ?? {}));
    const value = read.result ? payload?.[read.result] : payload;
    if (value === undefined) throw new Refusal(`${read.tool} returned no '${read.result}'`);
    return value;
  }
  const rows = [];
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const args = { ...(read.arguments ?? {}), limit: read.page.limit, offset: rows.length };
    const payload = toolPayload(read.tool, await client.callTool(read.tool, args));
    const batch = payload?.[read.result];
    const total = payload?.total;
    if (!Array.isArray(batch) || !Number.isInteger(total)) {
      throw new Refusal(`${read.tool} returned no '${read.result}' list with an integer total`);
    }
    rows.push(...batch);
    if (rows.length >= total || batch.length === 0) {
      if (rows.length < total) {
        throw new Refusal(`${read.tool} stopped at ${rows.length} of ${total}; an incomplete snapshot would plan duplicates`);
      }
      return rows;
    }
  }
  throw new Refusal(`${read.tool} needed more than ${MAX_PAGES} pages; refusing an incomplete snapshot`);
}

async function commandState(opts) {
  const endpoint = configuredEndpoint();
  const { decl } = await loadPiece(opts);
  validateStateDeclaration(decl.state);
  const client = new McpClient(endpoint, requireKey());
  try {
    await client.initialize();
    const connection = await readConnection(client, endpoint);
    for (const read of decl.state.reads) {
      if (!connection.read.has(read.tool) || connection.write.has(read.tool)) {
        throw new Refusal(`${read.tool} is not a read tool this connection offers`);
      }
    }
    const snapshot = {
      fetched_at: new Date().toISOString(),
      connection: {
        organization: connection.organization,
        endpoint: connection.endpoint,
        scopes: connection.scopes,
      },
    };
    const counts = [];
    for (const read of decl.state.reads) {
      snapshot[read.into] = await readAll(client, read);
      counts.push({
        into: read.into,
        tool: read.tool,
        count: Array.isArray(snapshot[read.into]) ? snapshot[read.into].length : null,
      });
    }
    const path = join(opts.repo, ".noru", ".cache", "noru-state.json");
    writeJsonAtomic(path, snapshot);
    return { ok: true, piece: opts.piece, state: path, organization_id: connection.organization.id, reads: counts };
  } finally {
    await client.close();
  }
}

// --- exec -------------------------------------------------------------------------------------

/** The id a create returned, for a later call that depends on it. The shape is not documented. */
export function idFrom(payload) {
  if (payload && typeof payload.id === "string" && payload.id) return payload.id;
  if (payload && typeof payload === "object") {
    const nested = Object.values(payload).filter(
      (v) => v && typeof v === "object" && !Array.isArray(v) && typeof v.id === "string" && v.id,
    );
    if (nested.length === 1) return nested[0].id;
  }
  return null;
}

/** The call list must be exactly what the plan says, or it is not the plan a person agreed to. */
export function assertCallsMatchPlan(calls, plan, decl) {
  if (calls.confirmed !== true) throw new Refusal("the call list was not written by a confirmed :push");
  if (calls.manifest_sha256 !== plan.manifest_sha256) {
    throw new Refusal("the call list and the plan describe different manifest bytes — re-run :diff and :push");
  }
  const declared = new Set((decl.push?.operations ?? []).map((op) => op.name));
  const expected = plan.operations.filter((op) => op.effect !== "skip" && op.operation);
  const list = Array.isArray(calls.calls) ? calls.calls : [];
  if (list.length !== expected.length) {
    throw new Refusal(`the call list has ${list.length} call(s); the plan has ${expected.length} to perform`);
  }
  list.forEach((call, i) => {
    const op = expected[i];
    if (call.order !== i + 1 || call.tool !== op.operation || canonical(call.arguments) !== canonical(op.arguments)) {
      throw new Refusal(`call ${i + 1} is not the plan's operation ${i + 1} (${op.operation}); refusing an edited call list`);
    }
    if (call.transport !== "mcp" || !declared.has(call.tool)) {
      throw new Refusal(`call ${i + 1} uses ${call.tool}, which this piece does not declare as an MCP write`);
    }
    if (call.error) throw new Refusal(`call ${i + 1}: ${call.error}`);
    // The substitution is the one thing an executor may change about a call, so it is bound to the
    // plan too: the same field, filled from the same earlier operation, and only where the plan left
    // that field empty.
    if (Boolean(call.depends_on) !== Boolean(op.depends_on)) {
      throw new Refusal(`call ${i + 1} does not carry the plan's dependency; refusing an edited call list`);
    }
    if (call.depends_on) {
      const source = expected[call.depends_on.order - 1];
      if (
        !(call.depends_on.order >= 1 && call.depends_on.order < call.order) ||
        call.depends_on.field !== op.depends_on.field ||
        source !== plan.operations[op.depends_on.operation_index] ||
        (op.arguments ?? {})[op.depends_on.field] != null
      ) {
        throw new Refusal(`call ${i + 1} substitutes something the plan does not; refusing an edited call list`);
      }
    }
  });
  return list;
}

async function commandExec(opts) {
  const endpoint = configuredEndpoint();
  const { decl, lib } = await loadPiece(opts);
  const transports = new Set((decl.push?.operations ?? []).map((op) => op.transport));
  if (transports.size !== 1 || !transports.has("mcp")) {
    throw new Refusal(`${opts.piece} does not publish over MCP; its own :push performs its writes`);
  }
  const cache = join(opts.repo, ".noru", ".cache");
  const planPath = lib.planPathFor(opts.repo, opts.piece);
  const callsPath = join(cache, `${opts.piece}.calls.json`);
  const receiptPath = join(cache, `${opts.piece}.receipt.json`);

  const loaded = lib.readPlan(planPath);
  if (!loaded.ok) throw new Refusal(loaded.reason);
  const plan = loaded.plan;
  const fresh = lib.assertPlanFresh(plan, join(opts.repo, decl.artifact));
  if (!fresh.ok) throw new Refusal(fresh.reason);
  const calls = readJson(callsPath, "confirmed call list");
  const list = assertCallsMatchPlan(calls, plan, decl);

  if (normalizedUrl(plan.target?.mcp_endpoint) !== normalizedUrl(endpoint)) {
    throw new Refusal(
      `the plan is bound to ${plan.target?.mcp_endpoint}, but this job is configured for ${endpoint} — ` +
        "re-run state and :diff against the configured endpoint",
    );
  }

  const receipt = {
    piece: opts.piece,
    plan_sha256: sha256(readFileSync(planPath)),
    calls_sha256: sha256(readFileSync(callsPath)),
    manifest: plan.manifest,
    manifest_sha256: plan.manifest_sha256,
    target: plan.target,
    repository: plan.repository,
    started_at: new Date().toISOString(),
    finished_at: null,
    ok: false,
    calls: list.map((call) => ({ order: call.order, tool: call.tool, subject: call.subject, status: "not_run" })),
  };
  const finish = (ok) => {
    receipt.ok = ok;
    receipt.finished_at = new Date().toISOString();
    writeJsonAtomic(receiptPath, receipt);
    return receipt;
  };

  if (list.length === 0) {
    // Nothing to write is the expected second run. No connection is opened and no key is read.
    finish(true);
    return { ok: true, piece: opts.piece, written: 0, failed: 0, not_run: 0, receipt: receiptPath };
  }

  const client = new McpClient(endpoint, requireKey());
  try {
    await client.initialize();
    const connection = await readConnection(client, endpoint);
    if (connection.organization.id !== plan.target.organization_id) {
      throw new Refusal(
        `the key reaches organization ${connection.organization.id}, but the plan was built for ` +
          `${plan.target.organization_id} — nothing was written`,
      );
    }
    const missing = (plan.required_scopes ?? []).filter((s) => !scopeGranted(connection.scopes, s));
    if (missing.length > 0) throw new Refusal(`the connection lacks required scope(s): ${missing.join(", ")}`);
    const unavailable = [...new Set(list.map((c) => c.tool))].filter((t) => !connection.write.has(t));
    if (unavailable.length > 0) {
      throw new Refusal(`the connection does not offer ${unavailable.join(", ")} — nothing was written`);
    }

    const results = new Map();
    for (const [i, call] of list.entries()) {
      const row = receipt.calls[i];
      const args = { ...(call.arguments ?? {}) };
      if (call.depends_on) {
        const value = idFrom(results.get(call.depends_on.order));
        if (!value) {
          row.status = "error";
          row.error = `no id in the result of call ${call.depends_on.order} to substitute into ${call.depends_on.field}`;
          finish(false);
          return summarize(receipt, receiptPath);
        }
        args[call.depends_on.field] = value;
        row.substituted = { [call.depends_on.field]: value };
      }
      const started = Date.now();
      try {
        const result = await client.callTool(call.tool, args);
        row.response_sha256 = sha256(JSON.stringify(result ?? null));
        row.duration_ms = Date.now() - started;
        results.set(call.order, toolPayloadLenient(call.tool, result));
        row.status = "ok";
      } catch (error) {
        row.status = "error";
        row.duration_ms = Date.now() - started;
        row.error = scrub(error.message).slice(0, 400);
        finish(false);
        return summarize(receipt, receiptPath);
      }
    }
    finish(true);
    return summarize(receipt, receiptPath);
  } catch (error) {
    // Refused after connecting and before the first write: say so in the receipt as well, so the
    // file a reviewer opens never looks like a run that simply has not happened yet.
    receipt.refused = scrub(error.message).slice(0, 400);
    finish(false);
    throw error;
  } finally {
    await client.close();
  }
}

/** A write may answer with prose. Only a call another call depends on needs JSON back. */
function toolPayloadLenient(name, result) {
  if (result?.isError) toolPayload(name, result);
  try {
    return toolPayload(name, result);
  } catch {
    return null;
  }
}

function summarize(receipt, receiptPath) {
  const count = (status) => receipt.calls.filter((c) => c.status === status).length;
  return {
    ok: receipt.ok,
    piece: receipt.piece,
    written: count("ok"),
    failed: count("error"),
    not_run: count("not_run"),
    receipt: receiptPath,
    ...(receipt.ok ? {} : { error: receipt.calls.find((c) => c.status === "error")?.error }),
  };
}

// --- entrypoint -------------------------------------------------------------------------------

export function parseArgs(argv) {
  const opts = { command: null, piece: null, repo: process.cwd(), plugins: join(ROOT, "plugins"), json: false, quiet: false };
  for (const arg of argv) {
    if (arg === "state" || arg === "exec") {
      if (opts.command) throw new UsageError("give exactly one of state or exec");
      opts.command = arg;
    } else if (arg.startsWith("--piece=")) opts.piece = arg.slice(8);
    else if (arg.startsWith("--repo=")) opts.repo = arg.slice(7);
    else if (arg.startsWith("--plugins=")) opts.plugins = arg.slice(10);
    else if (arg === "--output=json") opts.json = true;
    else if (arg === "--output=text") opts.json = false;
    else if (arg === "--quiet") opts.quiet = true;
    else if (arg === "-h" || arg === "--help") opts.help = true;
    else throw new UsageError(`unknown argument '${arg}'`);
  }
  if (opts.help) return opts;
  if (!opts.command) throw new UsageError("state or exec is required");
  if (!opts.piece || !/^[a-z][a-z0-9-]*$/.test(opts.piece)) throw new UsageError("--piece=<name> is required");
  if (!existsSync(opts.repo)) throw new UsageError(`--repo=${opts.repo} does not exist`);
  opts.repo = realpathSync(resolve(opts.repo));
  opts.plugins = resolve(opts.plugins);
  return opts;
}

function render(result, opts) {
  if (opts.json) {
    process.stdout.write(`${scrub(JSON.stringify(result, null, opts.quiet ? 0 : 2))}\n`);
    return;
  }
  if (opts.quiet) return;
  if (opts.command === "state") {
    process.stdout.write(
      `wrote ${result.state}\n` + result.reads.map((r) => `  ${r.into}: ${r.count ?? "1"} from ${r.tool}`).join("\n") + "\n",
    );
  } else {
    process.stdout.write(
      `${result.written} written, ${result.failed} failed, ${result.not_run} not run — receipt ${result.receipt}\n`,
    );
  }
}

async function main(argv) {
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (error) {
    process.stderr.write(`error: ${scrub(error.message)}\n${USAGE}`);
    return 2;
  }
  if (opts.help) {
    process.stdout.write(USAGE);
    return 0;
  }
  try {
    const result = opts.command === "state" ? await commandState(opts) : await commandExec(opts);
    render(result, opts);
    if (!result.ok) {
      process.stderr.write(`error: ${scrub(result.error ?? "a call failed")}\n`);
      return 1;
    }
    return 0;
  } catch (error) {
    if (error instanceof UsageError) {
      process.stderr.write(`error: ${scrub(error.message)}\n`);
      return 2;
    }
    if (opts.json) {
      process.stdout.write(`${scrub(JSON.stringify({ ok: false, piece: opts.piece, error: error.message }))}\n`);
    }
    process.stderr.write(`error: ${scrub(error instanceof Refusal ? error.message : error.stack ?? error.message)}\n`);
    return 1;
  }
}

function invokedAsScript() {
  try {
    return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    return false;
  }
}

if (invokedAsScript()) {
  main(process.argv.slice(2)).then((code) => process.exit(code));
}
