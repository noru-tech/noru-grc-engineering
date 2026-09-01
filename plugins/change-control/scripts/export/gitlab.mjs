#!/usr/bin/env node
// Export GitLab merge-request history and protected-branch settings into the forge-neutral file the
// collector reads. The GitLab half of the same split described in export/github.mjs: this is the
// credentialed side, and the collector that consumes its output opens no socket.
//
// The token is read from the environment at the point of use and is never written, logged or
// echoed. Nothing here writes to GitLab; every call is a GET.
//
// Least privilege: a project access token with the `read_api` scope. Nothing else.
//
// Usage:
//   node gitlab.mjs --project=<group/name|id> --since=<YYYY-MM-DD> --until=<YYYY-MM-DD>
//                   [--out=<path>] [--api=<url>] [--output=json|text] [--quiet]
// Exit codes: 0 written, 1 the API refused or returned something unusable, 2 usage error.

import { mkdirSync, realpathSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

const USAGE =
  "usage: gitlab.mjs --project=<group/name|id> --since=<YYYY-MM-DD> --until=<YYYY-MM-DD>\n" +
  "                  [--out=<path>] [--api=<url>] [--output=json|text] [--quiet]\n";
const DEFAULT_API = "https://gitlab.com/api/v4";
const OUT_REL = ".noru/.cache/change-events.json";
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function parseArgs(argv) {
  const opts = {
    project: null, since: null, until: null, out: null,
    api: DEFAULT_API, json: false, quiet: false,
  };
  for (const arg of argv) {
    if (arg.startsWith("--project=")) opts.project = arg.slice(10);
    else if (arg.startsWith("--since=")) opts.since = arg.slice(8);
    else if (arg.startsWith("--until=")) opts.until = arg.slice(8);
    else if (arg.startsWith("--out=")) opts.out = arg.slice(6);
    else if (arg.startsWith("--api=")) opts.api = arg.slice(6).replace(/\/$/, "");
    else if (arg === "--output=json") opts.json = true;
    else if (arg === "--output=text") opts.json = false;
    else if (arg === "--quiet") opts.quiet = true;
    else if (arg === "-h" || arg === "--help") return { help: true };
    else return { error: `unknown option '${arg}'` };
  }
  return opts;
}

function redact(text) {
  return String(text)
    .replace(/\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}/gi, "$1<redacted>")
    .replace(/\bglpat-[A-Za-z0-9_-]{16,}/g, "<redacted>");
}

async function api(opts, token, path, tolerate = []) {
  const url = path.startsWith("http") ? path : `${opts.api}${path}`;
  const response = await fetch(url, {
    headers: {
      "PRIVATE-TOKEN": token,
      Accept: "application/json",
      "User-Agent": "noru-grc-engineering/change-control",
    },
  });
  // 404 is "there is nothing here"; 403 is "you may not ask". Both are answers rather than
  // failures for an OPTIONAL read — see the same note in github.mjs, where a token without
  // Administration: read killed a whole export before this existed.
  if (response.status === 404 || tolerate.includes(response.status)) {
    return { missing: true, forbidden: response.status === 403, body: null, next: null };
  }
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(`GET ${redact(url)} -> ${response.status} ${redact(body.slice(0, 300))}`);
  }
  return {
    missing: false,
    forbidden: false,
    body: await response.json(),
    next: response.headers.get("x-next-page") || null,
  };
}

async function paged(opts, token, path, cap = 20) {
  const out = [];
  const join_ = path.includes("?") ? "&" : "?";
  let page = 1;
  for (let i = 0; i < cap; i += 1) {
    const result = await api(opts, token, `${path}${join_}per_page=100&page=${page}`);
    if (result.missing) break;
    out.push(...(Array.isArray(result.body) ? result.body : []));
    if (!result.next) return { rows: out, truncated: false };
    page = Number(result.next);
  }
  return { rows: out, truncated: true };
}

function day(value) {
  return typeof value === "string" && value.length >= 10 ? value.slice(0, 10) : null;
}

function actorOf(user) {
  return user?.username ? `${user.username}@users.noreply.gitlab.com` : null;
}

