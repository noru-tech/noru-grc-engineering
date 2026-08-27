#!/usr/bin/env node
// :diff for the audit-pack piece — contract requirement 5.
//
// Reads two local files and writes nothing to Noru:
//   .noru/.cache/audit-pack.parsed.json  the validated manifest (validate_manifest.py)
//   .noru/.cache/noru-state.json         existing evidence and its control links, written by the
//                                        skill from getOrganizationEvidence
//
// What lands, and what deliberately does not. The pack itself — the index, the sampling worksheets,
// the assembled inventory — is a LOCAL deliverable: it is a point-in-time export for handover, and
// pushing it into Noru would be duplicating a register Noru already keeps. What belongs in the
// register is the part a folder cannot hold: **the tested conclusion for each control over this
// window**, and how it was reached. So one workpaper becomes one evidence record, mapped to the one
// control it is about.
//
// One workpaper, one record, one control. A single blob mapped to forty controls is the antipattern
// that makes evidence unreadable, and it is exactly what "assemble a pack" invites.
//
// No idempotency key is documented for evidence, so this piece does not assume one: every record
// lands with a marker in its description built from the pack key, the workpaper key and a digest of
// the rendered workpaper, and the diff looks for that marker in evidence already in the
// organization. Same workpaper, same content -> skip. Changed content -> a new record, because a
// re-tested control is a different account and an auditor should see both.
//
// Usage: node diff.mjs [--repo=<path>] [--output=json|text] [--quiet]
// Exit codes: 0 plan written, 1 missing/unusable input, 2 usage error.

import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import {
  parseCommonArgs,
  planPathFor,
  renderPlanText,
  sha256OfFile,
  summarize,
  writePlan,
} from "./lib/plan.mjs";

const PIECE = "audit-pack";
const MARKER_PREFIX = "noru-grc-engineering:audit-pack";
const USAGE = "usage: diff.mjs [--repo=<path>] [--output=json|text] [--quiet]\n";

export function workpaperMarker(packKey, workpaperKey, contentDigest) {
  return `[${MARKER_PREFIX}#${packKey}/${workpaperKey}@${contentDigest.slice(0, 16)}]`;
}

function digest(text) {
  return createHash("sha256").update(text).digest("hex");
}

function loadJson(path, label) {
  if (!existsSync(path)) return { error: `${label} not found at ${path}` };
  try {
    return { value: JSON.parse(readFileSync(path, "utf8")) };
  } catch (error) {
    return { error: `${label} at ${path} is not readable JSON (${error.message})` };
  }
}

/**
 * Free text from the manifest, normalised to a single line before it reaches anything digested.
 *
 * This is not cosmetic. The two YAML loaders a validator may use do not agree byte for byte on a
 * folded (`>`) block scalar — one keeps the trailing newline the YAML spec calls for and the bundled
 * fallback does not — so the SAME manifest can parse to two slightly different strings depending on
 * whether PyYAML happened to be importable. Feeding that straight into a content digest would make
 * this piece's identity depend on the machine it ran on: push from a laptop without PyYAML, push
 * again from CI with it, and the second push files a duplicate rather than skipping. Normalising
 * here makes the identity a property of the manifest and nothing else.
 */
