#!/usr/bin/env node
// Turn an agent-change-control manifest into the forge-neutral file the collector reads, so that
// the separations acc already decides are decided once, by acc, and not again here.
//
//   acc scan --format json -o acc-manifest.json     (credentialed, run by your CI)
//   export/acc.mjs --manifest=acc-manifest.json      (this file, offline)
//                  [--forge-export=change-events.json] [--statement=acc.intoto.json]
//   collect (offline, deterministic)   ->  .noru/change-control.yml
//
// acc decides three of this piece's rules and, when its policy enables ACC005 or ACC012, the fourth:
//
//   approver_is_author                       <- ACC002
//   merged_without_independent_approval      <- ACC003
//   agent_change_without_independent_human   <- ACC001
//   bypass_used                              <- ACC005 (only when acc assessed it)
//   bypass_used                              <- ACC012, on a record of its own per branch update
//
// A branch update ACC012 fails (a direct push, a force push or a deletion of a protected branch
// that no merged change accounts for) is not a change, so it becomes a record of its own: kind
// `direct_push`, keyed on the update, with the bypass set. acc decided it whole, so none of the
// change separations apply to it. An ACC012 that is `unknown` for a branch (its record could not be
// read in full) becomes one such record for the branch, for a person to say whether it held.
//
// --statement stages acc's in-toto Statement (`acc ... --format in-toto`) in .noru/artifacts/ under
// one name per window, acc-<opens>_<closes>.intoto.json, for evidence-push to upload. It never
// edits .noru/evidence-push.yml: an upload entry is a person's signature over one file's sha256, so
// evidence-push's own collector drafts the entry, or reports the file changed, and a person signs.
//
// `deployer_is_author` stays here: acc has no deployment facts and no rule about who deployed.
//
// acc records nothing about deployments or branch protection settings. When the GitHub or GitLab
// exporter has also run (--forge-export), its change records, deployments and settings are kept
// and acc's verdicts are added to them; without it, the change records are built from the acc
// manifest and carry no deployment.
//
// Reads files only. Opens no socket, reads no credential.
//
// Usage:
//   node acc.mjs --manifest=<acc-manifest.json> [--forge-export=<path>] [--out=<path>]
//                [--statement=<acc.intoto.json>] [--artifacts=<dir>] [--output=json|text] [--quiet]
// Exit codes: 0 written, 1 the manifest is not a usable acc manifest, 2 usage error.

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

const USAGE =
  "usage: acc.mjs --manifest=<acc-manifest.json> [--forge-export=<path>] [--out=<path>]\n" +
  "               [--statement=<acc.intoto.json>] [--artifacts=<dir>] [--output=json|text] [--quiet]\n";
const OUT_REL = ".noru/.cache/change-events.json";
const ARTIFACTS_REL = ".noru/artifacts";
const STATEMENT_TYPE = "https://in-toto.io/Statement/v1";
const ACP_PREDICATE = "https://noru.tech/spec/ai-change-provenance/";
/** The ACP versions whose manifests this reads. */
const ACP_VERSIONS = ["0.3", "0.4"];
/** acc rule -> the verdict this piece records for it. */
export const MAPPED_RULES = ["ACC001", "ACC002", "ACC003", "ACC005", "ACC012"];

/** acc's branch update kind -> the bypass this piece records for it. */
const BRANCH_BYPASS = {
  push: "direct_push",
  force_push: "force_push",
  branch_deletion: "branch_deletion",
  branch_creation: "direct_push",
  pr_merge: "direct_push",
  merge_queue_merge: "direct_push",
};

function parseArgs(argv) {
  const opts = {
    manifest: null, forgeExport: null, out: OUT_REL, statement: null, artifacts: ARTIFACTS_REL,
    json: false, quiet: false,
  };
  for (const arg of argv) {
    if (arg.startsWith("--manifest=")) opts.manifest = arg.slice(11);
    else if (arg.startsWith("--statement=")) opts.statement = arg.slice(12);
    else if (arg.startsWith("--artifacts=")) opts.artifacts = arg.slice(12);
    else if (arg.startsWith("--forge-export=")) opts.forgeExport = arg.slice(15);
    else if (arg.startsWith("--out=")) opts.out = arg.slice(6);
    else if (arg === "--output=json") opts.json = true;
    else if (arg === "--output=text") opts.json = false;
    else if (arg === "--quiet") opts.quiet = true;
    else if (arg === "-h" || arg === "--help") return { help: true };
    else return { error: `unknown option '${arg}'` };
  }
  if (!opts.manifest) return { error: "--manifest is required" };
  return opts;
}

