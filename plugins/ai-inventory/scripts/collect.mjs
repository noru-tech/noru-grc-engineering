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
import {
  existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync,
} from "node:fs";
import { join, relative, sep, basename } from "node:path";

export const PIECE = "ai-inventory";
export const VERSION = "0.4.0";
const GENERATED_BY = `${PIECE}@${VERSION}`;

// Directories that are in the repository but are not the repository: a checked-in vendor/ or dist/
// holds a dependency's model calls or a build's output, not anything this codebase decided to run.
// This is a second filter on top of git's answer below, not the primary one — a denylist can only
// ever name the directories its author has already seen.
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

// --- EU AI Act signals ------------------------------------------------------------------------
//
// Everything below finds *signals*, not conclusions. A grep hit is not a determination about a
// customer's regulatory position, and the collector never writes one: signals reach the manifest as
// findings marked `needs_review: true` with a TODO interpretation, which the validator rejects until
// a person has decided them.
//
// Article 5(1) practices. Deliberately narrow — a false "you may be running a prohibited practice"
// is expensive to deliver and expensive to withdraw. `also` means the line must match both patterns,
// which is what keeps workplace emotion inference apart from the word "emotion" in a changelog.
// Article 5(1)(b), exploitation of vulnerabilities, has no pattern here: it turns on who the users
// are and what the system does to them, which is not visible in a line of code. It stays in the
// vocabulary so a person can record it; it is simply not something this scan can raise.
export const ART5_SIGNATURES = [
  {
    practice: "subliminal_or_manipulative_techniques",
    article: "Article 5(1)(a)",
    re: /\bsubliminal\b|\bmanipulat(?:e|es|ing|ive)[-_ ]?(?:user|users|behaviou?r)\b/i,
  },
  {
    practice: "social_scoring",
    article: "Article 5(1)(c)",
    re: /\b(?:social|citizen|trustworthiness|reputation)[-_ ]?scor(?:e|es|ing)\b/i,
  },
  {
    practice: "individual_crime_prediction",
    article: "Article 5(1)(d)",
    re: /\bpredictive[-_ ]?policing\b|\b(?:crime|criminal|recidivism|reoffend(?:ing)?)[-_ ]?(?:predict(?:ion|or)?|risk[-_ ]?scor(?:e|ing))\b/i,
  },
  {
    practice: "untargeted_facial_scraping",
    article: "Article 5(1)(e)",
    re: /\b(?:scrape|scraping|crawl|harvest)[-_ ]?(?:face|faces|facial)\b|\bfacial[-_ ]?recognition[-_ ]?(?:database|db|index)\b/i,
  },
  {
    practice: "emotion_inference_workplace_or_education",
    article: "Article 5(1)(f)",
    re: /\b(?:emotion|affect|mood)[-_ ]?(?:recognition|detect(?:ion|or)?|infer(?:ence)?|analysis|scor(?:e|ing))\b/i,
    // No trailing \b on the context tokens: real code writes `candidateVideo`, not `candidate`.
    // `exam` keeps its boundary, because without one it matches `example` in every repository.
    also: /\b(?:employees?|workers?|staff|workplace|candidates?|applicants?|interview|students?|pupils?|classroom|proctor)|\bexams?\b/i,
  },
  {
    practice: "sensitive_biometric_categorisation",
    article: "Article 5(1)(g)",
    re: /\bbiometric|\bface[-_ ]?(?:attribute|classif|categor)/i,
    also: /\brac(?:e|ial)|\bethnicit|\breligio(?:n|us)|\bsexual[-_ ]?orientation|\bsex[-_ ]?life|\bpolitical[-_ ]?(?:opinion|affiliation|view)|\btrade[-_ ]?union/i,
  },
  {
    practice: "realtime_remote_biometric_identification",
    article: "Article 5(1)(h)",
    re: /\breal[-_ ]?time\b/i,
    also: /\bremote[-_ ]?biometric|\blive[-_ ]?face[-_ ]?(?:match|recognition|identification)\b|\bpublic[-_ ]?space[-_ ]?(?:camera|surveillance)\b/i,
  },
];

