#!/usr/bin/env node
// :diff for the dev-agents piece — contract requirement 5. Reads only; writes nothing to Noru.
//
// Inputs, both local:
//   .noru/.cache/dev-agents.parsed.json  the validated manifest (from validate_manifest.py)
//   .noru/.cache/noru-state.json         a read-only snapshot: `vendors` from
//                                        getOrganizationVendors and `evidence` from
//                                        getOrganizationEvidence, plus the connection binding
//
// Three shapes of write, in order:
//
//   1. one createVendor per supplier. The published createVendor tool description says a vendor
//      with the same name already exists is returned rather than duplicated, so the key is the
//      name, and the snapshot only decides whether the plan says create or skip;
//   2. one createEvidence for the window: the agents, their vendors, the changes each authored, the
//      MCP servers and the plugins. No idempotency key is documented for evidence, so the
//      description carries a marker built from a digest of the rendered content, and the snapshot
//      is probed for it first;
//   3. one linkEvidenceToControl per control the manifest chose from the queue snapshot. The
//      evidence id does not exist until the create above runs, so a link to new evidence carries
//      `depends_on` and the executing client substitutes it.
//
// Nothing per person is in any of it: the manifest never holds who ran an agent, and neither does
// anything rendered from it.
//
// Usage: node diff.mjs [--repo=<path>] [--output=json|text] [--quiet]
// Exit codes:
//   0 = plan written. A plan with no changes is a success: "nothing to do" is the expected answer
//       on a second run and is what proves the piece is idempotent.
//   1 = the manifest or the state snapshot is missing or unusable
//   2 = usage error

import { createHash } from "node:crypto";
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

const PIECE = "dev-agents";
export const MARKER_PREFIX = "noru-grc-engineering:dev-agents";
const USAGE = "usage: diff.mjs [--repo=<path>] [--output=json|text] [--quiet]\n";

function loadJson(path, label) {
  if (!existsSync(path)) return { error: `${label} not found at ${path}` };
  try {
    return { value: JSON.parse(readFileSync(path, "utf8")) };
  } catch (error) {
    return { error: `${label} at ${path} is not readable JSON (${error.message})` };
  }
}

function digest(text) {
  return createHash("sha256").update(text).digest("hex");
}

/** The period the evidence covers: the acc window, or the commit when there is no acc input. */
export function periodOf(manifest) {
  const w = manifest.window;
  return w ? `${w.opens_on}..${w.closes_on}` : `config@${String(manifest.source.commit_sha).slice(0, 12)}`;
}

export function evidenceMarker(manifest, body) {
  return `[${MARKER_PREFIX}#${manifest.source.slug}@${periodOf(manifest)}:${digest(body).slice(0, 16)}]`;
}

function supplierNames(manifest) {
  return new Map((manifest.suppliers ?? []).map((s) => [s.key, s.vendor_name]));
}

