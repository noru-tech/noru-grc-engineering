#!/usr/bin/env node
// :diff for the ai-inventory piece — contract requirement 5.
//
// Reads two local files and writes nothing to Noru:
//   .noru/.cache/ai-inventory.parsed.json   the validated manifest (written by validate_manifest.py)
//   .noru/.cache/noru-state.json            a read-only snapshot of the org, written by the skill
//                                           from getOrganizationAssets / getOrganizationVendors /
//                                           getOrganizationEvidence / getOrganizationControls
//
// It produces the ordered, idempotency-resolved plan that :push later requires. Every operation is
// marked create, update or skip so a reviewer sees the write before it happens.
//
// Usage: node diff.mjs [--repo=<path>] [--output=json|text] [--quiet]
// Exit codes:
//   0 = plan written. A plan with no changes is a success, not a failure: "nothing to do" is the
//       expected answer on a second run and is what proves the piece is idempotent.
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

const PIECE = "ai-inventory";
// The `source` value that makes the asset upsert key stable. Noru's published API documentation
// (https://api.noru.tech/llms.txt, "Idempotency/Upsert Behavior") says an existing
// (source, externalId) is updated in place rather than duplicated.
const ASSET_SOURCE = "noru-ai-inventory";
// Marker embedded in evidence descriptions. No idempotency key is documented for evidence, so the
// piece gives itself one to recognise: a client probe matches this via getOrganizationEvidence's
// `search` filter.
const EVIDENCE_MARKER_PREFIX = "noru-grc-engineering:ai-inventory";

const USAGE = "usage: diff.mjs [--repo=<path>] [--output=json|text] [--quiet]\n";

export function evidenceMarker(systemKey, contentDigest) {
  return `[${EVIDENCE_MARKER_PREFIX}#${systemKey}@${contentDigest.slice(0, 16)}]`;
}

/** The four finding categories, in the order the schema declares and a reader should work them. */
const FINDING_CATEGORIES = [
  "prohibited_practices",
  "transparency_obligations",
  "role_and_risk",
  "standards_alignment",
];

function findingsFor(manifest, systemKey) {
  const findings = manifest.findings ?? {};
  const out = {};
  for (const category of FINDING_CATEGORIES) {
    out[category] = (findings[category] ?? []).filter((f) => f.system === systemKey);
  }
  return out;
}

/**
 * Findings as a person reads them in Noru, ordered by what is enforceable rather than by what is
 * interesting. Article 5 leads and is set apart: it is the one category whose answer is to stop, and
 * a reader must not have to find it among the risk-tier rows.
 */
