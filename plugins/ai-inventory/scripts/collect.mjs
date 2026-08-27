#!/usr/bin/env node
// Deterministic, offline collector for the ai-inventory piece (contract requirement 2).
//
// Node built-ins only. Opens no socket: everything it knows comes from files in the target
// repository plus `git` for provenance. Same repository state in, byte-identical derived output.
//
// It does NOT write judgement into the manifest. It writes the derived facts it can stand behind
// (which SDK is imported on which line, which model id appears where, whether CI gates the evals)
// to .noru/.cache/ai-inventory.derived.json, and — only when no manifest exists yet — stamps a
// skeleton .noru/ai-inventory.yml with `needs_review: true` on every field a human has to decide.
// If a manifest already exists it is never overwritten: the collector reports the delta and lets
// :diff and a human resolve it. That is what keeps a re-scan from silently editing an attributed
// claim.
//
// Usage:
//   node collect.mjs [--repo=<path>] [--check] [--output=json|text] [--quiet]
// Exit codes: 0 ok, 1 derived facts drifted from the manifest (--check), 2 usage/IO error.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep, basename } from "node:path";

export const PIECE = "ai-inventory";
export const VERSION = "0.1.0";
const GENERATED_BY = `${PIECE}@${VERSION}`;

const SKIP_DIRS = new Set([
  ".git", "node_modules", "dist", "build", "out", ".next", ".turbo", ".cache", "coverage",
  "vendor", "target", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
  ".gradle", ".idea", ".vscode", "Pods", ".terraform", ".noru",
]);

const CODE_EXT = new Set([
  ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rb", ".java", ".kt", ".rs",
  ".cs", ".php", ".swift", ".scala", ".ex", ".exs", ".yml", ".yaml", ".toml", ".json", ".env",
  ".md", ".mdx", ".sh",
]);

const MAX_FILE_BYTES = 1_500_000;

// Provider detection signatures. This is our own detection vocabulary for finding model calls in
// source code — it is not, and must never become, a framework control or evidence catalogue.
// `id` becomes the provider key in the manifest.
export const PROVIDER_SIGNATURES = [
  { id: "openai", vendor: "OpenAI", patterns: ["openai", "@ai-sdk/openai", "AzureOpenAI", "azure-openai"] },
  { id: "anthropic", vendor: "Anthropic", patterns: ["@anthropic-ai", "anthropic", "@ai-sdk/anthropic"] },
  { id: "aws-bedrock", vendor: "Amazon Web Services", patterns: ["bedrock-runtime", "@aws-sdk/client-bedrock", "boto3.client(\"bedrock", "BedrockRuntime"] },
  { id: "google-vertex", vendor: "Google Cloud", patterns: ["@google-cloud/vertexai", "vertexai", "google.generativeai", "@ai-sdk/google"] },
  { id: "azure-openai", vendor: "Microsoft Azure", patterns: ["azure.ai.openai", "@azure/openai", "AZURE_OPENAI"] },
  { id: "mistral", vendor: "Mistral AI", patterns: ["@mistralai", "mistralai", "@ai-sdk/mistral"] },
  { id: "cohere", vendor: "Cohere", patterns: ["cohere-ai", "cohere"] },
  { id: "ollama", vendor: "Ollama", patterns: ["ollama"] },
  { id: "huggingface", vendor: "Hugging Face", patterns: ["huggingface", "@huggingface", "transformers"] },
];

// Frameworks and orchestration layers. Recorded as evidence of an AI system, not as a provider.
export const FRAMEWORK_SIGNATURES = [
  { id: "vercel-ai-sdk", label: "Vercel AI SDK", patterns: ["\"ai\"", "'ai'", "@ai-sdk/", "generateObject", "streamText"] },
  { id: "langchain", label: "LangChain", patterns: ["langchain", "@langchain/"] },
  { id: "llamaindex", label: "LlamaIndex", patterns: ["llama_index", "llamaindex"] },
  { id: "semantic-kernel", label: "Semantic Kernel", patterns: ["semantic_kernel", "semantic-kernel"] },
  { id: "mcp", label: "Model Context Protocol", patterns: ["@modelcontextprotocol", "modelcontextprotocol"] },
];

export const VECTOR_STORE_SIGNATURES = [
  { id: "pinecone", label: "Pinecone", patterns: ["pinecone"] },
  { id: "weaviate", label: "Weaviate", patterns: ["weaviate"] },
  { id: "qdrant", label: "Qdrant", patterns: ["qdrant"] },
  { id: "pgvector", label: "pgvector", patterns: ["pgvector", "vector(1536", "CREATE EXTENSION vector"] },
  { id: "chroma", label: "Chroma", patterns: ["chromadb", "chroma-core"] },
  { id: "milvus", label: "Milvus", patterns: ["milvus"] },
  { id: "lancedb", label: "LanceDB", patterns: ["lancedb"] },
  { id: "faiss", label: "FAISS", patterns: ["faiss"] },
];

