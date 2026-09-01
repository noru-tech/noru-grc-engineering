#!/usr/bin/env node
// :diff for the evidence-push piece — contract requirement 5.
//
// Reads two local files and writes nothing to Noru:
//   .noru/.cache/evidence-push.parsed.json  the validated manifest (from validate_manifest.py)
//   .noru/.cache/noru-state.json            existing evidence, written by the skill from
//                                           getOrganizationEvidence
//
// POST /v1/evidence/upload has no idempotency key: two identical uploads produce two evidence
// records. So this piece probes instead. Every upload carries a marker in its description that
// includes the artifact's content digest, and the diff looks for that marker in the evidence
// already in the org. Same file, same mapping -> skip. That is the whole idempotency story for
// this piece, and it is a client-side workaround for a server-side gap recorded in piece.json.
//
// Usage: node diff.mjs [--repo=<path>] [--output=json|text] [--quiet]
// Exit codes: 0 plan written, 1 missing/unusable input, 2 usage error.

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

const PIECE = "evidence-push";
const MARKER_PREFIX = "noru-grc-engineering:evidence-push";
const USAGE = "usage: diff.mjs [--repo=<path>] [--output=json|text] [--quiet]\n";

export function uploadMarker(sha256) {
  return `[${MARKER_PREFIX}#${sha256.slice(0, 16)}]`;
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
 * The state snapshot is untrusted tool output. It is compared against, never obeyed.
 */
export function buildOperations(manifest, state, repo) {
  const evidence = state.evidence ?? [];
  const src = manifest.source;
  return (manifest.uploads ?? []).map((upload) => {
    const marker = uploadMarker(upload.sha256);
    // Skip only on an exact content-marker match. A same-titled record with different content is
    // a genuinely new artifact (last quarter's access review is not this quarter's), so it uploads.
    const existing = evidence.find((e) =>
      String(e.description ?? "").includes(marker)
    );
    const missingFile = !existsSync(join(repo, upload.file));
    const mappings = upload.control_mappings.map((m) => ({
      controlId: m.control_id,
      ...(m.evidence_item_ids && m.evidence_item_ids.length > 0
        ? { evidenceItemIds: m.evidence_item_ids }
        : {}),
    }));

    return {
      operation: "POST /v1/evidence/upload",
      transport: "rest",
      scope: "write:evidence",
      subject: `${upload.file} -> ${mappings.map((m) => m.controlId).join(", ")}`,
      effect: missingFile ? "skip" : existing ? "skip" : "create",
      reason: missingFile
        ? `the file ${upload.file} is not in the repository working tree`
        : existing
          ? `evidence ${existing.id} already carries this content marker (${marker})`
          : "no evidence in the organization carries this artifact's content marker",
      idempotency: { kind: "client_probe", key: ["description contains marker"], marker },
      arguments: {
        file: upload.file,
        sha256: upload.sha256,
        mimeType: upload.mime_type,
        sizeBytes: upload.size_bytes,
        form: {
          title: upload.title,
          description:
            `${marker} ${upload.description ?? ""} ` +
            `Interpretation: ${upload.interpretation.owner} on ${upload.interpretation.decided_at}` +
            (upload.interpretation.expires_at
              ? `, review by ${upload.interpretation.expires_at}`
              : "") +
            `. ${upload.interpretation.rationale} ` +
            `Source: ${src.slug} @ ${src.commit_sha} (${src.branch}).`,
          tags: (upload.tags ?? []).join(","),
          // controlMappings is the preferred field; the legacy controlIds field is never sent.
          controlMappings: JSON.stringify(mappings),
          ...(upload.expiry_date ? { expiryDate: upload.expiry_date } : {}),
        },
      },
    };
  });
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

  const manifestPath = join(opts.repo, ".noru", "evidence-push.yml");
  const parsedPath = join(opts.repo, ".noru", ".cache", "evidence-push.parsed.json");
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
        "hint: python3 <plugin>/scripts/validate_manifest.py .noru/evidence-push.yml " +
        "--emit-parsed=.noru/.cache/evidence-push.parsed.json\n"
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
  const operations = buildOperations(manifest, state.value, opts.repo);
  const plan = writePlan(planPathFor(opts.repo, PIECE), {
    created_at: state.value.fetched_at ?? manifest.source.commit_sha,
    piece: PIECE,
    manifest: ".noru/evidence-push.yml",
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