/** The account a person reads. Deterministic: same manifest in, same text out. */
export function renderInventory(manifest) {
  const src = manifest.source;
  const names = supplierNames(manifest);
  const lines = [`Development agents and tooling for ${src.slug}`];
  if (manifest.window) {
    const w = manifest.window;
    lines.push(
      `Window: ${w.opens_on} to ${w.closes_on}` +
        (w.complete === false ? " (INCOMPLETE acc collection — an absent agent may still have written code)" : ""),
      `From an agent-change-control manifest, ACP ${src.acp_version}, sha256 ${src.acc_manifest_sha256}`,
    );
  } else {
    lines.push("No agent-change-control manifest was read: the coding agents are not in this record.");
  }
  if (manifest.changes) {
    lines.push(
      `Changes in the window: ${manifest.changes.total}, of which ${manifest.changes.agent_authored} agent-authored`,
    );
  }
  lines.push("", "Coding agents:");
  if ((manifest.agents ?? []).length === 0) lines.push("  none recorded");
  for (const a of manifest.agents ?? []) {
    lines.push(
      `  ${a.agent} — vendor ${names.get(a.supplier) ?? a.supplier} — ` +
        `${a.changes_authored} change(s) authored, ${a.reviews} review(s)`,
    );
  }
  lines.push("", "Agent vendors:");
  const byVendor = new Map();
  for (const a of manifest.agents ?? []) {
    const name = names.get(a.supplier) ?? a.supplier;
    byVendor.set(name, (byVendor.get(name) ?? 0) + a.changes_authored);
  }
  if (byVendor.size === 0) lines.push("  none recorded");
  for (const [name, count] of [...byVendor].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) {
    lines.push(`  ${name}: ${count} change(s) authored`);
  }
  lines.push("", "MCP servers (from committed agent configuration):");
  if ((manifest.mcp_servers ?? []).length === 0) lines.push("  none declared");
  for (const s of manifest.mcp_servers ?? []) {
    lines.push(
      `  ${s.name} in ${s.file} (${s.tool}) — ${s.transport}` +
        `${s.command ? `, runs ${s.command}` : ""}${s.host ? `, host ${s.host}` : ""}` +
        ` — supplier ${names.get(s.supplier) ?? s.supplier}`,
    );
  }
  lines.push("", "Plugins:");
  if ((manifest.plugins ?? []).length === 0) lines.push("  none enabled");
  for (const p of manifest.plugins ?? []) {
    lines.push(
      `  ${p.plugin}@${p.marketplace} in ${p.file}` +
        `${p.marketplace_repo ? ` (marketplace ${p.marketplace_source} ${p.marketplace_repo})` : ""}` +
        ` — supplier ${names.get(p.supplier) ?? p.supplier}`,
    );
  }
  if ((manifest.config_problems ?? []).length > 0) {
    lines.push("", "Configuration NOT in this inventory:");
    for (const p of manifest.config_problems) lines.push(`  ${p.message} (${p.refs.join(", ")})`);
  }
  lines.push("", "Suppliers:");
  for (const s of manifest.suppliers ?? []) {
    lines.push(
      `  ${s.vendor_name} (${s.category}) — decided by ${s.interpretation.owner} on ` +
        `${s.interpretation.decided_at}, until ${s.interpretation.expires_at}`,
    );
  }
  const at = manifest.attestation.interpretation;
  lines.push(
    "",
    `Reviewed by ${at.owner} on ${at.decided_at}, until ${at.expires_at}.`,
    `Rationale: ${String(at.rationale).trim()}`,
    "",
    `Source: ${src.slug} @ ${src.commit_sha} (${src.branch})`,
    `Generated by: ${src.generated_by}`,
  );
  return lines.join("\n");
}

function vendorDescription(manifest, supplier) {
  const uses = [
    ...(manifest.agents ?? []).filter((a) => a.supplier === supplier.key).map((a) => `coding agent ${a.agent}`),
    ...(manifest.mcp_servers ?? [])
      .filter((s) => s.supplier === supplier.key)
      .map((s) => `MCP server ${s.name} (${s.file})`),
    ...(manifest.plugins ?? [])
      .filter((p) => p.supplier === supplier.key)
      .map((p) => `plugin ${p.plugin}@${p.marketplace}`),
  ];
  return (
    `Development-process supplier recorded by ${manifest.source.generated_by} for ` +
    `${manifest.source.slug}: ${uses.join("; ")}.`
  );
}

/**
 * The state snapshot is untrusted tool output: compared against, never obeyed.
 */