function day(value) {
  return typeof value === "string" && value.length >= 10 ? value.slice(0, 10) : null;
}

/** `github:login` -> the name the GitHub exporter gives the same account. */
export function personOf(actorId) {
  if (typeof actorId !== "string") return null;
  const [namespace, name] = actorId.split(/:(.*)/s);
  if (!name) return null;
  if (namespace === "github") return `${name}@users.noreply.github.com`;
  if (namespace === "gitlab") return `${name}@users.noreply.gitlab.com`;
  return null;
}

/** `github:OWNER/REPO:pr:N` -> `pr-N`, the key the GitHub exporter uses. */
export function keyOf(changeId) {
  const match = /:(pr|mr):(\d+)$/.exec(String(changeId));
  return match ? `${match[1]}-${match[2]}` : null;
}

const REVIEW_STATE = {
  approved: "approved",
  changes_requested: "changes_requested",
  dismissed: "dismissed",
  commented: "commented",
};

/** One merged acc change as this piece's change record, for when no forge export has it. */
function recordFromAcc(change, actors) {
  const author = change.author?.actor_id;
  const isAgent = actors[author]?.kind === "agent";
  const forgeAuthor = personOf(change.forge_author?.actor_id);
  const operator = personOf(change.agent_operator?.actor_id);
  return {
    key: keyOf(change.id),
    kind: "pull_request",
    title: change.title,
    authored_by: isAgent ? forgeAuthor ?? author : personOf(author) ?? author,
    author_kind: isAgent ? "agent" : "human",
    ...(isAgent && operator ? { agent_operator: operator } : {}),
    opened_on: day(change.opened_at),
    approvals: (change.reviews ?? [])
      .filter((r) => REVIEW_STATE[r.state] && personOf(r.actor_id))
      .map((r) => ({
        by: personOf(r.actor_id),
        state: REVIEW_STATE[r.state],
        reviewed_on: day(r.at),
      })),
    ...(change.merger ? { merged_by: personOf(change.merger.actor_id) } : {}),
    merged_on: day(change.merged_at),
    ...(change.merge_commit_sha ? { artifact_digest: change.merge_commit_sha } : {}),
    bypass: { used: false },
    url: change.url,
  };
}

/** A file-name-safe form of a branch name, for a record key. */
function slug(value) {
  return String(value).replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "branch";
}

/**
 * The branch updates ACC012 holds against, one record each: a failing update from its finding, and
 * a branch whose record acc could not read in full from its `unknown` assessment.
 */
function branchRecords(manifest) {
  const branches = new Map((manifest.events.branches ?? []).map((b) => [b.id, b]));
  const records = [];
  for (const finding of manifest.findings ?? []) {
    if (finding.rule_id !== "ACC012") continue;
    const [branchId, updateId] = String(finding.change_id).split("#");
    const branch = branches.get(branchId);
    const update = branch?.events?.find((e) => e.id === updateId);
    if (!branch || !update) {
      throw new Error(`acc finding ${finding.id} names branch update ${finding.change_id}, which its events do not hold`);
    }
    const kind = BRANCH_BYPASS[update.kind] ?? "direct_push";
    const by = personOf(update.actor_id) ?? update.actor_id ?? null;
    records.push({
      key: `update-${slug(branch.name)}-${slug(update.id)}`,
      kind: "direct_push",
      title: finding.explanation ?? `${update.kind} to ${branch.name}`,
      authored_by: by,
      author_kind: "human",
      opened_on: day(update.at),
      approvals: [],
      ...(by ? { merged_by: by } : {}),
      merged_on: day(update.at),
      ...(update.after ? { artifact_digest: update.after } : {}),
      bypass: {
        used: true,
        kind,
        ...(by ? { by } : {}),
        reason:
          `acc recorded ${update.kind.replace(/_/g, " ")} on ${branch.name} that no merged change accounts for ` +
          "(ACC012). Replace this with what actually happened before signing.",
      },
      acc: { change_id: finding.change_id, verdicts: { ACC012: "fail" } },
    });
  }
  for (const assessment of manifest.assessments) {
    if (assessment.rule_id !== "ACC012" || assessment.status !== "unknown") continue;
    const branch = branches.get(assessment.change_id);
    const name = branch?.name ?? String(assessment.change_id).split(":branch:").pop();
    const window = manifest.events.window ?? {};
    records.push({
      key: `update-${slug(name)}-unread`,
      kind: "direct_push",
      title: `The updates of ${name} could not all be read: ${assessment.reason ?? "acc did not say why"}`,
      authored_by: null,
      author_kind: "human",
      opened_on: day(window.from),
      approvals: [],
      merged_on: day(window.to),
      bypass: { used: false },
      acc: { change_id: assessment.change_id, verdicts: { ACC012: "unknown" } },
    });
  }
  return records;
}