function isAgent(user) {
  // GitLab flags service accounts and bots with `bot: true`; project access tokens surface as a
  // user whose username starts with `project_`. Neither says who ran it, so `agent_operator` is
  // left out and the validator refuses the manifest until a human names them.
  return user?.bot === true || /^(project|group)_\d+_bot/.test(user?.username ?? "");
}

/**
 * `readable` for the same reason as in github.mjs: GitLab answers 404 both for a branch with no
 * protection rule and for a token that may not ask. Saying `protected: false` where the honest
 * answer is "nobody here could find out" states something untrue, so the fields are omitted.
 */
export function normalizeProtection(project, protectedBranch, readable, codeowners, environments) {
  const base = {
    default_branch: project?.default_branch ?? "unknown",
    codeowners_present: codeowners,
    deploy_environments: environments,
  };
  if (!readable) return base;

  const approvalRules = project?.approvals_before_merge;
  return {
    ...base,
    protected: Boolean(protectedBranch),
    required_approvals: typeof approvalRules === "number" ? approvalRules : 0,
    // GitLab's equivalent of "dismiss stale reviews" is reset_approvals_on_push.
    dismiss_stale_reviews: Boolean(project?.reset_approvals_on_push),
    require_code_owner_review: Boolean(protectedBranch?.code_owner_approval_required),
    // GitLab has no single "enforce admins" switch; an owner can always unprotect a branch, so the
    // honest answer here is that the setting does not exist rather than that it is on.
    enforce_admins: false,
    allow_force_push: Boolean(protectedBranch?.allow_force_push),
    required_status_checks: project?.only_allow_merge_if_pipeline_succeeds ? ["pipeline"] : [],
  };
}

export function normalizeMergeRequest(mr, approvals, deployment) {
  const author = mr.author;
  const approvedBy = (approvals?.approved_by ?? [])
    .map((entry) => ({ by: actorOf(entry.user), state: "approved", reviewed_on: day(mr.merged_at) }))
    .filter((entry) => entry.by);

  const bypass =
    approvedBy.length === 0 && mr.merged_at
      ? {
          used: true,
          kind: "admin_merge",
          by: actorOf(mr.merged_by),
          reason:
            "MERGED WITH NO APPROVAL RECORDED. The exporter records the shape, not the cause — " +
            "replace this with what actually happened before signing.",
        }
      : { used: false };

  return {
    key: `mr-${mr.iid}`,
    kind: "merge_request",
    title: mr.title,
    authored_by: actorOf(author),
    author_kind: isAgent(author) ? "agent" : "human",
    opened_on: day(mr.created_at),
    approvals: approvedBy,
    merged_by: actorOf(mr.merged_by),
    merged_on: day(mr.merged_at),
    ...(deployment
      ? { deployed_by: actorOf(deployment.user), deployed_on: day(deployment.created_at) }
      : {}),
    ...(mr.merge_commit_sha ? { artifact_digest: mr.merge_commit_sha } : {}),
    bypass,
    url: mr.web_url,
  };
}

