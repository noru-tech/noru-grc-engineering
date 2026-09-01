#!/usr/bin/env node
// Deterministic, offline collector for the change-control piece (contract requirement 2).
//
// Node built-ins only. Opens no socket. Same input in, byte-identical derived output —
// scripts/contract_test.py runs this twice and diffs the result.
//
// The input is `.noru/.cache/change-events.json`, a normalized, forge-neutral export written by a
// *credentialed* job (scripts/export/github.mjs, scripts/export/gitlab.mjs). That split is the
// whole architecture of this piece: who approved a pull request and whether branch protection was
// on are repository *settings and history*, not files, so nothing offline can read them — and
// contract requirement 2 says a collector may not open a socket. So the collector does not. It
// reads what the exporter left behind, exactly as review-signoff reads its review queue.
//
// What this file decides, and what it refuses to: it computes which separations did not hold, by
// comparing names. That is arithmetic on the export, not a judgement, which is what keeps it
// deterministic. Whether a violation is acceptable is a judgement, so every one it finds is
// written into the skeleton with `needs_review: true` and no disposition, and a manifest carrying
// one cannot be pushed.
//
// Usage: node collect.mjs [--repo=<path>] [--check] [--output=json|text] [--quiet]
// Exit codes: 0 ok, 1 drift against the manifest (--check), 2 usage/IO error.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { basename, join, relative, sep } from "node:path";
import { pathToFileURL } from "node:url";

export const PIECE = "change-control";
export const VERSION = "0.4.0";
const GENERATED_BY = `${PIECE}@${VERSION}`;

const EVENTS_REL = ".noru/.cache/change-events.json";
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
// Segregation of duties, as arithmetic on names.
//
// Each rule below answers one question — did the same person hold two duties that are supposed to
// be held apart? — and nothing else. It does not know which framework asks for which separation,
// and it must not: that is Noru's queue to answer (requirement 9).

export function normalizePerson(value) {
  return typeof value === "string" ? value.trim().toLowerCase() : null;
}

function approversOf(change) {
  const out = [];
  for (const approval of change.approvals ?? []) {
    if (approval?.state === "approved") {
      const who = normalizePerson(approval.by);
      if (who) out.push(who);
    }
  }
  return out;
}

/**
 * Every separation that did not hold for one change, in a fixed order so the derived facts never
 * depend on iteration order.
 *
 * Returns [{ rule, detail }]. The collector proposes these; a person dispositions them; the
 * validator refuses a manifest where one is present and nobody owns it.
 */
export function violationsOf(change) {
  const out = [];
  const author = normalizePerson(change.authored_by);
  const operator = normalizePerson(change.agent_operator);
  const approvers = approversOf(change);
  const independent = approvers.filter((who) => who !== author);

  if (author && approvers.includes(author)) {
    out.push({
      rule: "approver_is_author",
      detail: `${change.authored_by} approved their own change`,
    });
  }

  if (independent.length === 0) {
    const merged = normalizePerson(change.merged_by);
    out.push({
      rule: "merged_without_independent_approval",
      detail:
        approvers.length === 0
          ? "no approval from anybody"
          : `the only approval came from ${change.authored_by}, who wrote it`,
      ...(merged ? {} : {}),
    });
  }

  const deployer = normalizePerson(change.deployed_by);
  if (deployer && author && deployer === author) {
    out.push({
      rule: "deployer_is_author",
      detail: `${change.authored_by} wrote the change and also put it in production`,
    });
  }

  // An agent-authored change needs a human who is neither the agent nor the person who ran it.
  // Whoever pressed go is not an independent reviewer of what came back, and this is the one rule
  // here that a conventional change-management control does not already cover.
  if (change.author_kind === "agent") {
    const humanReviewers = independent.filter((who) => who !== operator);
    if (humanReviewers.length === 0) {
      out.push({
        rule: "agent_change_without_independent_human",
        detail: operator
          ? `written by an agent and reviewed by nobody other than ${change.agent_operator}, who ran it`
          : "written by an agent with no independent human approval",
      });
    }
  }

  if (change.bypass?.used === true) {
    out.push({
      rule: "bypass_used",
      detail: change.bypass.kind
        ? `${change.bypass.kind} was used to get this change in`
        : "a control was stepped around",
    });
  }

  return out;
}

