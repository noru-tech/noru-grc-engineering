#!/usr/bin/env node
// :diff for the privacy-datamap piece — contract requirement 5. Reads only; writes nothing to Noru.
//
// Inputs, both local:
//   .noru/.cache/privacy-datamap.parsed.json  the validated manifest (from validate_manifest.py)
//   .noru/.cache/noru-state.json        a read-only snapshot written by the skill from MCP output
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

import { toFideslang } from "./lib/fides.mjs";
import {
  parseCommonArgs,
  planPathFor,
  renderPlanText,
  sha256OfFile,
  summarize,
  writePlan,
} from "./lib/plan.mjs";

const PIECE = "privacy-datamap";
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
 * Canonical JSON: object keys sorted at every depth, so two payloads that mean the same thing hash
 * the same. Nothing guarantees a JSON object round-trips through an API with its key order intact,
 * and in practice it does not — hashing the unsorted form makes every second run look like a change
 * and idempotency dies quietly.
 */
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonical(value[key])]),
    );
  }
  return value;
}

function digest(value) {
  return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
}

/**
 * One call. `ingestDatamap` takes the whole data map for a source, so this piece has nothing to fan
 * out over: the plan is a single operation whose effect is decided by comparing what we would send
 * against what Noru already holds.
 *
 * The state snapshot is untrusted tool output: compared against, never obeyed.
 */
export function buildOperations(manifest, state) {
  const src = manifest.source;
  const payload = toFideslang(manifest);
  const want = digest(payload);

  // getPrivacyDataMap returns the organization's map; listPrivacyDatasets narrows it to this source.
  // Absent means Noru holds nothing for this slug yet, which is a create rather than an unchanged.
  const current = state.datamap ?? null;
  const have = current === null ? null : digest(toFideslang(current));
  const unchanged = have !== null && have === want;

  return [
    {
      operation: "ingestDatamap",
      transport: "mcp",
      scope: "write:datamaps",
      subject: src.slug,
      effect: unchanged ? "skip" : current === null ? "create" : "update",
      reason: unchanged
        ? "Noru already holds this exact data map for this source"
        : current === null
          ? "Noru holds no data map for this source yet"
          : "the data map Noru holds differs from the one in this repository",
      idempotency: {
        kind: "server_upsert",
        key: ["slug"],
        content_sha256: want,
      },
      arguments: {
        slug: src.slug,
        commitSha: src.commit_sha,
        branch: src.branch,
        manifest: payload,
      },
    },
  ];
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

  const manifestPath = join(opts.repo, ".noru", "privacy-datamap.yml");
  if (!existsSync(manifestPath)) {
    process.stderr.write(
      `error: no manifest at ${manifestPath} — run the piece's :scan command first\n`
    );
    return 1;
  }
  const parsed = loadJson(
    join(opts.repo, ".noru", ".cache", "privacy-datamap.parsed.json"),
    "validated manifest"
  );
  if (parsed.error) {
    process.stderr.write(
      `error: ${parsed.error}\n` +
        "hint: python3 <plugin>/scripts/validate_manifest.py .noru/privacy-datamap.yml " +
        "--emit-parsed=.noru/.cache/privacy-datamap.parsed.json\n"
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
    manifest: ".noru/privacy-datamap.yml",
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