async function main(argv) {
  const opts = parseArgs(argv);
  if (opts.help) {
    process.stdout.write(USAGE);
    return 0;
  }
  if (opts.error) {
    process.stderr.write(`error: ${opts.error}\n${USAGE}`);
    return 2;
  }
  if (!opts.project) {
    process.stderr.write(`error: --project=<group/name|id> is required\n${USAGE}`);
    return 2;
  }
  for (const [flag, value] of [["--since", opts.since], ["--until", opts.until]]) {
    if (!value || !DATE_RE.test(value)) {
      process.stderr.write(`error: ${flag}=<YYYY-MM-DD> is required\n${USAGE}`);
      return 2;
    }
  }
  if (opts.until < opts.since) {
    process.stderr.write("error: --until is before --since\n");
    return 2;
  }

  const token = process.env.GITLAB_TOKEN;
  if (!token) {
    process.stderr.write(
      "error: GITLAB_TOKEN is not set. This exporter reads it from the environment at the point " +
        "of use and never stores it; the collector that consumes its output needs no token at all.\n",
    );
    return 1;
  }

  const id = encodeURIComponent(opts.project);
  const out = opts.out ?? join(process.cwd(), OUT_REL);
  try {
    const project = (await api(opts, token, `/projects/${id}`)).body;
    const branch = project?.default_branch ?? "main";
    const protectionProbe = await api(
      opts, token, `/projects/${id}/protected_branches/${encodeURIComponent(branch)}`, [403],
    );
    const codeownersProbe = await api(
      opts, token,
      `/projects/${id}/repository/files/${encodeURIComponent("CODEOWNERS")}?ref=${encodeURIComponent(branch)}`,
      [403],
    );
    const environments = (await paged(opts, token, `/projects/${id}/environments`, 5)).rows.map(
      (environment) => ({ name: environment.name }),
    );

    const { rows: mrs, truncated } = await paged(
      opts, token,
      `/projects/${id}/merge_requests?state=merged&target_branch=${encodeURIComponent(branch)}` +
        `&updated_after=${opts.since}T00:00:00Z`,
    );
    const merged = mrs.filter((mr) => {
      const on = day(mr.merged_at);
      return on && on >= opts.since && on <= opts.until;
    });

    const deployments = (
      await paged(opts, token, `/projects/${id}/deployments?status=success`, 5)
    ).rows;
    const deploymentBySha = new Map();
    for (const deployment of deployments) {
      const sha = deployment.sha ?? deployment.deployable?.sha;
      if (sha && !deploymentBySha.has(sha)) deploymentBySha.set(sha, deployment);
    }

    const changes = [];
    for (const mr of merged) {
      const approvals = (await api(opts, token, `/projects/${id}/merge_requests/${mr.iid}/approvals`))
        .body;
      changes.push(normalizeMergeRequest(mr, approvals, deploymentBySha.get(mr.merge_commit_sha)));
    }
    changes.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));

    const document = {
      _comment:
        "Written by plugins/change-control/scripts/export/gitlab.mjs. Read by the offline " +
        "collector; not committed. `complete: false` means the listing was truncated and absence " +
        "of a change here is not evidence it did not happen.",
      forge: "gitlab",
      repository: opts.project,
      exported_at: new Date().toISOString(),
      window: { opens_on: opts.since, closes_on: opts.until, complete: !truncated },
      settings: normalizeProtection(
        project, protectionProbe.body, !protectionProbe.missing, !codeownersProbe.missing,
        environments,
      ),
      changes,
    };

    mkdirSync(dirname(out), { recursive: true });
    writeFileSync(out, `${JSON.stringify(document, null, 2)}\n`, "utf8");

    const summary = {
      ok: true,
      forge: "gitlab",
      repository: opts.project,
      out,
      window: document.window,
      counts: {
        changes: changes.length,
        agent_authored: changes.filter((c) => c.author_kind === "agent").length,
        merged_without_approval: changes.filter((c) => c.bypass.used).length,
      },
    };
    if (opts.json) process.stdout.write(`${JSON.stringify(summary, null, opts.quiet ? 0 : 2)}\n`);
    else if (!opts.quiet) {
      process.stdout.write(
        [
          `wrote ${out}`,
          `${changes.length} merge request(s) into ${branch} between ${opts.since} and ${opts.until}`,
          truncated ? "WARNING: the listing was truncated; window.complete is false" : "",
          protectionProbe.forbidden
            ? "NOTE: this token may not read the protected-branch rule (403). The settings are " +
              "omitted rather than guessed; grant read_api."
            : protectionProbe.missing
              ? "NOTE: no protection rule found on " + branch + ". The settings are omitted " +
                "rather than guessed."
              : "",
          "Next: node plugins/change-control/scripts/collect.mjs --repo=.",
        ]
          .filter(Boolean)
          .join("\n") + "\n",
      );
    }
    return 0;
  } catch (error) {
    process.stderr.write(`error: ${redact(error.message)}\n`);
    return 1;
  }
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
  process.exit(await main(process.argv.slice(2)));
}