// Concrete model identifiers as they appear in source. Deliberately shape-based so a model
// released after this collector was written is still found.
const MODEL_ID_RE =
  /\b(?:gpt-[0-9][a-z0-9.-]*|o[1-9](?:-[a-z0-9-]+)?|claude-[a-z0-9.-]+|gemini-[0-9][a-z0-9.-]*|mistral-[a-z0-9.-]+|llama-?[0-9][a-z0-9.-]*|command-[a-z0-9.-]+|text-embedding-[a-z0-9.-]+|amazon\.titan-[a-z0-9.-]+|anthropic\.claude-[a-z0-9.-]+)\b/g;

// Sites where a retention / training / residency posture is configured or asserted. The collector
// only records that the text exists and where; deciding what it means is a human's job, and the
// decision has to carry an interpretation block.
const CLAIM_PATTERNS = [
  { kind: "zero_retention", re: /\b(zero[-_ ]?data[-_ ]?retention|\bzdr\b|zero[-_ ]?retention)\b/i },
  { kind: "no_training", re: /\b(do[-_ ]?not[-_ ]?train|no[-_ ]?train(?:ing)?|opt[-_ ]?out[-_ ]?of[-_ ]?training|training\s*[:=]\s*false)\b/i },
  { kind: "retention_period", re: /\b(retention[-_ ]?period|retention[-_ ]?days|data[-_ ]?retention)\b/i },
  { kind: "data_residency", re: /\b(data[-_ ]?residency|region[-_ ]?lock|eu[-_ ]?only|in[-_ ]?region)\b/i },
  { kind: "dpa", re: /\b(data[-_ ]?processing[-_ ]?(?:agreement|addendum)|\bdpa\b)\b/i },
];

// Places a human decision is wired into an AI path.
const OVERSIGHT_PATTERNS = [
  { type: "human_approval_gate", re: /\b(requires?[-_ ]?approval|approval[-_ ]?gate|awaiting[-_ ]?approval|needs?[-_ ]?confirmation)\b/i },
  { type: "human_review_before_action", re: /\b(human[-_ ]?review|manual[-_ ]?review|review[-_ ]?before|pending[-_ ]?review|reviewer)\b/i },
  { type: "human_in_the_loop_editing", re: /\b(human[-_ ]?in[-_ ]?the[-_ ]?loop|editable[-_ ]?suggestion|accept[-_ ]?suggestion|suggestion[-_ ]?status)\b/i },
  { type: "override_or_kill_switch", re: /\b(kill[-_ ]?switch|feature[-_ ]?flag|disable[-_ ]?ai|ai[-_ ]?enabled)\b/i },
];

const EVAL_DIR_RE = /(^|\/)(evals?|benchmarks?)(\/|$)/i;
const EVAL_FILE_RE = /\.(eval|evals|bench)\.[cm]?[jt]sx?$|_eval\.py$|test_eval.*\.py$/i;
const PROMPT_RE = /(^|\/)prompts?(\/|$)|\.prompt(\.|$)|system[-_ ]?prompt/i;

function usage(stream = process.stderr) {
  stream.write(
    "usage: collect.mjs [--repo=<path>] [--check] [--output=json|text] [--quiet]\n"
  );
}

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
      const full = join(dir, entry.name);
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) {
        if (SKIP_DIRS.has(entry.name)) continue;
        stack.push(full);
      } else if (entry.isFile()) {
        const dot = entry.name.lastIndexOf(".");
        const ext = dot === -1 ? "" : entry.name.slice(dot);
        if (!CODE_EXT.has(ext) && !entry.name.startsWith(".env")) continue;
        let size = 0;
        try {
          size = statSync(full).size;
        } catch {
          continue;
        }
        if (size > MAX_FILE_BYTES) continue;
        out.push(full);
      }
    }
  }
  // Sort by repo-relative POSIX path so the result never depends on directory iteration order.
  return out
    .map((f) => relative(root, f).split(sep).join("/"))
    .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

