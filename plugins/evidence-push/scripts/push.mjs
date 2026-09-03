#!/usr/bin/env node
// :push for the evidence-push piece — contract requirements 4 and 5.
//
// Unlike ai-inventory, this piece really does perform the write itself. It has to: the published
// createEvidence tool says plainly that "File uploads (multipart) are not supported via MCP",
// because tool arguments are JSON and cannot carry a multipart body. So the transport here is
// REST — POST /v1/evidence/upload, multipart/form-data — and the credential is a NORU_API_KEY
// environment variable read at the point of use.
//
// Credential rules this file obeys, and that a reviewer should check it still obeys:
//   * NORU_API_KEY is read from the environment when a request is about to be made, never earlier
//   * it is never written to a file, a plan, a log line, or stdout
//   * every error string that leaves this process goes through redact()
//
// Usage:
//   node push.mjs [--repo=<path>] --confirm [--dry-run] [--output=json|text] [--quiet]
// Environment:
//   NORU_API_KEY   required for a real push (not for --dry-run)
//   NORU_API_URL   optional, defaults to https://api.noru.tech
// Exit codes:
//   0 = every planned upload succeeded (an empty plan on a re-run is success — that is idempotency)
//   1 = no plan, stale plan, missing credential, or an upload failed
//   2 = usage error, including a missing --confirm

import { createHash } from "node:crypto";
import { readFileSync, existsSync, realpathSync } from "node:fs";
import { basename, join } from "node:path";
import { pathToFileURL } from "node:url";

import {
  assertPlanFresh,
  parseCommonArgs,
  planPathFor,
  readPlan,
  redact,
} from "./lib/plan.mjs";

const PIECE = "evidence-push";
const DEFAULT_BASE_URL = "https://api.noru.tech";
const UPLOAD_PATH = "/v1/evidence/upload";
const USAGE =
  "usage: push.mjs [--repo=<path>] --confirm [--dry-run] [--output=json|text] [--quiet]\n";