/** acc's verdicts for the mapped rules on one change: `{ACC001: "pass", ...}`. */
function verdictsOf(changeId, assessments) {
  const out = {};
  for (const a of assessments) {
    if (a.change_id === changeId && MAPPED_RULES.includes(a.rule_id)) out[a.rule_id] = a.status;
  }
  return out;
}

/**
 * The forge-neutral export, from an acc manifest and optionally the forge export of the same
 * window. Pure: the same inputs give the same document, apart from `exported_at`.
 */
export function fromAcc(manifest, manifestBytes, forgeExport = null) {
  if (!manifest || typeof manifest !== "object" || !manifest.events || !Array.isArray(manifest.assessments)) {
    throw new Error("not an agent-change-control manifest (no events or assessments)");
  }
  if (!ACP_VERSIONS.includes(manifest.version)) {
    throw new Error(
      `acc manifest version ${manifest.version} is not one this reads (${ACP_VERSIONS.join(", ")})`,
    );
  }
  const events = manifest.events;
  const actors = events.actors ?? {};
  const forgeChanges = new Map((forgeExport?.changes ?? []).map((c) => [c.key, c]));
  const changes = [];
  const seen = new Set();
  for (const change of events.changes ?? []) {
    if (!change.merged_at) continue;
    const key = keyOf(change.id);
    if (!key) throw new Error(`acc change ${change.id} has no pull or merge request number`);
    const verdicts = verdictsOf(change.id, manifest.assessments);
    const base = forgeChanges.get(key) ?? recordFromAcc(change, actors);
    const record = { ...base, acc: { change_id: change.id, verdicts } };
    // ACC005 decides bypass when acc assessed it; otherwise the forge export's record stands.
    if (verdicts.ACC005 === "fail") {
      record.bypass = {
        used: true,
        kind: "ruleset_bypass",
        ...(change.merger ? { by: base.merged_by ?? personOf(change.merger.actor_id) } : {}),
        reason:
          "acc recorded a ruleset bypass on the merge (ACC005). Replace this with what actually " +
          "happened before signing.",
      };
    } else if (verdicts.ACC005 !== undefined) {
      record.bypass = { used: false };
    }
    changes.push(record);
    seen.add(key);
  }
  // A change the forge export has and acc does not keeps this piece's own arithmetic.
  for (const [key, change] of forgeChanges) {
    if (!seen.has(key)) changes.push(change);
  }
  changes.push(...branchRecords(manifest));
  changes.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));

  const window = events.window ?? {};
  const complete =
    window.complete === true && (events.changes ?? []).every((c) => c.reviews_complete !== false);
  const repository = events.repository;
  return {
    _comment:
      "Written by plugins/change-control/scripts/export/acc.mjs from an agent-change-control " +
      "manifest. Read by the offline collector; not committed.",
    forge:
      forgeExport?.forge ??
      (changes.some((c) => c.acc?.change_id?.startsWith("gitlab:")) ? "gitlab" : "github"),
    repository,
    exported_at: new Date().toISOString(),
    window: {
      opens_on: forgeExport?.window?.opens_on ?? day(window.from),
      closes_on: forgeExport?.window?.closes_on ?? day(window.to),
      complete: complete && (forgeExport?.window?.complete ?? true),
    },
    ...(forgeExport?.settings ? { settings: forgeExport.settings } : {}),
    source: {
      input: "agent-change-control",
      acp_version: manifest.version,
      tool_version: manifest.generated?.version ?? null,
      manifest_sha256: createHash("sha256").update(manifestBytes).digest("hex"),
      forge_export: forgeExport !== null,
    },
    changes,
  };
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
  let bytes;
  let manifest;
  try {
    bytes = readFileSync(opts.manifest);
  } catch (error) {
    process.stderr.write(`error: cannot read ${opts.manifest} (${error.code ?? error.message})\n`);
    return 2;
  }
  try {
    manifest = JSON.parse(bytes.toString("utf8"));
  } catch {
    process.stderr.write(
      `error: ${opts.manifest} is not JSON. Write the acc manifest with --format json.\n`,
    );
    return 1;
  }
  let forgeExport = null;
  if (opts.forgeExport) {
    if (!existsSync(opts.forgeExport)) {
      process.stderr.write(`error: no forge export at ${opts.forgeExport}\n`);
      return 2;
    }
    try {
      forgeExport = JSON.parse(readFileSync(opts.forgeExport, "utf8"));
    } catch (error) {
      process.stderr.write(`error: ${opts.forgeExport} is not JSON (${error.message})\n`);
      return 1;
    }
  }
  let document;
  try {
    document = fromAcc(manifest, bytes, forgeExport);
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    return 1;
  }
  let staged = null;
  if (opts.statement) {
    try {
      staged = stageStatement(opts.statement, opts.artifacts, document.window);
    } catch (error) {
      process.stderr.write(`error: ${error.message}\n`);
      return 1;
    }
    document.source.statement = staged;
  }
  mkdirSync(dirname(opts.out), { recursive: true });
  writeFileSync(opts.out, `${JSON.stringify(document, null, 2)}\n`, "utf8");
  const summary = {
    ok: true,
    out: opts.out,
    source: document.source,
    window: document.window,
    counts: {
      changes: document.changes.length,
      decided_by_acc: document.changes.filter((c) => c.acc).length,
      branch_updates: document.changes.filter((c) => c.kind === "direct_push").length,
    },
    ...(staged ? { statement: staged } : {}),
  };
  if (opts.json) process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  else if (!opts.quiet) {
    process.stdout.write(
      `wrote ${opts.out}: ${summary.counts.changes} change(s), ${summary.counts.decided_by_acc} ` +
        `decided by acc ${manifest.version} manifest sha256:${document.source.manifest_sha256.slice(0, 12)}\n`,
    );
  }
  return 0;
}