function gitValue(repo, args, fallback) {
  try {
    return execFileSync("git", ["-C", repo, ...args], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim() || fallback;
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

function addHit(map, key, ref, extra) {
  if (!map.has(key)) map.set(key, { refs: [], ...extra });
  const entry = map.get(key);
  if (entry.refs.length < 25 && !entry.refs.includes(ref)) entry.refs.push(ref);
  return entry;
}

export function scanRepository(repo) {
  const files = walk(repo);
  const providers = new Map();
  const frameworks = new Map();
  const vectorStores = new Map();
  const models = new Map();
  const claims = [];
  const oversight = [];
  const evalSuites = new Map();
  const promptFiles = [];
  const ciFiles = [];

  for (const rel of files) {
    if (rel.startsWith(".github/workflows/")) ciFiles.push(rel);
    if (EVAL_DIR_RE.test(rel) || EVAL_FILE_RE.test(rel)) {
      const dir = rel.includes("/") ? rel.slice(0, rel.lastIndexOf("/")) : rel;
      addHit(evalSuites, dir, rel, { name: dir });
    }
    if (PROMPT_RE.test(rel)) promptFiles.push(rel);

    let text;
    try {
      text = readFileSync(join(repo, rel), "utf8");
    } catch {
      continue;
    }
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      if (line.length > 2000) continue;
      const ref = `${rel}:${i + 1}`;
      const lower = line.toLowerCase();

      for (const sig of PROVIDER_SIGNATURES) {
        if (sig.patterns.some((p) => lower.includes(p.toLowerCase()))) {
          addHit(providers, sig.id, ref, { vendor: sig.vendor });
        }
      }
      for (const sig of FRAMEWORK_SIGNATURES) {
        if (sig.patterns.some((p) => lower.includes(p.toLowerCase()))) {
          addHit(frameworks, sig.id, ref, { label: sig.label });
        }
      }
      for (const sig of VECTOR_STORE_SIGNATURES) {
        if (sig.patterns.some((p) => lower.includes(p.toLowerCase()))) {
          addHit(vectorStores, sig.id, ref, { label: sig.label });
        }
      }

      MODEL_ID_RE.lastIndex = 0;
      let m;
      while ((m = MODEL_ID_RE.exec(line)) !== null) {
        addHit(models, m[0], ref, {});
      }

      for (const pattern of CLAIM_PATTERNS) {
        if (pattern.re.test(line)) {
          claims.push({ kind: pattern.kind, ref, excerpt: line.trim().slice(0, 160) });
        }
      }
      for (const pattern of OVERSIGHT_PATTERNS) {
        if (pattern.re.test(line)) {
          oversight.push({ type: pattern.type, ref, excerpt: line.trim().slice(0, 160) });
        }
      }
    }
  }

  // An eval suite only counts as a control if something fails when it fails.
  let ciGated = false;
  const ciRefs = [];
  for (const rel of ciFiles) {
    let text;
    try {
      text = readFileSync(join(repo, rel), "utf8");
    } catch {
      continue;
    }
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i += 1) {
      if (/\b(eval|evals|benchmark)\b/i.test(lines[i])) {
        ciGated = true;
        if (ciRefs.length < 10) ciRefs.push(`${rel}:${i + 1}`);
      }
    }
  }

  const byKey = (m) =>
    [...m.entries()]
      .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
      .map(([key, value]) => ({ key, ...value }));

  const capped = (rows, limit) => {
    const sortKey = (row) => `${row.ref}\u0000${row.type ?? row.kind ?? ""}`;
    return rows
      .slice()
      .sort((a, b) => {
        const ka = sortKey(a);
        const kb = sortKey(b);
        return ka < kb ? -1 : ka > kb ? 1 : 0;
      })
      .slice(0, limit);
  };

  return {
    piece: PIECE,
    generated_by: GENERATED_BY,
    files_scanned: files.length,
    providers: byKey(providers),
    frameworks: byKey(frameworks),
    vector_stores: byKey(vectorStores),
    models: byKey(models),
    prompt_files: promptFiles.slice(0, 200),
    eval_suites: byKey(evalSuites),
    evals_ci_gated: ciGated,
    evals_ci_refs: ciRefs,
    claim_sites: capped(claims, 200),
    oversight_sites: capped(oversight, 200),
  };
}

export function digestOf(derived) {
  return createHash("sha256").update(JSON.stringify(derived, null, 0)).digest("hex");
}

// --- minimal deterministic YAML emitter -------------------------------------------------------
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

// --- skeleton ---------------------------------------------------------------------------------
export function buildSkeleton(derived, provenance) {
  const todo = {
    owner: "TODO@example.com",
    decided_at: "TODO-YYYY-MM-DD",
    expires_at: "TODO-YYYY-MM-DD",
    rationale: "TODO: why this claim holds, in a sentence a reviewer can argue with",
  };

  const systems = derived.providers.map((p) => {
    const models = derived.models
      .filter((m) => m.refs.some((r) => p.refs.some((pr) => pr.split(":")[0] === r.split(":")[0])))
      .map((m) => m.key);
    return {
      key: p.key,
      name: `TODO: name the system that calls ${p.vendor}`,
      purpose: "TODO: what this system is for, in one sentence",
      provider: p.key,
      models: models.length > 0 ? models : derived.models.map((m) => m.key).slice(0, 5),
      deployment: "hosted_api",
      autonomy: "assistive",
      human_oversight: [],
      evals: { suites: [], ci_gated: derived.evals_ci_gated },
      data_categories: [],
      refs: p.refs.slice(0, 10),
      interpretation: { ...todo },
      needs_review: true,
    };
  });

  const providers = derived.providers.map((p) => ({
    key: p.key,
    vendor_name: p.vendor,
    category: "software_as_a_service",
    endpoints: [],
    claims: [],
    refs: p.refs.slice(0, 10),
    interpretation: { ...todo },
  }));

  return {
    version: VERSION,
    piece: PIECE,
    source: { ...provenance, derived_digest: digestOf(derived) },
    providers,
    ai_systems: systems,
    classifications: [],
  };
}

const SKELETON_HEADER = `# .noru/ai-inventory.yml — generated by ${GENERATED_BY}
#
# This is a SKELETON. Every TODO below is a decision a person has to make and sign for.
# Rules that the validator enforces (contract requirement 8):
#   * refs[] must cite the repository lines (file:line) that produced each claim
#   * interpretation.owner must be a person, not a team alias
#   * a technical claim must carry expires_at
#   * needs_review: true blocks the push
#
# Run:  python3 <plugin>/scripts/validate_manifest.py .noru/ai-inventory.yml
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
    usage(process.stdout);
    return 0;
  }
  if (opts.error) {
    process.stderr.write(`error: ${opts.error}\n`);
    usage();
    return 2;
  }
  if (!existsSync(opts.repo)) {
    process.stderr.write(`error: no such directory: ${opts.repo}\n`);
    return 2;
  }

  const derived = scanRepository(opts.repo);
  const provenance = repoProvenance(opts.repo);
  const digest = digestOf(derived);
  const cacheDir = join(opts.repo, ".noru", ".cache");
  const manifestPath = join(opts.repo, ".noru", "ai-inventory.yml");
  const derivedPath = join(cacheDir, "ai-inventory.derived.json");

  let wroteSkeleton = false;
  let drift = false;
  try {
    mkdirSync(cacheDir, { recursive: true });
    writeFileSync(derivedPath, `${JSON.stringify(derived, null, 2)}\n`, "utf8");
    const existingDigest = readManifestDigest(manifestPath);
    if (existingDigest === null) {
      if (!opts.check) {
        const skeleton = buildSkeleton(derived, provenance);
        writeFileSync(manifestPath, SKELETON_HEADER + toYaml(skeleton), "utf8");
        wroteSkeleton = true;
      } else {
        drift = true;
      }
    } else if (existingDigest !== digest) {
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
      files_scanned: derived.files_scanned,
      providers: derived.providers.length,
      frameworks: derived.frameworks.length,
      vector_stores: derived.vector_stores.length,
      models: derived.models.length,
      eval_suites: derived.eval_suites.length,
      claim_sites: derived.claim_sites.length,
      oversight_sites: derived.oversight_sites.length,
    },
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    process.stdout.write(
      [
        `scanned ${derived.files_scanned} file(s) in ${opts.repo}`,
        `providers:      ${derived.providers.map((p) => p.key).join(", ") || "(none)"}`,
        `frameworks:     ${derived.frameworks.map((f) => f.key).join(", ") || "(none)"}`,
        `vector stores:  ${derived.vector_stores.map((v) => v.key).join(", ") || "(none)"}`,
        `models:         ${derived.models.map((m) => m.key).join(", ") || "(none)"}`,
        `eval suites:    ${derived.eval_suites.length} (CI gated: ${derived.evals_ci_gated})`,
        `claim sites:    ${derived.claim_sites.length}`,
        `oversight:      ${derived.oversight_sites.length}`,
        `derived facts:  ${summary.derived_facts}`,
        wroteSkeleton ? `wrote skeleton: ${summary.manifest}` : "",
        drift ? "DRIFT: the manifest does not match the repository as it is now" : "",
      ]
        .filter(Boolean)
        .join("\n") + "\n"
    );
  }

  return opts.check && drift ? 1 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  process.exit(main(process.argv.slice(2)));
}