// --------------------------------------------------------------------------------------------- //

function lineOfKey(text, key) {
  // A real citation into the export, so a finding points at a record and not just a filename.
  const needle = `"key": ${JSON.stringify(key)}`;
  const at = text.indexOf(needle);
  if (at < 0) return 1;
  return text.slice(0, at).split("\n").length;
}

export function collectFacts(repo) {
  const eventsPath = join(repo, EVENTS_REL);
  if (!existsSync(eventsPath)) return null;

  let text;
  let events;
  try {
    text = readFileSync(eventsPath, "utf8");
    events = JSON.parse(text);
  } catch (error) {
    throw new Error(`${EVENTS_REL} could not be read as JSON (${error.message})`);
  }

  const changes = (events.changes ?? []).map((change) => {
    const ref = `${EVENTS_REL}:${lineOfKey(text, change.key)}`;
    return {
      key: change.key,
      kind: change.kind,
      title: change.title,
      authored_by: change.authored_by,
      author_kind: change.author_kind ?? "human",
      ...(change.agent_operator ? { agent_operator: change.agent_operator } : {}),
      opened_on: change.opened_on,
      approvals: (change.approvals ?? []).map((a) => ({
        by: a.by,
        state: a.state,
        ...(a.reviewed_on ? { reviewed_on: a.reviewed_on } : {}),
      })),
      ...(change.merged_by ? { merged_by: change.merged_by } : {}),
      ...(change.merged_on ? { merged_on: change.merged_on } : {}),
      ...(change.deployed_by ? { deployed_by: change.deployed_by } : {}),
      ...(change.deployed_on ? { deployed_on: change.deployed_on } : {}),
      ...(change.artifact_digest ? { artifact_digest: change.artifact_digest } : {}),
      ...(change.bypass ? { bypass: change.bypass } : {}),
      violations: violationsOf(change),
      ref,
    };
  });
  // Sorted by key so the derived facts never depend on the exporter's page order.
  changes.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));

  const violationCount = changes.reduce((n, c) => n + c.violations.length, 0);
  return {
    piece: PIECE,
    generated_by: GENERATED_BY,
    forge: events.forge ?? "other",
    window: events.window ?? null,
    settings: events.settings ?? null,
    settings_ref: `${EVENTS_REL}:1`,
    changes,
    counts: {
      changes: changes.length,
      with_violations: changes.filter((c) => c.violations.length > 0).length,
      violations: violationCount,
      agent_authored: changes.filter((c) => c.author_kind === "agent").length,
    },
  };
}