/**
 * Copy acc's in-toto Statement into the artifacts directory, under one name per window, for
 * evidence-push. Refuses anything that is not one ACP Statement: a per-change JSONL stream is not
 * one file evidence-push can upload, and a file that is not acc's should not be staged as acc's.
 */
export function stageStatement(path, artifacts, window) {
  let bytes;
  try {
    bytes = readFileSync(path);
  } catch (error) {
    throw new Error(`cannot read the Statement ${path} (${error.code ?? error.message})`);
  }
  let statement;
  try {
    statement = JSON.parse(bytes.toString("utf8"));
  } catch {
    throw new Error(
      `${path} is not one JSON document. Write the Statement with \`acc ... --format in-toto\`; ` +
        "the per-change JSONL stream is not a single file to upload",
    );
  }
  if (statement?._type !== STATEMENT_TYPE || !String(statement?.predicateType ?? "").startsWith(ACP_PREDICATE)) {
    throw new Error(`${path} is not an AI Change Provenance in-toto Statement`);
  }
  if (!window?.opens_on || !window?.closes_on) {
    throw new Error("the acc manifest has no window, so the Statement cannot be named for one");
  }
  const name = `acc-${window.opens_on}_${window.closes_on}.intoto.json`;
  const target = join(artifacts, name);
  mkdirSync(artifacts, { recursive: true });
  writeFileSync(target, bytes);
  return {
    file: target,
    sha256: createHash("sha256").update(bytes).digest("hex"),
    predicate_type: statement.predicateType,
  };
}

function invokedAsScript() {
  try {
    return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    return false;
  }
}

if (invokedAsScript()) process.exitCode = main(process.argv.slice(2));