// Article 50 triggers, with the paragraph each one comes from and what that paragraph asks for.
// Informing a person (50(1), 50(3)) and marking an output (50(2)) are different duties and one does
// not satisfy the other, so the required action travels with the trigger rather than being inferred
// later.
export const ART50_TRIGGERS = [
  {
    trigger: "direct_human_interaction",
    article: "Article 50(1)",
    required_action: "inform_natural_person",
    applies_to_role: "provider",
    re: /\buse[-_]?chat\b|\bchat[-_ ]?(?:bot|widget|panel|window|session|thread|route|ui)\b|\bchatbot\b|\bvirtual[-_ ]?(?:assistant|agent)\b|\blive[-_ ]?chat\b|\bconversational[-_ ]?(?:ui|agent|interface)\b|["'`]\/api\/chat/i,
  },
  {
    trigger: "synthetic_content_generation",
    article: "Article 50(2)",
    required_action: "machine_readable_marking",
    applies_to_role: "provider",
    re: /\bimages?\.generate\b|\bgenerate[-_]?image\b|\btext[-_ ]?to[-_ ]?(?:image|speech|video)\b|\bdall[-_ ]?e\b|\bstable[-_ ]?diffusion\b|\bspeech\.create\b|\belevenlabs\b|\bsynthesi[sz]e[-_ ]?(?:speech|voice|audio)\b|\bvideo[-_ ]?generat/i,
  },
  {
    // Article 3(39) grounds emotion recognition in biometric data, so plain text sentiment analysis
    // is deliberately not a trigger here. Requiring a biometric-input token on the same line is what
    // keeps a sentiment score on a support ticket out of this category.
    trigger: "emotion_recognition",
    article: "Article 50(3)",
    required_action: "inform_natural_person",
    applies_to_role: "deployer",
    re: /\b(?:emotion|affect|mood)[-_ ]?(?:recognition|detect(?:ion|or)?|infer(?:ence)?|analysis|scor(?:e|ing))\b/i,
    also: /\b(?:face|facial|voice|speech|audio|video|webcam|camera|gaze|expression|biometric)/i,
  },
  {
    trigger: "biometric_categorisation",
    article: "Article 50(3)",
    required_action: "inform_natural_person",
    applies_to_role: "deployer",
    re: /\bbiometric[-_ ]?(?:categor|classif|attribute)|\bface[-_ ]?(?:recognition|detect(?:ion|or)?|match|embedding)\b|\bface[-_]?api\b|\brekognition\b|\bvoice[-_ ]?print\b|\bspeaker[-_ ]?(?:id|identification|recognition)\b|\biris[-_ ]?scan\b|\bgait[-_ ]?analysis\b/i,
  },
  {
    trigger: "deep_fake",
    article: "Article 50(4)",
    required_action: "disclose_artificial_content",
    applies_to_role: "deployer",
    re: /\bdeep[-_ ]?fake\b|\bface[-_ ]?swap\b|\bvoice[-_ ]?clon(?:e|ing)\b|\blip[-_ ]?sync\b/i,
  },
  {
    // The weakest of the six by a distance: whether generated text is "published to inform the
    // public on matters of public interest" is an editorial fact, not a code fact.
    trigger: "public_interest_text",
    article: "Article 50(4)",
    required_action: "disclose_artificial_content",
    applies_to_role: "deployer",
    re: /\bauto[-_ ]?publish\b|\b(?:publish|post)[-_ ]?(?:article|story|news|release)\b/i,
    also: /\b(?:generat(?:e|ed|ion)|llm|model|completion|prompt)\b/i,
  },
];

// What a disclosure or a mark looks like in a repository.
//
// `user_disclosure` is text a person reads. The multilingual pattern is a concept-stem pair rather
// than a phrase list: a synthetic-content token AND a generated-stem token on the same line. That
// catches "detta svar är AI-genererat" and "généré par IA" without an unmaintainable phrase table,
// and requiring both tokens keeps the very common bare "ai" from matching everything.
//
// `machine_readable_marking` is Article 50(2)'s different question: a mark that travels with the
// artifact. A caption in the interface is not one.
export const DISCLOSURE_SIGNATURES = [
  {
    kind: "user_disclosure",
    re: /\bai[-_ ]?(?:disclosure|disclaimer|notice|banner|badge|label)\b|\b(?:is|was)[-_ ]?ai[-_ ]?generated\b|\bgenerated[-_ ]?by[-_ ]?ai\b/i,
  },
  {
    kind: "user_disclosure",
    re: /\bai[-_ ]?generated\b|\bgenerated (?:by|with|using) (?:ai|artificial intelligence)\b|\bartificially[-_ ]?generated\b|\bnot a (?:human|real person)\b|\byou(?:'re| are) (?:chatting|talking|speaking) (?:with|to) an? (?:ai|bot|assistant|automated)\b|\bthis is an ai\b|\bi(?:'m| am) an ai\b|\bautomated (?:assistant|response|reply)\b|\bpowered by ai\b/i,
  },
  {
    // The multilingual rule, and the reason it is a stem pair rather than a phrase list: a notice
    // is a notice in Swedish too, and nobody will maintain a table of every way to write one. Both
    // tokens must be on the same line — bare "ai" matches half a codebase on its own.
    //
    // The stem endings are enumerated rather than left open (…[a-z]*) so that "general", "generic"
    // and "generous" do not read as a disclosure. English generated, Swedish genererad/genererat,
    // German generiert, Spanish generado, Italian generato, French généré.
    kind: "user_disclosure",
    re: /\b(?:ai|ia|ki|artificial|artificiell|artificielle|künstlich(?:e|er)?|kunstig)\b/i,
    also: /\bg[eé]n[eé]r(?:at|er|ier|ad|é)[a-zà-ÿ]*\b|\bskapad\b|\berstellt\b/i,
  },
  {
    kind: "machine_readable_marking",
    re: /\bc2pa\b|\bcontent[-_ ]?credentials?\b|\bsynthid\b|\bdigitalsourcetype\b|\btrainedalgorithmicmedia\b|\bprovenance[-_ ]?manifest\b|\bx-ai-generated\b|\bx-generated-by\b/i,
  },
  {
    // Weaker than the provenance standards above: a file named watermark.ts proves intent, not that
    // every output path is marked. Recorded so a reviewer can look, not treated as settled.
    kind: "machine_readable_marking",
    re: /\b(?:invisible[-_ ]?)?watermark(?:ing)?\b|\bsteganograph(?:y|ic)\b/i,
    weak: true,
  },
];

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

// --------------------------------------------------------------------------------------------- //
// Which files are in scope. git decides, wherever there is a git to ask.
//
// `:diff` and CI mode both compare a committed manifest against a fresh scan, and CI scans an
// `actions/checkout` — tracked files, and nothing else. A developer scans a working tree, which
// holds whatever else they keep in it: scratch checkouts, worktrees, unpacked archives, generated
// fixtures. Walking the working tree therefore produces drift nobody can resolve, because the
// manifest can match one of those two environments or the other and never both — and here every
// extra file is a provider, a model id or an Article 50 trigger site attributed to a path that is
// not in the repository at all.
//
// `git ls-files` is the set CI checks out, and it honours .gitignore, .git/info/exclude and the
// user's global excludesfile without this collector reimplementing any of them. It also settles
// two questions a denylist leaves open, and both answers are deliberate:
//
//   * a tracked file that some ignore rule also matches is IN SCOPE. It is in the checkout, so it
//     belongs in the inventory — what git tracks is the definition here, not what git would ignore.
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

// The name half of "is this worth reading", shared by both enumerations below. Two file lists that
// applied different extension rules would answer different questions, which is the disagreement
// this pair exists to remove rather than to relocate.
function isScannableName(name) {
  const dot = name.lastIndexOf(".");
  const ext = dot === -1 ? "" : name.slice(dot);
  return CODE_EXT.has(ext) || name.startsWith(".env");
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
  const paths = raw.split("\0").filter((rel) => rel !== "");
  // An empty list is not the same answer as no answer, and the two have to be separated *here*,
  // before the filters below. A directory inside a work tree but not tracked by it — an unpacked
  // archive, a scratch copy, a repository whose first commit has not happened yet — gets an empty,
  // successful `ls-files`, and must fall back to the disk rather than scan nothing at all. A
  // repository that tracks files of which none are code has still had its scope answered by git,
  // and reading the disk there would put the ignored directories straight back into the scan.
  if (paths.length === 0) return null;
  // A Set because an unmerged path is listed once per conflict stage.
  const out = new Set();
  for (const rel of paths) {
    if (isSkipped(rel)) continue;
    if (!isScannableName(rel.slice(rel.lastIndexOf("/") + 1))) continue;
    let stat;
    try {
      stat = lstatSync(join(repo, rel));
    } catch {
      continue;
    }
    // lstat does not follow, so isFile() is already false for a symlink — excluded here for the
    // same reason walk() excludes one: its target is either in the list already or outside the
    // repository, and neither is a file worth reading twice.
    if (stat.isFile() && stat.size <= MAX_FILE_BYTES) out.add(rel);
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
      const full = join(dir, entry.name);
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) {
        if (SKIP_DIRS.has(entry.name)) continue;
        stack.push(full);
      } else if (entry.isFile()) {
        if (!isScannableName(entry.name)) continue;
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
  return out.map((f) => relative(root, f).split(sep).join("/")).sort(BY_PATH);
}

/**
 * The files to read, and how they were chosen — the second half being the part that has to be
 * reported. A scan of an exported tarball and a scan of a checkout are both legitimate and they do
 * not see the same repository, so which one happened is a fact about the inventory.
 */
export function listFiles(repo) {
  const tracked = trackedFiles(repo);
  if (tracked) return { files: tracked, enumeratedBy: "git" };
  return { files: walk(repo), enumeratedBy: "walk" };
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

function matches(signature, line) {
  if (!signature.re.test(line)) return false;
  return signature.also ? signature.also.test(line) : true;
}

export function dirOf(rel) {
  const cut = rel.lastIndexOf("/");
  return cut === -1 ? "" : rel.slice(0, cut);
}

function fileOfRef(ref) {
  return ref.slice(0, ref.lastIndexOf(":"));
}

/** Shared leading path segments — how "near" one file is to another, deterministically. */
function sharedDepth(a, b) {
  const left = a.split("/");
  const right = b.split("/");
  let i = 0;
  while (i < left.length && i < right.length && left[i] === right[i]) i += 1;
  return i;
}

/**
 * Pair each Article 50 trigger with the disclosure evidence that would satisfy it, and say how far
 * away that evidence is. This is the piece's highest-value output, so the rule it applies is written
 * down rather than left implicit:
 *
 *   present  the required kind of signal is in the SAME FILE as the trigger — the code that runs
 *            the model also emits the notice or the mark
 *   unclear  a signal of the required kind exists in the same directory, or elsewhere in the repo,
 *            but nothing in the scan ties it to this call site
 *   absent   no signal of the required kind was found anywhere that was scanned
 *
 * `absent` never means "there is no disclosure". It means "not in the files this scan read", which
 * is why every check carries `searched`: a notice rendered by a design system, a CMS, a mobile
 * client or another repository is invisible from here, and the manifest has to say so.
 */
export function pairDisclosures(triggerHits, disclosureHits) {
  const byKind = new Map();
  for (const hit of disclosureHits) {
    if (!byKind.has(hit.kind)) byKind.set(hit.kind, []);
    byKind.get(hit.kind).push(hit);
  }

  const groups = new Map();
  for (const hit of triggerHits) {
    const file = fileOfRef(hit.ref);
    const key = `${hit.trigger}\u0000${file}`;
    if (!groups.has(key)) {
      groups.set(key, {
        trigger: hit.trigger,
        article: hit.article,
        required_action: hit.required_action,
        applies_to_role: hit.applies_to_role,
        required_signal: hit.required_action === "machine_readable_marking"
          ? "machine_readable_marking"
          : "user_disclosure",
        file,
        trigger_refs: [],
      });
    }
    const group = groups.get(key);
    if (group.trigger_refs.length < 10) group.trigger_refs.push(hit.ref);
  }

  const checks = [];
  for (const group of groups.values()) {
    const candidates = byKind.get(group.required_signal) ?? [];
    const dir = dirOf(group.file);
    const sameFile = candidates.filter((c) => fileOfRef(c.ref) === group.file);
    const sameDir = candidates.filter(
      (c) => fileOfRef(c.ref) !== group.file && dirOf(fileOfRef(c.ref)) === dir
    );
    const elsewhere = candidates
      .filter((c) => dirOf(fileOfRef(c.ref)) !== dir)
      .slice()
      .sort((a, b) => {
        const near = sharedDepth(fileOfRef(b.ref), group.file) - sharedDepth(fileOfRef(a.ref), group.file);
        if (near !== 0) return near;
        return a.ref < b.ref ? -1 : a.ref > b.ref ? 1 : 0;
      })
      .slice(0, 5);

    let state = "absent";
    let reason =
      `no ${group.required_signal.replace(/_/g, " ")} signal was found anywhere that was scanned`;
    let evidence = [];
    if (sameFile.length > 0) {
      state = "present";
      evidence = sameFile.slice(0, 5).map((c) => c.ref);
      reason = "a signal of the required kind is in the same file as the model call";
    } else if (sameDir.length > 0) {
      state = "unclear";
      evidence = sameDir.slice(0, 5).map((c) => c.ref);
      reason =
        "a signal of the required kind is in the same directory but not in the file that calls the " +
        "model, so nothing in the scan ties it to this surface";
    } else if (elsewhere.length > 0) {
      state = "unclear";
      evidence = elsewhere.map((c) => c.ref);
      reason =
        "a signal of the required kind exists elsewhere in the repository, but nothing in the scan " +
        "ties it to this surface";
    }
    if (sameFile.length > 0 && sameFile.every((c) => c.weak === true)) {
      state = "unclear";
      reason =
        "the only signal found is a weak one (a watermarking or steganography reference), which " +
        "shows intent rather than that every output path is marked";
    }

    checks.push({
      trigger: group.trigger,
      article: group.article,
      required_action: group.required_action,
      applies_to_role: group.applies_to_role,
      required_signal: group.required_signal,
      file: group.file,
      trigger_refs: group.trigger_refs,
      state,
      reason,
      evidence_refs: evidence,
      searched: [
        dir === "" ? "the repository root" : `${dir}/`,
        `the whole repository, for ${group.required_signal.replace(/_/g, " ")} signals`,
      ],
    });
  }

  return checks.sort((a, b) => {
    const key = (c) => `${c.state}\u0000${c.trigger}\u0000${c.file}`;
    const ka = key(a);
    const kb = key(b);
    return ka < kb ? -1 : ka > kb ? 1 : 0;
  });
}

export function scanRepository(repo) {
  const { files, enumeratedBy } = listFiles(repo);
  const providers = new Map();
  const frameworks = new Map();
  const vectorStores = new Map();
  const models = new Map();
  const claims = [];
  const oversight = [];
  const art5 = [];
  const art50 = [];
  const disclosures = [];
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

      for (const signature of ART5_SIGNATURES) {
        if (matches(signature, line)) {
          art5.push({
            practice: signature.practice,
            article: signature.article,
            ref,
            excerpt: line.trim().slice(0, 160),
          });
        }
      }
      for (const signature of ART50_TRIGGERS) {
        if (matches(signature, line)) {
          art50.push({
            trigger: signature.trigger,
            article: signature.article,
            required_action: signature.required_action,
            applies_to_role: signature.applies_to_role,
            ref,
            excerpt: line.trim().slice(0, 160),
          });
        }
      }
      for (const signature of DISCLOSURE_SIGNATURES) {
        if (matches(signature, line)) {
          disclosures.push({
            kind: signature.kind,
            weak: signature.weak === true,
            ref,
            excerpt: line.trim().slice(0, 160),
          });
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

  const art5Signals = capped(art5, 100);
  const art50Triggers = capped(art50, 200);
  const disclosureSignals = capped(disclosures, 200);

  return {
    piece: PIECE,
    generated_by: GENERATED_BY,
    // Which files this scan could even see. A `walk` means the file list is whatever is on disk
    // rather than whatever is committed, so a scan here and a scan in CI can legitimately
    // disagree — and a reader comparing two inventories needs to know that before blaming one.
    //
    // It sits under `coverage` so it is out of the digest: the same file set enumerated two
    // different ways is the same repository, and must not read as drift.
    coverage: { enumerated_by: enumeratedBy },
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
    // The Article 5 screen ran. An empty list is the answer, not silence — and it is only ever an
    // answer about the practices ART5_SIGNATURES can see.
    art5_screened: ART5_SIGNATURES.map((s) => s.practice).sort(),
    art5_signals: art5Signals,
    art50_triggers: art50Triggers,
    disclosure_signals: disclosureSignals,
    art50_disclosure_checks: pairDisclosures(art50Triggers, disclosureSignals).slice(0, 100),
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
  //
  // `coverage` is excluded for the same reason and a sharper one: the manifest does not record it,
  // so an export and a checkout of one commit — the same repository, enumerated two ways — would
  // produce a drift that re-running :scan could never clear.
  const { generated_by, coverage, ...facts } = derived;
  void generated_by;
  void coverage;
  return createHash("sha256").update(JSON.stringify(facts, null, 0)).digest("hex");
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
    findings: buildSkeletonFindings(derived, systems, todo),
  };
}

/**
 * The system a signal most likely belongs to, by file and then by directory. A guess, which is why
 * every finding the skeleton writes carries needs_review: true — the validator refuses to push a
 * manifest that still has one.
 */
function systemForRef(systems, ref) {
  if (systems.length === 0) return null;
  const file = ref.slice(0, ref.lastIndexOf(":"));
  const inFile = systems.find((s) => (s.refs ?? []).some((r) => r.slice(0, r.lastIndexOf(":")) === file));
  if (inFile) return inFile.key;
  const dir = dirOf(file);
  const inDir = systems.find((s) =>
    (s.refs ?? []).some((r) => dirOf(r.slice(0, r.lastIndexOf(":"))) === dir)
  );
  return inDir ? inDir.key : systems[0].key;
}

/**
 * Findings the collector proposes, in the fixed order the schema declares. Two rules it does not
 * break:
 *
 *   * it never writes `determination: indicated`. A pattern match is a reason to look, not a finding
 *     that a prohibited practice is running, and the difference is the whole reason this is a
 *     suggestion rather than an assertion.
 *   * it never writes an Article 50 trigger without the disclosure check that goes with it, because
 *     a trigger on its own is the failure mode the category exists to prevent.
 */
export function buildSkeletonFindings(derived, systems, todo) {
  const prohibited = [];
  const seenArt5 = new Set();
  for (const signal of derived.art5_signals ?? []) {
    const system = systemForRef(systems, signal.ref);
    if (!system) break;
    const key = `${system} ${signal.practice}`;
    if (seenArt5.has(key)) continue;
    seenArt5.add(key);
    prohibited.push({
      system,
      practice: signal.practice,
      article: signal.article,
      determination: "needs_legal_review",
      action:
        "TODO: say what happens next. Article 5 is a prohibition, so if this is confirmed the " +
        "answer is to stop the practice, not to schedule remediation.",
      status: "suggested",
      needs_review: true,
      refs: (derived.art5_signals ?? [])
        .filter((s) => s.practice === signal.practice)
        .slice(0, 5)
        .map((s) => s.ref),
      interpretation: { ...todo },
    });
  }

  const transparency = [];
  for (const check of derived.art50_disclosure_checks ?? []) {
    const system = systemForRef(systems, check.trigger_refs[0]);
    if (!system) break;
    const disclosure = { state: check.state };
    if (check.state === "present") {
      disclosure.mechanism = `TODO: how the disclosure is produced. The scan found: ${check.reason}`;
      disclosure.refs = check.evidence_refs;
    } else {
      disclosure.gap = `TODO: confirm and say what would close it. The scan found: ${check.reason}`;
      if (check.state === "absent") disclosure.searched = check.searched;
      else disclosure.refs = check.evidence_refs;
    }
    transparency.push({
      system,
      trigger: check.trigger,
      article: check.article,
      required_action: check.required_action,
      applies_to_role: check.applies_to_role,
      disclosure,
      status: "suggested",
      needs_review: true,
      refs: check.trigger_refs,
      interpretation: { ...todo },
    });
  }

  // role_and_risk and standards_alignment are left empty on purpose: neither is derivable from a
  // line of code, and a placeholder tier would be exactly the kind of unattributed legal claim this
  // piece exists to avoid. The keys are still written, in order, so the shape is obvious.
  return {
    prohibited_practices: prohibited,
    transparency_obligations: transparency,
    role_and_risk: [],
    standards_alignment: [],
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

/**
 * The two things a reader must not scroll past, lifted out of the counts and printed above
 * everything else. Both are enforceable now: Article 5 has applied since 2 February 2025 and
 * Article 50 since 2 August 2026 (Article 113). Everything else the scan finds can wait for the
 * manifest review; these two cannot be a row in a table.
 */
export function alertsFor(derived) {
  const alerts = [];
  const practices = [...new Set((derived.art5_signals ?? []).map((s) => s.practice))].sort();
  for (const practice of practices) {
    const first = derived.art5_signals.find((s) => s.practice === practice);
    alerts.push({
      severity: "review",
      category: "prohibited_practices",
      message:
        `${first.article} ${practice}: the repository has something that looks like this practice ` +
        `(${first.ref}). A pattern match is not a finding — but Article 5 is a prohibition, so ` +
        "settle it before anything else in this scan.",
    });
  }
  for (const check of derived.art50_disclosure_checks ?? []) {
    if (check.state === "present") continue;
    alerts.push({
      severity: check.state === "absent" ? "gap" : "unresolved",
      category: "transparency_obligations",
      message:
        `${check.article} ${check.trigger} at ${check.file}: the required ` +
        `${check.required_action.replace(/_/g, " ")} is ${check.state} — ${check.reason}.`,
    });
  }
  return alerts;
}

export function renderAlerts(alerts) {
  if (alerts.length === 0) return [];
  const rule = "=".repeat(96);
  return [rule, ...alerts.map((a) => `  ${a.severity.toUpperCase()}: ${a.message}`), rule, ""];
}

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

  const gaps = (derived.art50_disclosure_checks ?? []).filter((c) => c.state !== "present");
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
    alerts: alertsFor(derived),
    counts: {
      files_scanned: derived.files_scanned,
      providers: derived.providers.length,
      frameworks: derived.frameworks.length,
      vector_stores: derived.vector_stores.length,
      models: derived.models.length,
      eval_suites: derived.eval_suites.length,
      claim_sites: derived.claim_sites.length,
      oversight_sites: derived.oversight_sites.length,
      art5_signals: derived.art5_signals.length,
      art50_triggers: derived.art50_disclosure_checks.length,
      art50_disclosure_gaps: gaps.length,
    },
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    process.stdout.write(
      [
        ...renderAlerts(summary.alerts),
        derived.coverage.enumerated_by === "git"
          ? `scanned ${derived.files_scanned} tracked file(s) in ${opts.repo}`
          : `scanned ${derived.files_scanned} file(s) in ${opts.repo} — no tracked file list here, ` +
            "so everything on disk was read and a scan in CI may not agree",
        `providers:      ${derived.providers.map((p) => p.key).join(", ") || "(none)"}`,
        `frameworks:     ${derived.frameworks.map((f) => f.key).join(", ") || "(none)"}`,
        `vector stores:  ${derived.vector_stores.map((v) => v.key).join(", ") || "(none)"}`,
        `models:         ${derived.models.map((m) => m.key).join(", ") || "(none)"}`,
        `eval suites:    ${derived.eval_suites.length} (CI gated: ${derived.evals_ci_gated})`,
        `claim sites:    ${derived.claim_sites.length}`,
        `oversight:      ${derived.oversight_sites.length}`,
        // Printed whether or not anything was found. "The Article 5 screen ran and found nothing"
        // is a different statement from silence, and only one of them is worth anything to a
        // reader who wants to know the check exists.
        `Art. 5 screen:  ${derived.art5_signals.length} signal(s) across ` +
          `${derived.art5_screened.length} screened practice(s)`,
        `Art. 50:        ${derived.art50_disclosure_checks.length} trigger site(s), ` +
          `${gaps.length} with the required disclosure absent or unclear`,
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