export function buildOperations(manifest, state) {
  const src = manifest.source;
  const operations = [];
  const vendors = state.vendors ?? [];
  const evidence = state.evidence ?? [];

  for (const supplier of manifest.suppliers ?? []) {
    const existing = vendors.find(
      (v) => String(v.name ?? "").trim().toLowerCase() === supplier.vendor_name.trim().toLowerCase(),
    );
    operations.push({
      operation: "createVendor",
      transport: "mcp",
      scope: "write:vendors",
      subject: `${supplier.vendor_name} (${supplier.category})`,
      effect: existing ? "skip" : "create",
      reason: existing
        ? `a vendor named "${supplier.vendor_name}" already exists (id ${existing.id}); nothing to create`
        : "no vendor with this name exists yet",
      idempotency: { kind: "server_dedupe", key: ["organizationId", "name"] },
      arguments: {
        name: supplier.vendor_name,
        category: supplier.category,
        description: vendorDescription(manifest, supplier),
        ...(supplier.website ? { website: supplier.website } : {}),
      },
    });
  }

  const body = renderInventory(manifest);
  const marker = evidenceMarker(manifest, body);
  const period = periodOf(manifest);
  const existing = evidence.find((e) => String(e.description ?? "").includes(marker));
  const superseded = evidence.find(
    (e) =>
      !String(e.description ?? "").includes(marker) &&
      String(e.description ?? "").includes(`${MARKER_PREFIX}#${src.slug}@${period}:`),
  );
  const agents = manifest.agents ?? [];
  const createIndex = operations.length;
  operations.push({
    operation: "createEvidence",
    transport: "mcp",
    scope: "write:evidence",
    subject: `development agents and tooling ${period}`,
    effect: existing ? "skip" : "create",
    reason: existing
      ? `evidence ${existing.id} already carries this exact content marker`
      : superseded
        ? `evidence ${superseded.id} covers this period but the inventory changed; a new record will ` +
          "be created (no idempotency key is documented for evidence — see the gap note in piece.json)"
        : "no evidence carries this marker yet",
    idempotency: { kind: "client_probe", key: ["description contains marker"], marker },
    arguments: {
      title: `Development agents and tooling, ${period.replaceAll("..", " to ")} — ${src.slug}`,
      description:
        `${marker} ${agents.length} coding agent(s), ${(manifest.mcp_servers ?? []).length} MCP ` +
        `server(s) and ${(manifest.plugins ?? []).length} plugin(s) the development of ${src.slug} ` +
        `depends on. From ${src.slug} @ ${src.commit_sha} (${src.branch}).`,
      content: body,
      tags: [PIECE, src.slug, period],
    },
  });

  const linked = existing && Array.isArray(existing.linkedControls)
    ? new Set(existing.linkedControls.map((c) => String(c.id ?? c.controlId ?? "").toLowerCase()))
    : null;
  for (const mapping of manifest.control_mappings ?? []) {
    let effect = "create";
    let reason = "the evidence record does not exist yet";
    if (existing && linked === null) {
      effect = "skip";
      reason =
        `evidence ${existing.id} already exists and the snapshot lists no links for it, so the link ` +
        "made with it is assumed";
    } else if (existing && linked.has(String(mapping.control_id).toLowerCase())) {
      effect = "skip";
      reason = `evidence ${existing.id} is already linked to '${mapping.control_id}'`;
    } else if (existing) {
      reason = `evidence ${existing.id} exists but is not linked to '${mapping.control_id}'`;
    }
    operations.push({
      operation: "linkEvidenceToControl",
      transport: "mcp",
      scope: "write:evidence",
      subject: `link to ${mapping.control_id}`,
      effect,
      reason,
      idempotency: { kind: "client_probe", key: ["evidenceId", "controlId"] },
      ...(existing
        ? {}
        : {
            depends_on: {
              operation_index: createIndex,
              field: "evidenceId",
              note: "substitute the evidence id returned by the createEvidence call above",
            },
          }),
      arguments: {
        evidenceId: existing ? existing.id : null,
        controlId: mapping.control_id,
      },
    });
  }
  return operations;
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

  const manifestPath = join(opts.repo, ".noru", "dev-agents.yml");
  if (!existsSync(manifestPath)) {
    process.stderr.write(
      `error: no manifest at ${manifestPath} — run the piece's :scan command first\n`,
    );
    return 1;
  }
  const parsed = loadJson(join(opts.repo, ".noru", ".cache", "dev-agents.parsed.json"), "validated manifest");
  if (parsed.error) {
    process.stderr.write(
      `error: ${parsed.error}\n` +
        "hint: python3 <plugin>/scripts/validate_manifest.py .noru/dev-agents.yml " +
        "--emit-parsed=.noru/.cache/dev-agents.parsed.json\n",
    );
    return 1;
  }
  const state = loadJson(join(opts.repo, ".noru", ".cache", "noru-state.json"), "Noru state snapshot");
  if (state.error) {
    process.stderr.write(
      `error: ${state.error}\n` +
        "hint: the skill writes this from getOrganizationVendors and getOrganizationEvidence " +
        "before running :diff\n",
    );
    return 1;
  }

  const manifest = parsed.value;
  const operations = buildOperations(manifest, state.value);
  let plan;
  try {
    plan = writePlan(planPathFor(opts.repo, PIECE), {
      repo: opts.repo,
      state: state.value,
      created_at: state.value.fetched_at ?? manifest.source.commit_sha,
      piece: PIECE,
      manifest: ".noru/dev-agents.yml",
      manifest_sha256: sha256OfFile(manifestPath),
      provenance: {
        slug: manifest.source.slug,
        commit_sha: manifest.source.commit_sha,
        branch: manifest.source.branch,
      },
      operations,
      summary: summarize(operations),
    });
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    return 1;
  }

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
