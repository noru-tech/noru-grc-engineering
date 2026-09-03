#!/usr/bin/env node
// :push for the review-signoff piece — contract requirements 4 and 5.
//
// This piece lands in Noru over MCP, and MCP authentication belongs to the MCP client (OAuth, or a
// bearer key the client holds). A script therefore cannot and must not make the calls itself: it
// would have to handle a credential, which the contract forbids. What this script does instead is
// the part that must be deterministic and gated:
//
//   1. refuse to run without an explicit --confirm
//   2. refuse to run against a plan that does not match the manifest on disk right now
//   3. drop every operation the plan already marked "skip"
//   4. emit the exact, ordered tool calls to .noru/.cache/review-signoff.calls.json
//
// One wrinkle this piece has and the others do not: setting the sign-off's expiry needs the id of
// the evidence record the previous call creates. Those calls carry `depends_on`, and an argument
// left null is filled in from the named earlier call's result. That substitution is the ONLY thing
// the executing client may change about a call.
//
// Usage: node push.mjs [--repo=<path>] --confirm [--output=json|text] [--quiet]
// Exit codes:
//   0 = calls emitted (an empty call list on a re-run is success — that is idempotency)
//   1 = no plan, or the plan is stale
//   2 = usage error, including a missing --confirm

import { writeFileSync, mkdirSync, realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

import {
  assertPlanFresh,
  parseCommonArgs,
  planPathFor,
  readPlan,
  redact,
} from "./lib/plan.mjs";

const PIECE = "review-signoff";
const USAGE = "usage: push.mjs [--repo=<path>] --confirm [--output=json|text] [--quiet]\n";

/**
 * Re-point each dependency at the position the call it depends on ends up in, since skipped
 * operations are dropped. A dependency whose target was skipped is a bug in the plan, not something
 * to paper over, so it is reported rather than silently dropped.
 */
export function resolveDependencies(pending, indexInPlan) {
  const positionOf = new Map();
  pending.forEach((op, i) => positionOf.set(indexInPlan[i], i + 1));

  return pending.map((op, i) => {
    const call = {
      order: i + 1,
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
    };
    if (!op.depends_on) return call;
    const order = positionOf.get(op.depends_on.operation_index);
    if (order === undefined) {
      call.error =
        `depends on an operation that is not in this push (plan index ${op.depends_on.operation_index}); ` +
        "do not execute this call — re-run :diff";
      return call;
    }
    call.depends_on = {
      order,
      field: op.depends_on.field,
      note: op.depends_on.note,
    };
    return call;
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

  const manifestPath = join(opts.repo, ".noru", "review-signoff.yml");
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
        `  ${plan.summary.create} sign-off(s) would be created and ${plan.summary.update} updated in Noru.\n` +
        "  Review the plan first (the piece's :diff command prints it), then re-run with --confirm.\n"
    );
    return 2;
  }

  const indexInPlan = [];
  const pending = plan.operations.filter((op, i) => {
    if (op.effect === "skip" || !op.operation) return false;
    indexInPlan.push(i);
    return true;
  });
  const calls = resolveDependencies(pending, indexInPlan);
  const broken = calls.filter((c) => c.error);

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

  if (broken.length > 0) {
    process.stderr.write(
      `error: ${broken.length} call(s) depend on an operation that is not in this push — re-run :diff\n`
    );
    return 1;
  }

  if (opts.json) {
    process.stdout.write(
      `${JSON.stringify({ ...payload, calls_file: callsPath }, null, opts.quiet ? 0 : 2)}\n`
    );
  } else if (!opts.quiet) {
    if (calls.length === 0) {
      process.stdout.write(
        "nothing to push: every sign-off in the plan is already in Noru with its expiry set.\n" +
          "This is the expected result of a second run.\n"
      );
    } else {
      process.stdout.write(
        [
          `${calls.length} confirmed MCP call(s) written to ${callsPath}:`,
          ...calls.map(
            (c) =>
              `  ${String(c.order).padStart(2)}. ${c.tool.padEnd(22)} ${c.subject}` +
              (c.depends_on ? `  [${c.depends_on.field} from call ${c.depends_on.order}]` : "")
          ),
          "",
          "Execute exactly these calls, in this order, through the Noru MCP connection.",
          "Where a call carries depends_on, substitute that one field from the earlier call's result",
          "and change nothing else.",
        ].join("\n") + "\n"
      );
    }
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