export function renderFindings(byCategory) {
  const lines = [];

  const prohibited = byCategory.prohibited_practices ?? [];
  const raised = prohibited.filter((f) => f.determination !== "no_indication");
  const screened = prohibited.filter((f) => f.determination === "no_indication");
  if (raised.length > 0) {
    lines.push("!! ARTICLE 5 — PROHIBITED PRACTICE RAISED. Read this before anything below.");
    for (const f of raised) {
      lines.push(`   ${f.article} ${f.practice}: ${f.determination}`);
      if (f.action) lines.push(`   Action: ${f.action.trim()}`);
      lines.push(`   Cited: ${(f.refs ?? []).join(", ")}`);
    }
    lines.push("");
  }
  if (screened.length > 0) {
    // "The screen ran and found nothing" is a different statement from silence, and it is the one
    // an auditor asks for. It is recorded, but it never leads.
    lines.push(
      `Article 5 screened, no indication found: ${screened.map((f) => f.practice).join(", ")}`,
      ""
    );
  }

  const transparency = byCategory.transparency_obligations ?? [];
  if (transparency.length > 0) {
    lines.push("Article 50 transparency (applicable since 2 August 2026):");
    for (const f of transparency) {
      const disclosure = f.disclosure ?? {};
      lines.push(
        `  ${f.article} ${f.trigger} — requires ${f.required_action}` +
          `${f.applies_to_role ? ` (${f.applies_to_role})` : ""}`,
        `    disclosure: ${disclosure.state ?? "NOT CHECKED"}`
      );
      if (disclosure.mechanism) lines.push(`    how: ${disclosure.mechanism.trim()}`);
      if (disclosure.gap) lines.push(`    GAP: ${disclosure.gap.trim()}`);
      if ((disclosure.searched ?? []).length > 0) {
        lines.push(`    searched: ${disclosure.searched.join("; ")}`);
      }
      if ((disclosure.refs ?? []).length > 0) {
        lines.push(`    evidence: ${disclosure.refs.join(", ")}`);
      }
      lines.push(`    cited: ${(f.refs ?? []).join(", ")}`);
    }
    lines.push("");
  }

  const roles = byCategory.role_and_risk ?? [];
  if (roles.length > 0) {
    lines.push("Role and risk tier:");
    for (const f of roles) {
      lines.push(
        `  role ${f.role} (${f.role_article}), tier ${f.tier} (${f.tier_article})`,
        `    Annex III area: ${f.annex_iii_area ?? "not screened"}`,
        `    obligations apply from: ${f.enforceable_from}`
      );
      if (f.not_high_risk_assessment) {
        const a = f.not_high_risk_assessment;
        lines.push(
          `    ${a.article} assessment: ground ${a.ground}, ` +
            `profiling ${a.profiling === true ? "yes" : "no"}`
        );
      }
      lines.push(`    cited: ${(f.refs ?? []).join(", ")}`);
    }
    lines.push("");
  }

  const standards = byCategory.standards_alignment ?? [];
  if (standards.length > 0) {
    lines.push("Standards alignment:");
    for (const f of standards) {
      lines.push(`  ${f.scheme}: ${f.value}${f.reference ? ` — ${f.reference}` : ""}`);
    }
    lines.push("");
  }

  if (lines.length === 0) return ["No findings recorded for this system.", ""];
  return [
    "All of the below are SUGGESTIONS with citations, for a person to accept or reject in Noru.",
    "",
    ...lines,
  ];
}

/**
 * The two alerts lifted above the plan. :diff is where a person decides whether to write, so an
 * obligation that is already enforceable must not arrive as one row among the createAsset calls.
 */
export function planAlerts(manifest) {
  const alerts = [];
  const findings = manifest.findings ?? {};
  for (const f of findings.prohibited_practices ?? []) {
    if (f.determination === "no_indication") continue;
    alerts.push({
      severity: f.determination === "indicated" ? "stop" : "review",
      category: "prohibited_practices",
      system: f.system,
      message: `${f.article} ${f.practice} — ${f.determination} on '${f.system}'.`,
    });
  }
  for (const f of findings.transparency_obligations ?? []) {
    const state = f.disclosure?.state;
    if (state !== "absent" && state !== "unclear") continue;
    alerts.push({
      severity: state === "absent" ? "gap" : "unresolved",
      category: "transparency_obligations",
      system: f.system,
      message:
        `${f.article} ${f.trigger} on '${f.system}': the required ` +
        `${f.required_action} is ${state}.`,
    });
  }
  return alerts;
}

export function evidenceBody(system, manifest) {
  const src = manifest.source;
  const lines = [
    `AI system: ${system.name}`,
    `Purpose: ${system.purpose}`,
    `Deployment: ${system.deployment}`,
    `Autonomy: ${system.autonomy}`,
    `Provider: ${system.provider ?? "(none declared)"}`,
    `Models: ${(system.models ?? []).join(", ") || "(none declared)"}`,
    `Human oversight: ${
      (system.human_oversight ?? []).map((o) => o.type).join(", ") || "none declared"
    }`,
    `Evals CI-gated: ${system.evals?.ci_gated === true ? "yes" : "no"}`,
    "",
    "Findings:",
    ...renderFindings(findingsFor(manifest, system.key)).map((l) => (l ? `  ${l}` : "")),
    `Interpretation owner: ${system.interpretation.owner}`,
    `Decided: ${system.interpretation.decided_at}`,
    `Expires: ${system.interpretation.expires_at ?? "(not set)"}`,
    `Rationale: ${system.interpretation.rationale}`,
    "",
    "Repository evidence:",
    ...(system.refs ?? []).map((r) => `  - ${r}`),
    "",
    `Source: ${src.slug} @ ${src.commit_sha} (${src.branch})`,
    `Generated by: ${src.generated_by}`,
  ];
  return lines.join("\n");
}