async function uploadOne(baseUrl, apiKey, repo, op) {
  const absolute = join(repo, op.arguments.file);
  if (!existsSync(absolute)) {
    return { ok: false, status: 0, error: `file not found: ${op.arguments.file}` };
  }
  const bytes = readFileSync(absolute);

  // The manifest carries the digest :scan computed. Recompute it from the bytes
  // we are about to send: if the file changed after the plan was written, the
  // plan no longer describes what would be uploaded.
  const localDigest = createHash("sha256").update(bytes).digest("hex");
  if (op.arguments.sha256 && localDigest !== op.arguments.sha256) {
    return {
      ok: false,
      status: 0,
      error:
        `file changed since :scan: ${op.arguments.file}\n` +
        `  manifest sha256 ${op.arguments.sha256}\n` +
        `  on disk now      ${localDigest}\n` +
        "  re-run :scan and :diff before pushing.",
    };
  }

  const form = new FormData();
  form.append(
    "file",
    new Blob([bytes], { type: op.arguments.mimeType }),
    basename(op.arguments.file)
  );
  for (const [key, value] of Object.entries(op.arguments.form)) {
    if (value !== undefined && value !== null && value !== "") {
      form.append(key, String(value));
    }
  }
  // Let Noru reject the upload itself if the bytes it receives disagree.
  form.append("expectedDigest", localDigest);

  let response;
  try {
    response = await fetch(`${baseUrl}${UPLOAD_PATH}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Idempotency-Key": op.arguments.idempotencyKey,
      },
      body: form,
    });
  } catch (error) {
    return { ok: false, status: 0, error: redact(error.message) };
  }

  const text = await response.text();
  if (!response.ok) {
    return { ok: false, status: response.status, error: redact(text).slice(0, 500) };
  }
  let evidenceId = null;
  let idempotencyStatus = null;
  let storedDigest = null;
  try {
    const parsed = JSON.parse(text);
    evidenceId = parsed?.data?.id ?? parsed?.id ?? null;
    storedDigest = parsed?.data?.integrity?.artifact?.digest ?? null;
    idempotencyStatus = parsed?.data?.idempotencyStatus ?? null;
  } catch {
    evidenceId = null;
  }

  // Recompute rather than trust the stamp. Noru returns the digest it computed
  // over the bytes it stored; it has to equal the one we computed here, or the
  // artifact in Noru is not the artifact on this machine. A server that does
  // not return an integrity block yet is not an error -- it is an older
  // deployment -- but a server that returns a DIFFERENT digest is.
  if (storedDigest && storedDigest !== localDigest) {
    return {
      ok: false,
      status: response.status,
      evidenceId,
      error:
        `digest mismatch after upload: ${op.arguments.file}\n` +
        `  sent    ${localDigest}\n` +
        `  stored  ${storedDigest}\n` +
        "  the artifact in Noru is not the artifact that was uploaded.",
    };
  }

  return {
    ok: true,
    status: response.status,
    evidenceId,
    digestVerified: Boolean(storedDigest),
    idempotencyStatus: idempotencyStatus ?? "created",
    idempotencyMode: idempotencyStatus ? "server_key" : "legacy_marker_probe",
  };
}

async function main(argv) {
  const opts = parseCommonArgs(argv, { dryRun: false });
  for (const arg of [...opts.rest]) {
    if (arg === "--dry-run") {
      opts.dryRun = true;
      opts.rest.splice(opts.rest.indexOf(arg), 1);
    }
  }
  if (opts.help) {
    process.stdout.write(USAGE);
    return 0;
  }
  if (opts.rest.length > 0) {
    process.stderr.write(`error: unexpected argument '${opts.rest[0]}'\n${USAGE}`);
    return 2;
  }

  const manifestPath = join(opts.repo, ".noru", "evidence-push.yml");
  const loaded = readPlan(planPathFor(opts.repo, PIECE));
  if (!loaded.ok) {
    process.stderr.write(`error: ${loaded.reason}\n`);
    return 1;
  }
  const { plan } = loaded;
  const binding = {
    target: plan.target,
    repository: plan.repository,
    piece_version: plan.piece_version,
    plan_expires_at: plan.expires_at,
    required_scopes: plan.required_scopes,
  };

  const fresh = assertPlanFresh(plan, manifestPath);
  if (!fresh.ok) {
    process.stderr.write(`error: ${fresh.reason}\n`);
    return 1;
  }

  if (!opts.confirm) {
    process.stderr.write(
      "error: refusing to upload without --confirm.\n" +
        `  ${plan.summary.create} file(s) would be uploaded to Noru as evidence.\n` +
        "  Review the plan first (the piece's :diff command prints it), then re-run with --confirm.\n"
    );
    return 2;
  }

  const pending = plan.operations.filter((op) => op.effect === "create");
  const baseUrl = (process.env.NORU_API_URL ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
  const plannedBaseUrl = new URL(plan.target.mcp_endpoint).origin;
  if (baseUrl !== plannedBaseUrl) {
    process.stderr.write(
      `error: REST endpoint changed (plan ${plannedBaseUrl}, current ${baseUrl}) — re-run :diff\n`
    );
    return 1;
  }

  if (pending.length === 0) {
    const payload = { piece: PIECE, ...binding, ok: true, uploaded: 0, skipped: plan.operations.length, results: [] };
    if (opts.json) process.stdout.write(`${JSON.stringify(payload, null, opts.quiet ? 0 : 2)}\n`);
    else if (!opts.quiet) {
      process.stdout.write(
        "nothing to upload: every artifact in the plan is already in Noru.\n" +
          "This is the expected result of a second run.\n"
      );
    }
    return 0;
  }

  if (opts.dryRun) {
    const payload = {
      piece: PIECE,
      ...binding,
      ok: true,
      dry_run: true,
      base_url: baseUrl,
      endpoint: UPLOAD_PATH,
      would_upload: pending.map((op) => ({
        file: op.arguments.file,
        sha256: op.arguments.sha256,
        mime_type: op.arguments.mimeType,
        size_bytes: op.arguments.sizeBytes,
        idempotency_key: op.arguments.idempotencyKey,
        control_mappings: JSON.parse(op.arguments.form.controlMappings),
      })),
    };
    if (opts.json) process.stdout.write(`${JSON.stringify(payload, null, opts.quiet ? 0 : 2)}\n`);
    else if (!opts.quiet) {
      process.stdout.write(
        [
          `dry run against ${baseUrl}${UPLOAD_PATH} — no request will be made:`,
          ...payload.would_upload.map(
            (u) => `  ${u.file} (${u.mime_type}, ${u.size_bytes} bytes) -> ${u.control_mappings.map((m) => m.controlId).join(", ")}`
          ),
        ].join("\n") + "\n"
      );
    }
    return 0;
  }

  // Read at the point of use, never earlier, and never stored.
  const apiKey = process.env.NORU_API_KEY;
  if (!apiKey) {
    process.stderr.write(
      "error: NORU_API_KEY is not set.\n" +
        "  File upload is REST-only (MCP tool arguments cannot carry a multipart body), so this\n" +
        "  step needs a bearer key with the write:evidence scope. Export it in your shell for this\n" +
        "  command only; do not write it to a file in the repository.\n"
    );
    return 1;
  }

  const results = [];
  let failures = 0;
  for (const op of pending) {
    // Sequential on purpose: Noru rate-limits at 500 requests per 10 minutes per key, and a
    // partial failure is much easier to reason about in order.
    const result = await uploadOne(baseUrl, apiKey, opts.repo, op);
    if (!result.ok) failures += 1;
    results.push({
      file: op.arguments.file,
      sha256: op.arguments.sha256,
      ok: result.ok,
      status: result.status,
      evidence_id: result.evidenceId ?? null,
      outcome: result.idempotencyStatus ?? null,
      idempotency_mode: result.idempotencyMode ?? null,
      error: result.error ?? null,
    });
  }

  const payload = {
    piece: PIECE,
    ...binding,
    ok: failures === 0,
    base_url: baseUrl,
    uploaded: results.filter((r) => r.ok).length,
    created: results.filter((r) => r.ok && r.outcome === "created").length,
    reused: results.filter((r) => r.ok && r.outcome === "reused").length,
    failed: failures,
    skipped: plan.operations.length - pending.length,
    results,
  };

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(payload, null, opts.quiet ? 0 : 2)}\n`);
  } else if (!opts.quiet) {
    for (const r of results) {
      process.stdout.write(
        r.ok
          ? `  ${String(r.outcome ?? "uploaded").padEnd(8)} ${r.file} -> evidence ${r.evidence_id ?? "(id not returned)"}\n`
          : `  FAILED   ${r.file} (HTTP ${r.status}) ${r.error}\n`
      );
    }
    process.stdout.write(
      `\n${payload.created} created, ${payload.reused} reused, ${payload.failed} failed, ${payload.skipped} skipped.\n`
    );
  }
  return failures === 0 ? 0 : 1;
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
  main(process.argv.slice(2)).then((code) => process.exit(code));
}
