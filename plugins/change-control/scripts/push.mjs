#!/usr/bin/env node
// :push for the change-control piece — contract requirements 4 and 5.
//
// MCP authentication belongs to the MCP client, so a script cannot make the calls itself without
// handling a credential — which the contract forbids. What it does instead is the part that must be
// deterministic and gated:
//
//   1. refuse to run without an explicit --confirm
//   2. refuse to run against a plan that does not match the manifest on disk right now
//   3. drop every operation the plan already marked "skip"
//   4. emit the exact, ordered tool calls to .noru/.cache/change-control.calls.json
//
// The skill then executes those calls through the MCP client and nothing else.
//
// Usage: node push.mjs [--repo=<path>] --confirm [--output=json|text] [--quiet]
// Exit codes:
//   0 = calls emitted (an empty call list on a re-run is success — that is idempotency)
//   1 = no plan, or the plan is stale
//   2 = usage error, including a missing --confirm

import { mkdirSync, realpathSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

import { assertPlanFresh, parseCommonArgs, planPathFor, readPlan, redact } from "./lib/plan.mjs";

const PIECE = "change-control";
const USAGE = "usage: push.mjs [--repo=<path>] --confirm [--output=json|text] [--quiet]\n";

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
  const loaded = readPlan(planPathFor(opts.repo, PIECE));
  if (!loaded.ok) {
    process.stderr.write(`error: ${loaded.reason}\n`);
    return 1;
  }
  const { plan } = loaded;

  const fresh = assertPlanFresh(plan, manifestPath);
  if (!fresh.ok) {
    process.stderr.write(`error: ${fresh.reason}\n`);
    return 1;
  }

  if (!opts.confirm) {
    process.stderr.write(
      "error: refusing to push without --confirm.\n" +
        `  ${plan.summary.create} record(s) would be created and ${plan.summary.update} updated in Noru.\n` +
        "  Review the plan first (the piece's :diff command prints it), then re-run with --confirm.\n"
    );
    return 2;
  }

  const calls = plan.operations
    .filter((op) => op.effect !== "skip" && op.operation)
    .map((op, index) => ({
      order: index + 1,
      tool: op.operation,
      transport: op.transport,
      scope: op.scope,
      subject: op.subject,
    effect: op.effect,
    idempotency: op.idempotency,
    ...(op.idempotency?.fallback
      ? {
          compatibility: {
            when: "connected tool schema does not expose idempotencyKey",
            mode: op.idempotency.fallback.kind,
            arguments: Object.fromEntries(
              Object.entries(op.arguments).filter(([key]) => key !== "idempotencyKey")
            ),
          },
        }
      : {}),
    arguments: op.arguments,
    }));

  const callsPath = join(opts.repo, ".noru", ".cache", `${PIECE}.calls.json`);
  const payload = {
    piece: PIECE,
    manifest: plan.manifest,
    manifest_sha256: plan.manifest_sha256,
    target: plan.target,
    repository: plan.repository,
    piece_version: plan.piece_version,
    plan_expires_at: plan.expires_at,
    required_scopes: plan.required_scopes,
    provenance: plan.provenance,
    confirmed: true,
    calls,
  };
  try {
    mkdirSync(dirname(callsPath), { recursive: true });
    writeFileSync(callsPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  } catch (error) {
    process.stderr.write(`error: ${redact(error.message)}\n`);
    return 2;
  }

  if (opts.json) {
    process.stdout.write(
      `${JSON.stringify({ ...payload, calls_file: callsPath }, null, opts.quiet ? 0 : 2)}\n`
    );
  } else if (!opts.quiet) {
    process.stdout.write(
      calls.length === 0
        ? "nothing to push: every operation in the plan was already satisfied in Noru.\n" +
            "This is the expected result of a second run.\n"
        : [
            `${calls.length} confirmed MCP call(s) written to ${callsPath}:`,
            ...calls.map((c) => `  ${String(c.order).padStart(2)}. ${c.tool.padEnd(22)} ${c.subject}`),
            "",
            "Execute exactly these calls, in this order, through the Noru MCP connection.",
          ].join("\n") + "\n"
    );
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