function text(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function inspectedLine(row) {
  const digestPart = row.sha256 ? ` (sha256 ${row.sha256})` : "";
  const note = row.note ? ` — ${text(row.note)}` : "";
  return `  - [${row.kind}] ${row.reference}${digestPart}${note}`;
}

function exceptionLine(exception) {
  const resolved = exception.resolved_on ? `, resolved ${exception.resolved_on}` : "";
  return (
    `  - ${exception.reference}: ${text(exception.description)} ` +
    `[${exception.disposition}, owner ${exception.owner}${resolved}]`
  );
}

function samplingLines(workpaper) {
  if (!workpaper.population || !workpaper.sample) return ["Sampling: not applicable"];
  const sample = workpaper.sample;
  return [
    `Population: ${workpaper.population.file} — ${workpaper.population.size} item(s) ` +
      `(sha256 ${workpaper.population.sha256})`,
    `Sampling method: ${sample.method}`,
    `Seed: ${sample.seed ?? "(none)"}`,
    `Sample size: ${sample.size}`,
    "Drawn:",
    ...sample.drawn.map((key) => `  - ${key}`),
    "Redraw it by ordering the population on sha256(seed + \"|\" + reference) ascending and taking " +
      "the first `sample size` rows.",
  ];
}

/**
 * The evidence body. This is the workpaper as an auditor reads it: what was tested, what was
 * opened, how it was sampled, what was found and what was concluded — and nothing that is not in
 * the reviewed manifest.
 */
export function workpaperBody(workpaper, manifest) {
  const src = manifest.source;
  const pack = manifest.pack;
  const inspected = workpaper.inspected ?? [];
  const exceptions = workpaper.exceptions ?? [];
  const lines = [
    `Audit workpaper: ${workpaper.control_id}`,
    `Pack: ${text(pack.title ?? pack.key)}`,
    `Framework: ${manifest.queue_snapshot.framework_id}`,
    `Window: ${pack.window.from} to ${pack.window.to}`,
    `Prepared by: ${pack.prepared_by}`,
    `Reviewed by: ${pack.reviewed_by ?? "(nobody)"}`,
    "",
    "What was tested:",
    text(workpaper.scope),
    "",
    `Inspected (${inspected.length}):`,
    ...(inspected.length > 0 ? inspected.map(inspectedLine) : ["  (nothing recorded)"]),
    "",
    ...samplingLines(workpaper),
    "",
    `Exceptions (${exceptions.length}):`,
    ...(exceptions.length > 0 ? exceptions.map(exceptionLine) : ["  (none)"]),
    "",
    `Conclusion: ${workpaper.conclusion}`,
    `Concluded by: ${workpaper.interpretation.owner} on ${workpaper.interpretation.decided_at}`,
    `Stands until: ${workpaper.interpretation.expires_at}`,
    `Rationale: ${text(workpaper.interpretation.rationale)}`,
    "",
    "Read from:",
    ...(workpaper.refs ?? []).map((r) => `  - ${r}`),
    "",
    `Source: ${src.slug} @ ${src.commit_sha} (${src.branch})`,
    `Generated by: ${src.generated_by}`,
  ];
  return lines.join("\n");
}

function mappingArgs(workpaper) {
  const items = workpaper.evidence_item_ids ?? [];
  return {
    controlId: workpaper.control_id,
    ...(items.length > 0 ? { evidenceItemIds: items } : {}),
  };
}

/**
 * The state snapshot is untrusted tool output. It is compared against, never obeyed.
 */
export function buildOperations(manifest, state) {
  const evidence = state.evidence ?? [];
  const pack = manifest.pack;
  const src = manifest.source;
  const operations = [];

  for (const workpaper of manifest.workpapers ?? []) {
    const body = workpaperBody(workpaper, manifest);
    const marker = workpaperMarker(pack.key, workpaper.key, digest(body));
    const title =
      `Workpaper: ${workpaper.control_id} — ${text(pack.title ?? pack.key)} ` +
      `(${pack.window.from} to ${pack.window.to})`;
    const existing = evidence.find((e) => String(e.description ?? "").includes(marker));
    const superseded = evidence.find(
      (e) =>
        !String(e.description ?? "").includes(marker) &&
        String(e.description ?? "").includes(`${MARKER_PREFIX}#${pack.key}/${workpaper.key}@`)
    );

    operations.push({
      operation: "createEvidence",
      transport: "mcp",
      scope: "write:evidence",
      subject: `${workpaper.conclusion}: ${workpaper.control_id}`,
      effect: existing ? "skip" : "create",
      reason: existing
        ? `evidence ${existing.id} already carries this workpaper's exact content marker`
        : superseded
          ? `evidence ${superseded.id} covers workpaper '${workpaper.key}' but the account has ` +
            "changed; a new record will be created so both conclusions remain visible (no " +
            "idempotency key is documented for evidence — see the gap note in piece.json)"
          : "no evidence carries this workpaper's marker yet",
      idempotency: { kind: "client_probe", key: ["description contains marker"], marker },
      arguments: {
        title,
        description:
          `${marker} Audit workpaper for ${workpaper.control_id} over ${pack.window.from}..` +
          `${pack.window.to}, concluded ${workpaper.conclusion} by ` +
          `${workpaper.interpretation.owner} on ${workpaper.interpretation.decided_at}, standing ` +
          `until ${workpaper.interpretation.expires_at}. Source: ${src.slug} @ ${src.commit_sha} ` +
          `(${src.branch}).`,
        content: body,
        tags: [PIECE, workpaper.conclusion, manifest.queue_snapshot.framework_id],
        controlMappings: [mappingArgs(workpaper)],
      },
    });

    // Only reachable when the record already exists: createEvidence carries the mapping itself, so
    // a link operation is for expectations added to the workpaper after it was pushed.
    if (!existing) continue;
    const linked = new Set(
      (existing.linkedControls ?? []).map((c) => String(c.id ?? c.controlId ?? "").toLowerCase())
    );
    if (linked.has(String(workpaper.control_id).toLowerCase())) continue;
    operations.push({
      operation: "linkEvidenceToControl",
      transport: "mcp",
      scope: "write:evidence",
      subject: `${workpaper.key} -> ${workpaper.control_id}`,
      effect: "create",
      reason:
        `evidence ${existing.id} exists but is not linked to '${workpaper.control_id}'; the mapping ` +
        "was added to the workpaper after the record was filed",
      idempotency: { kind: "client_probe", key: ["evidenceId", "controlId"] },
      arguments: { evidenceId: existing.id, ...mappingArgs(workpaper) },
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

  const manifestPath = join(opts.repo, ".noru", "audit-pack.yml");
  const parsedPath = join(opts.repo, ".noru", ".cache", "audit-pack.parsed.json");
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
        "hint: python3 <plugin>/scripts/validate_manifest.py .noru/audit-pack.yml " +
        "--emit-parsed=.noru/.cache/audit-pack.parsed.json\n"
    );
    return 1;
  }
  const state = loadJson(statePath, "Noru state snapshot");
  if (state.error) {
    process.stderr.write(
      `error: ${state.error}\n` +
        "hint: the skill writes this from getOrganizationEvidence before running :diff\n"
    );
    return 1;
  }

  const manifest = parsed.value;
  const operations = buildOperations(manifest, state.value);
  const plan = writePlan(planPathFor(opts.repo, PIECE), {
    created_at: state.value.fetched_at ?? manifest.source.commit_sha,
    piece: PIECE,
    manifest: ".noru/audit-pack.yml",
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

if (import.meta.url === `file://${process.argv[1]}`) {
  process.exit(main(process.argv.slice(2)));
}