/**
 * Key-order-independent digest. The API gives no guarantee that a JSON object comes back with its
 * keys in the order they were sent, and in practice they do not match. Hashing the raw
 * JSON.stringify would make every second run look like a change, which would quietly destroy the
 * idempotency property.
 */
function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

function digest(value) {
  return createHash("sha256").update(stableStringify(value)).digest("hex");
}

function loadJson(path, label) {
  if (!existsSync(path)) {
    return { error: `${label} not found at ${path}` };
  }
  try {
    return { value: JSON.parse(readFileSync(path, "utf8")) };
  } catch (error) {
    return { error: `${label} at ${path} is not readable JSON (${error.message})` };
  }
}

/**
 * The state snapshot is untrusted input like any other tool output: it is read for comparison only,
 * and nothing in it is treated as an instruction.
 */
export function buildOperations(manifest, state) {
  const operations = [];
  const assets = state.assets ?? [];
  const vendors = state.vendors ?? [];
  const evidence = state.evidence ?? [];
  const controls = state.ai_controls ?? [];
  const slug = manifest.source.slug;

  for (const provider of manifest.providers ?? []) {
    const existing = vendors.find(
      (v) => String(v.name ?? "").toLowerCase() === provider.vendor_name.toLowerCase()
    );
    operations.push({
      operation: "createVendor",
      transport: "mcp",
      scope: "write:vendors",
      subject: provider.vendor_name,
      effect: existing ? "skip" : "create",
      reason: existing
        ? `a vendor named "${provider.vendor_name}" already exists (id ${existing.id}); nothing to create`
        : "no vendor with this name exists yet",
      idempotency: { kind: "server_dedupe", key: ["organizationId", "name"] },
      arguments: {
        name: provider.vendor_name,
        category: provider.category ?? "software_as_a_service",
        description:
          `Model provider discovered by ${manifest.source.generated_by} in ${slug}. ` +
          `Claims recorded: ${(provider.claims ?? []).map((c) => c.kind).join(", ") || "none"}.`,
        website: (provider.endpoints ?? [])[0] ?? null,
      },
    });
  }

  for (const system of manifest.ai_systems ?? []) {
    const externalId = `${slug}:${system.key}`;
    const existing = assets.find(
      (a) => a.source === ASSET_SOURCE && a.externalId === externalId
    );
    const args = {
      name: system.name,
      type: "software",
      source: ASSET_SOURCE,
      externalId,
      description: system.purpose,
      dataTypes: system.data_categories ?? [],
      metadata: {
        piece: PIECE,
        deployment: system.deployment,
        autonomy: system.autonomy,
        provider: system.provider ?? null,
        models: system.models ?? [],
        human_oversight: (system.human_oversight ?? []).map((o) => o.type),
        evals_ci_gated: system.evals?.ci_gated ?? false,
        // The findings travel with the asset, in the same order everything else prints them, so
        // that what is enforceable now is legible on the record itself and not only in the evidence
        // body next to it.
        findings: findingsFor(manifest, system.key),
        refs: system.refs ?? [],
        interpretation: system.interpretation,
        slug,
        commitSha: manifest.source.commit_sha,
        branch: manifest.source.branch,
      },
    };
    const unchanged =
      Boolean(existing) &&
      existing.name === args.name &&
      (existing.description ?? null) === (args.description ?? null) &&
      digest(existing.metadata ?? null) === digest(args.metadata);
    operations.push({
      operation: "createAsset",
      transport: "mcp",
      scope: "write:assets",
      subject: `${system.name} (${externalId})`,
      effect: unchanged ? "skip" : existing ? "update" : "create",
      reason: unchanged
        ? "the asset already carries exactly this payload"
        : existing
          ? `asset ${existing.id} exists on (source, externalId) and will be updated in place`
          : "no asset with this (source, externalId) yet",
      idempotency: { kind: "server_upsert", key: ["source", "externalId"] },
      arguments: args,
    });
  }

  for (const system of manifest.ai_systems ?? []) {
    const body = evidenceBody(system, manifest);
    const marker = evidenceMarker(system.key, digest(body));
    const title = `AI system inventory: ${system.name}`;
    const existing = evidence.find((e) => String(e.description ?? "").includes(marker));
    const stale = evidence.find(
      (e) =>
        !String(e.description ?? "").includes(marker) &&
        String(e.description ?? "").includes(`${EVIDENCE_MARKER_PREFIX}#${system.key}@`)
    );
    operations.push({
      operation: "createEvidence",
      transport: "mcp",
      scope: "write:evidence",
      subject: title,
      effect: existing ? "skip" : "create",
      reason: existing
        ? `evidence ${existing.id} already carries this exact content marker`
        : stale
          ? `evidence ${stale.id} covers this system but the content changed; a new record will be created ` +
            "(no idempotency key is documented for evidence — see the gap note in piece.json)"
          : "no evidence carries this system's marker yet",
      idempotency: {
        kind: "client_probe",
        key: ["description contains marker"],
        marker,
      },
      arguments: {
        title,
        description: `${marker} AI system inventory generated from ${slug} @ ${manifest.source.commit_sha} (${manifest.source.branch}).`,
        content: body,
        tags: [PIECE, slug],
        controlMappings: controls.map((c) => ({ controlId: c.id })),
      },
    });
  }

  if (controls.length === 0) {
    operations.push({
      operation: "linkEvidenceToControl",
      transport: "mcp",
      scope: "write:evidence",
      subject: "(no AI-framework controls in the state snapshot)",
      effect: "skip",
      reason:
        "noru-state.json listed no controls under the organization's AI frameworks, so the evidence " +
        "will land unlinked — enable an AI framework in Noru, or re-run :scan after refreshing the snapshot",
      idempotency: { kind: "client_probe", key: ["evidenceId", "controlId"] },
      arguments: {},
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

  const manifestPath = join(opts.repo, ".noru", "ai-inventory.yml");
  const parsedPath = join(opts.repo, ".noru", ".cache", "ai-inventory.parsed.json");
  const statePath = join(opts.repo, ".noru", ".cache", "noru-state.json");

  if (!existsSync(manifestPath)) {
    process.stderr.write(
      `error: no manifest at ${manifestPath} — run the piece's :scan command first\n`
    );
    return 1;
  }
  const parsed = loadJson(parsedPath, "validated manifest");
  if (parsed.error) {
    process.stderr.write(
      `error: ${parsed.error}\n` +
        "hint: python3 <plugin>/scripts/validate_manifest.py .noru/ai-inventory.yml " +
        "--emit-parsed=.noru/.cache/ai-inventory.parsed.json\n"
    );
    return 1;
  }
  const state = loadJson(statePath, "Noru state snapshot");
  if (state.error) {
    process.stderr.write(
      `error: ${state.error}\n` +
        "hint: the skill writes this from getOrganizationAssets, getOrganizationVendors, " +
        "getOrganizationEvidence and getOrganizationControls before running :diff\n"
    );
    return 1;
  }

  const manifest = parsed.value;
  const operations = buildOperations(manifest, state.value);
  const plan = writePlan(planPathFor(opts.repo, PIECE), {
    created_at: state.value.fetched_at ?? manifest.source.commit_sha,
    piece: PIECE,
    manifest: ".noru/ai-inventory.yml",
    manifest_sha256: sha256OfFile(manifestPath),
    provenance: {
      slug: manifest.source.slug,
      commit_sha: manifest.source.commit_sha,
      branch: manifest.source.branch,
    },
    operations,
    summary: summarize(operations),
  });

  // The alert list rides on stdout rather than in the plan file: the plan is the contract-shaped
  // artifact :push verifies, and it stays exactly what the shared writer produces.
  const alerts = planAlerts(manifest);

  if (opts.json) {
    process.stdout.write(
      `${JSON.stringify({ ...plan, alerts }, null, opts.quiet ? 0 : 2)}\n`
    );
  } else if (!opts.quiet) {
    const banner =
      alerts.length === 0
        ? ""
        : [
            "=".repeat(96),
            ...alerts.map((a) => `  ${a.severity.toUpperCase()}: ${a.message}`),
            "  These are enforceable now. The plan below is about landing them in Noru for review,",
            "  not about resolving them.",
            "=".repeat(96),
            "",
          ].join("\n") + "\n";
    process.stdout.write(`${banner}${renderPlanText(plan)}\n`);
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