export function digestOf(derived) {
  // `generated_by` is excluded for the reason established in privacy-datamap: a plugin upgrade is
  // not a change to the change history, and hashing it would report drift on every manifest the
  // day the version bumps.
  const { generated_by, ...facts } = derived;
  void generated_by;
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
  const window = derived.window ?? { opens_on: "TODO", closes_on: "TODO" };
  const settings = derived.settings ?? {};
  const skeleton = {
    version: VERSION,
    piece: PIECE,
    source: {
      ...provenance,
      forge: derived.forge,
      derived_digest: digestOf(derived),
    },
    window: {
      opens_on: window.opens_on,
      closes_on: window.closes_on,
      ...(typeof window.complete === "boolean" ? { complete: window.complete } : {}),
    },
  };

  // Requirement 9: the control ids come from Noru, never from here. The skeleton leaves the shape
  // for the skill to fill from getOrganizationControls / getControlContext, and the validator
  // refuses a mapping to any id the snapshot did not offer.
  skeleton.queue_snapshot = {
    fetched_at: "TODO_YYYY-MM-DDTHH:MM:SSZ",
    via: ["getOrganizationControls", "getControlContext", "getEvidenceForControl"],
    controls: [],
  };
  skeleton.control_mappings = [];

  if (derived.settings) {
    skeleton.controls = {
      default_branch: settings.default_branch ?? "unknown",
      observed_on: window.closes_on,
      ...pick(settings, [
        "protected",
        "required_approvals",
        "dismiss_stale_reviews",
        "require_code_owner_review",
        "enforce_admins",
        "allow_force_push",
        "required_status_checks",
        "codeowners_present",
        "deploy_environments",
      ]),
      refs: [derived.settings_ref],
      needs_review: true,
      interpretation: {
        owner: "TODO_a.person@example.com",
        decided_at: window.closes_on,
        expires_at: "TODO_YYYY-MM-DD",
        rationale: "TODO: say who observed this configuration and how.",
      },
    };
  }

  skeleton.changes = derived.changes.map((change) => ({
    key: change.key,
    kind: change.kind,
    title: change.title,
    authored_by: change.authored_by,
    author_kind: change.author_kind,
    ...(change.agent_operator ? { agent_operator: change.agent_operator } : {}),
    opened_on: change.opened_on,
    ...(change.approvals.length > 0 ? { approvals: change.approvals } : {}),
    ...pick(change, [
      "merged_by",
      "merged_on",
      "deployed_by",
      "deployed_on",
      "artifact_digest",
      "bypass",
    ]),
    ...(change.violations.length > 0
      ? {
          exceptions: change.violations.map((violation) => ({
            rule: violation.rule,
            disposition: "TODO_remediated|accepted_risk|false_positive|deferred",
            owner: "TODO_a.person@example.com",
            note: `TODO: ${violation.detail}`,
          })),
          needs_review: true,
        }
      : {}),
    refs: [change.ref],
    interpretation: {
      owner: "TODO_a.person@example.com",
      decided_at: window.closes_on,
      expires_at: "TODO_YYYY-MM-DD",
      rationale: "TODO: what this record accounts for, in a sentence a reviewer can argue with.",
    },
  }));

  return skeleton;
}

function pick(source, keys) {
  const out = {};
  for (const key of keys) {
    if (source?.[key] !== undefined && source?.[key] !== null) out[key] = source[key];
  }
  return out;
}

const HEADER = `# .noru/change-control.yml — generated by ${GENERATED_BY}
#
# This file records what happened: who wrote each change, who approved it, who merged it, who
# deployed it. It does not assert that what happened was correct, and the validator will not ask
# you to pretend it was.
#
# What it does ask is that every separation which did not hold has somebody's name against it.
# Each \`exceptions:\` entry below was computed by comparing names — the collector proposes, you
# decide — and every one carries needs_review: true until you replace the TODOs.
#
# A manifest with any needs_review: true cannot be pushed.
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

  let derived;
  try {
    derived = collectFacts(opts.repo);
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    return 2;
  }
  if (derived === null) {
    // The ordinary state on a fork pull request, and on any checkout where the credentialed
    // exporter has not run. Not drift, and not a broken build — CI mode reports it as skipped.
    process.stderr.write(
      `error: no ${EVENTS_REL}. Run the exporter for your forge first:\n` +
        `  node plugins/${PIECE}/scripts/export/github.mjs --repo=<owner/name> --since=<YYYY-MM-DD>\n` +
        "It needs a token and this collector deliberately does not.\n",
    );
    return 1;
  }

  const provenance = repoProvenance(opts.repo);
  const digest = digestOf(derived);
  const manifestPath = join(opts.repo, ".noru", "change-control.yml");
  const derivedPath = join(opts.repo, ".noru", ".cache", "change-control.derived.json");

  let wroteSkeleton = false;
  let drift = false;
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
    counts: derived.counts,
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    process.stdout.write(
      [
        `${derived.counts.changes} change(s) in ${opts.repo}`,
        `separations that did not hold: ${derived.counts.violations} ` +
          `across ${derived.counts.with_violations} change(s)`,
        `agent-authored: ${derived.counts.agent_authored}`,
        `derived facts: ${summary.derived_facts}`,
        wroteSkeleton ? `wrote skeleton: ${summary.manifest}` : "",
        drift ? "DRIFT: the manifest does not match the export as it is now" : "",
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
