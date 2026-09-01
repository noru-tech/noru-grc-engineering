#!/usr/bin/env node
// :diff for the change-control piece — contract requirement 5. Reads only; writes nothing to Noru.
//
// Inputs, both local:
//   .noru/.cache/change-control.parsed.json  the validated manifest (from validate_manifest.py)
//   .noru/.cache/noru-state.json        a read-only snapshot written by the skill from MCP output
//
// Usage: node diff.mjs [--repo=<path>] [--output=json|text] [--quiet]
// Exit codes:
//   0 = plan written. A plan with no changes is a success: "nothing to do" is the expected answer
//       on a second run and is what proves the piece is idempotent.
//   1 = the manifest or the state snapshot is missing or unusable
//   2 = usage error

import { existsSync, readFileSync, realpathSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import {
  parseCommonArgs,
  planPathFor,
  renderPlanText,
  sha256OfFile,
  summarize,
  writePlan,
} from "./lib/plan.mjs";

const PIECE = "change-control";
const MARKER_PREFIX = "noru-grc-engineering:change-control";
const USAGE = "usage: diff.mjs [--repo=<path>] [--output=json|text] [--quiet]\n";

function loadJson(path, label) {
  if (!existsSync(path)) return { error: `${label} not found at ${path}` };
  try {
    return { value: JSON.parse(readFileSync(path, "utf8")) };
  } catch (error) {
    return { error: `${label} at ${path} is not readable JSON (${error.message})` };
  }
}

/**
 * The state snapshot is untrusted tool output: compared against, never obeyed.
 *
 * Three shapes of write, in the order a reader should think about them:
 *
 *   1. Each owned exception becomes one security finding, keyed on `(source, externalId)`, which
 *      is a documented server-side upsert. That is what lets the same call file a finding and
 *      close one: an exception dispositioned `remediated` or `false_positive` is pushed with
 *      status `resolved`, so re-running the piece after the fix lands closes the record rather
 *      than leaving a stale open finding beside a fixed problem.
 *   2. The window itself becomes one evidence record. Evidence has no documented idempotency key,
 *      so this half probes for a marker first.
 *   3. Each control mapping links that evidence to a control the queue snapshot offered. The
 *      evidence id does not exist until the create above runs, so the call carries `depends_on`
 *      and the executing client substitutes it.
 */

const SOURCE = "noru-grc-engineering/change-control";

export function externalIdFor(slug, window, changeKey, rule) {
  return `${slug}:${window.opens_on}..${window.closes_on}:${changeKey}:${rule}`;
}

// A remediated or false-positive exception is not an open problem, and pushing it as one would
// leave the register describing a state of the world that stopped being true.
const RESOLVED_DISPOSITIONS = new Set(["remediated", "false_positive"]);

const SEVERITY_BY_RULE = {
  approver_is_author: "medium",
  merged_without_independent_approval: "high",
  deployer_is_author: "medium",
  agent_change_without_independent_human: "high",
  bypass_used: "high",
};

export function buildOperations(manifest, state) {
  const src = manifest.source;
  const window = manifest.window;
  const operations = [];
  const existingFindings = new Map(
    (state.security_findings ?? []).map((row) => [row.externalId, row]),
  );

  for (const change of manifest.changes ?? []) {
    for (const exception of change.exceptions ?? []) {
      const externalId = externalIdFor(src.slug, window, change.key, exception.rule);
      const status = RESOLVED_DISPOSITIONS.has(exception.disposition) ? "resolved" : "open";
      const existing = existingFindings.get(externalId);
      const unchanged = existing && existing.status === status;
      operations.push({
        operation: "createSecurityFinding",
        transport: "mcp",
        scope: "write:risks",
        subject: `${change.key}: ${exception.rule}`,
        effect: unchanged ? "skip" : existing ? "update" : "create",
        reason: unchanged
          ? `finding ${externalId} is already ${status}`
          : existing
            ? `finding ${externalId} is ${existing.status}, and this manifest says ${status}`
            : "no finding carries this external id yet",
        idempotency: { kind: "server_upsert", key: ["source", "externalId"], externalId },
        arguments: {
          source: SOURCE,
          externalId,
          title: `${exception.rule.replace(/_/g, " ")} on ${change.key}`,
          severity: SEVERITY_BY_RULE[exception.rule] ?? "medium",
          status,
          description: [
            `${change.kind} ${change.key}: ${change.title}`,
            `Authored by ${change.authored_by} (${change.author_kind})`,
            change.agent_operator ? `Agent run by ${change.agent_operator}` : null,
            `Disposition: ${exception.disposition}, owned by ${exception.owner}`,
            exception.resolved_on ? `Resolved on ${exception.resolved_on}` : null,
            "",
            exception.note,
            "",
            `From ${src.slug} @ ${src.commit_sha} (${src.branch}), window ` +
              `${window.opens_on}..${window.closes_on}.`,
            ...(change.refs ?? []).map((ref) => `  - ${ref}`),
          ]
            .filter((line) => line !== null)
            .join("\n"),
        },
      });
    }
  }

  // One evidence record for the window, not one per change: forty records nobody can read is the
  // antipattern, and the thing an auditor asks for is the account of the period.
  const marker = `[${MARKER_PREFIX}#${window.opens_on}..${window.closes_on}]`;
  const existingEvidence = (state.evidence ?? []).find((e) =>
    String(e.description ?? "").includes(marker),
  );
  const changes = manifest.changes ?? [];
  const withExceptions = changes.filter((c) => (c.exceptions ?? []).length > 0);
  const createIndex = operations.length;
  operations.push({
    operation: "createEvidence",
    transport: "mcp",
    scope: "write:evidence",
    subject: `change control ${window.opens_on}..${window.closes_on}`,
    effect: existingEvidence ? "skip" : "create",
    reason: existingEvidence
      ? `evidence ${existingEvidence.id} already carries this marker`
      : "no evidence carries this marker yet",
    idempotency: { kind: "client_probe", key: ["description contains marker"], marker },
    arguments: {
      title: `Change control, ${window.opens_on} to ${window.closes_on} — ${src.slug}`,
      description:
        `${marker} ${changes.length} change(s) merged into ` +
        `${manifest.controls?.default_branch ?? src.branch}, of which ${withExceptions.length} ` +
        `carried a separation that did not hold. From ${src.slug} @ ${src.commit_sha}.`,
      content: renderAttestation(manifest),
      tags: [PIECE, src.slug, `${window.opens_on}..${window.closes_on}`],
    },
  });

  for (const mapping of manifest.control_mappings ?? []) {
    operations.push({
      operation: "linkEvidenceToControl",
      transport: "mcp",
      scope: "write:evidence",
      subject: `link to ${mapping.control_id}`,
      effect: existingEvidence ? "skip" : "create",
      reason: existingEvidence
        ? `evidence ${existingEvidence.id} already exists and is assumed linked`
        : "the evidence record does not exist yet",
      idempotency: { kind: "client_probe", key: ["evidenceId", "controlId"] },
      ...(existingEvidence
        ? {}
        : {
            depends_on: {
              operation_index: createIndex,
              field: "evidenceId",
              note: "substitute the evidence id returned by the createEvidence call above",
            },
          }),
      arguments: {
        evidenceId: existingEvidence ? existingEvidence.id : null,
        controlId: mapping.control_id,
        ...(mapping.evidence_item_ids ? { evidenceItemIds: mapping.evidence_item_ids } : {}),
      },
    });
  }

  return operations;
}

/** The account a person reads. Deterministic: same manifest in, same text out. */
export function renderAttestation(manifest) {
  const window = manifest.window;
  const lines = [
    `Change control for ${manifest.source.slug}`,
    `Window: ${window.opens_on} to ${window.closes_on}` +
      (window.complete === false ? " (PARTIAL export — absence is not evidence of absence)" : ""),
    "",
  ];
  if (manifest.controls) {
    const c = manifest.controls;
    lines.push(
      `Branch protection on ${c.default_branch}, observed ${c.observed_on}:`,
      `  protected: ${c.protected ?? "unknown"}`,
      `  required approvals: ${c.required_approvals ?? "unknown"}`,
      `  administrators bound by it: ${c.enforce_admins ?? "unknown"}`,
      "",
    );
  }
  for (const change of manifest.changes ?? []) {
    const exceptions = change.exceptions ?? [];
    lines.push(
      `${change.key} (${change.kind}) — ${change.title}`,
      `  authored by ${change.authored_by} (${change.author_kind})` +
        (change.agent_operator ? `, run by ${change.agent_operator}` : ""),
      `  approved by ${
        (change.approvals ?? [])
          .filter((a) => a.state === "approved")
          .map((a) => a.by)
          .join(", ") || "nobody"
      }`,
      `  merged by ${change.merged_by ?? "unrecorded"}, deployed by ${
        change.deployed_by ?? "unrecorded"
      }`,
    );
    for (const exception of exceptions) {
      lines.push(
        `  ! ${exception.rule}: ${exception.disposition}, owned by ${exception.owner}` +
          (exception.resolved_on ? ` (resolved ${exception.resolved_on})` : ""),
      );
    }
    lines.push("");
  }
  return lines.join("\n");
}

function main(argv) {
  const opts = parseCommonArgs(argv);
  if (opts.help) {
    process.stdout.write(USAGE);
    return 0;
  }
  if (opts.rest.length > 0) {
    process.stderr.write(`error: unexpected argument '${opts.rest[0]}'\n${USAGE}`);
    return 2;
  }

  const manifestPath = join(opts.repo, ".noru", "change-control.yml");
  if (!existsSync(manifestPath)) {
    process.stderr.write(
      `error: no manifest at ${manifestPath} — run the piece's :scan command first\n`
    );
    return 1;
  }
  const parsed = loadJson(
    join(opts.repo, ".noru", ".cache", "change-control.parsed.json"),
    "validated manifest"
  );
  if (parsed.error) {
    process.stderr.write(
      `error: ${parsed.error}\n` +
        "hint: python3 <plugin>/scripts/validate_manifest.py .noru/change-control.yml " +
        "--emit-parsed=.noru/.cache/change-control.parsed.json\n"
    );
    return 1;
  }
  const state = loadJson(
    join(opts.repo, ".noru", ".cache", "noru-state.json"),
    "Noru state snapshot"
  );
  if (state.error) {
    process.stderr.write(
      `error: ${state.error}\n` +
        "hint: the skill writes this from the piece's read tools before running :diff\n"
    );
    return 1;
  }

  const manifest = parsed.value;
  const operations = buildOperations(manifest, state.value);
  const plan = writePlan(planPathFor(opts.repo, PIECE), {
    created_at: state.value.fetched_at ?? manifest.source.commit_sha,
    piece: PIECE,
    manifest: ".noru/change-control.yml",
    manifest_sha256: sha256OfFile(manifestPath),
    provenance: {
      slug: manifest.source.slug,
      commit_sha: manifest.source.commit_sha,
      branch: manifest.source.branch,
    },
    operations,
    summary: summarize(operations),
  });

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(plan, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    process.stdout.write(`${renderPlanText(plan)}\n`);
  }
  return 0;
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
