#!/usr/bin/env node
// Export GitHub change history and branch protection into the forge-neutral file the collector
// reads. This is the CREDENTIALED half of the change-control piece, and it is deliberately not the
// collector: contract requirement 2 says a collector opens no socket, and who approved a pull
// request is not a fact any file in the repository contains.
//
//   export (this file, needs a token)  ->  .noru/.cache/change-events.json
//   collect (offline, deterministic)   ->  .noru/change-control.yml
//
// The token is read from the environment at the point of use and is never written, logged or
// echoed — the same arrangement evidence-push uses for NORU_API_KEY. Nothing here writes to
// GitHub; every call is a GET.
//
// Least privilege: a fine-grained token with Pull requests: read, Contents: read, Administration:
// read (for branch protection), Environments: read and Deployments: read. Nothing else.
//
// Usage:
//   node github.mjs --repo=<owner/name> --since=<YYYY-MM-DD> --until=<YYYY-MM-DD>
//                   [--out=<path>] [--api=<url>] [--output=json|text] [--quiet]
// Exit codes: 0 written, 1 the API refused or returned something unusable, 2 usage error.

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

const USAGE =
  "usage: github.mjs --repo=<owner/name> --since=<YYYY-MM-DD> --until=<YYYY-MM-DD>\n" +
  "                  [--out=<path>] [--api=<url>] [--output=json|text] [--quiet]\n";
const DEFAULT_API = "https://api.github.com";
const OUT_REL = ".noru/.cache/change-events.json";
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function parseArgs(argv) {
  const opts = {
    repo: null, since: null, until: null, out: null,
    api: DEFAULT_API, json: false, quiet: false,
  };
  for (const arg of argv) {
    if (arg.startsWith("--repo=")) opts.repo = arg.slice(7);
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

/** Redaction mirrors plugins/noru/scripts/lib/plan.mjs: an error body can echo a header back. */
function redact(text) {
  return String(text)
    .replace(/\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}/gi, "$1<redacted>")
    .replace(/\bgh[pousr]_[A-Za-z0-9]{20,}/g, "<redacted>");
}

async function api(opts, token, path, tolerate = []) {
  const url = path.startsWith("http") ? path : `${opts.api}${path}`;
  const response = await fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "noru-grc-engineering/change-control",
    },
  });
  // 404 is "there is nothing here"; 403 is "you may not ask". Both are answers rather than
  // failures for an OPTIONAL read, and a caller says which it can live without by passing them in
  // `tolerate`. A mandatory read still throws on either, because an export missing the pull
  // requests is not an export.
  if (response.status === 404 || tolerate.includes(response.status)) {
    return { missing: true, forbidden: response.status === 403, status: response.status, body: null, link: null };
  }
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(`GET ${redact(url)} -> ${response.status} ${redact(body.slice(0, 300))}`);
  }
  return {
    missing: false,
    forbidden: false,
    status: response.status,
    body: await response.json(),
    link: response.headers.get("link"),
  };
}

/** Follow RFC 5988 `rel="next"` rather than counting pages, so a partial export is impossible. */
async function paged(opts, token, path, cap = 20) {
  const out = [];
  let next = path;
  for (let page = 0; next && page < cap; page += 1) {
    const result = await api(opts, token, next);
    if (result.missing) break;
    out.push(...(Array.isArray(result.body) ? result.body : []));
    const match = /<([^>]+)>;\s*rel="next"/.exec(result.link ?? "");
    next = match ? match[1] : null;
  }
  return { rows: out, truncated: Boolean(next) };
}

function day(value) {
  return typeof value === "string" && value.length >= 10 ? value.slice(0, 10) : null;
}

function actorOf(user) {
  // An email is what a person is called in every other piece here, and GitHub does not return one
  // on a review. The login is the stable identity the forge actually has, so it is what is
  // recorded; a manifest may replace it with an email as long as the same person keeps one name.
  return user?.login ? `${user.login}@users.noreply.github.com` : null;
}

function isAgent(user) {
  // GitHub types an app's account as "Bot", and app logins carry the [bot] suffix. Neither says
  // *who ran it* — nothing in the API does — so `agent_operator` is deliberately left out and the
  // validator refuses the manifest until a human names them. Forcing the question is the point.
  return user?.type === "Bot" || /\[bot\]$/.test(user?.login ?? "");
}

/**
 * `readable` is not decoration. GitHub answers 404 for a branch that is not protected AND for a
 * token that may not ask, and the two are indistinguishable from the response. Mapping both to
 * `protected: false` states something false — "this branch has no protection" — where the honest
 * answer is "nobody here could find out". So an unreadable probe omits the fields entirely rather
 * than guessing at them, and the validator treats an absent field as unknown while it warns on an
 * explicit `false`.
 */
export function normalizeProtection(repo, protection, readable, codeowners, environments) {
  const base = {
    default_branch: repo?.default_branch ?? "unknown",
    codeowners_present: codeowners,
    deploy_environments: environments,
  };
  if (!readable) return base;

  const reviews = protection?.required_pull_request_reviews;
  return {
    ...base,
    protected: Boolean(protection),
    required_approvals: reviews?.required_approving_review_count ?? 0,
    dismiss_stale_reviews: Boolean(reviews?.dismiss_stale_reviews),
    require_code_owner_review: Boolean(reviews?.require_code_owner_reviews),
    enforce_admins: Boolean(protection?.enforce_admins?.enabled),
    allow_force_push: Boolean(protection?.allow_force_pushes?.enabled),
    required_status_checks: protection?.required_status_checks?.contexts ?? [],
  };
}

export function normalizePull(pull, reviews, deployment) {
  const author = pull.user;
  const approvals = reviews
    .map((review) => ({
      by: actorOf(review.user),
      state:
        review.state === "APPROVED" ? "approved"
        : review.state === "CHANGES_REQUESTED" ? "changes_requested"
        : review.state === "DISMISSED" ? "dismissed"
        : "commented",
      on: day(review.submitted_at),
    }))
    .filter((review) => review.by);

  // GitHub does not report "this was an admin merge" directly. What it does report is a merge with
  // no approving review on a branch that required one, which is the observable shape of it. The
  // exporter records the shape and says so; it does not claim to know the cause.
  const approvedBy = approvals.filter((a) => a.state === "approved");
  const bypass =
    approvedBy.length === 0 && pull.merged_at
      ? {
          used: true,
          kind: "admin_merge",
          by: actorOf(pull.merged_by),
          reason:
            "MERGED WITH NO APPROVING REVIEW. The exporter records the shape, not the cause — " +
            "replace this with what actually happened before signing.",
        }
      : { used: false };

  return {
    key: `pr-${pull.number}`,
    kind: "pull_request",
    title: pull.title,
    authored_by: actorOf(author),
    author_kind: isAgent(author) ? "agent" : "human",
    opened_on: day(pull.created_at),
    approvals,
    merged_by: actorOf(pull.merged_by),
    merged_on: day(pull.merged_at),
    ...(deployment
      ? { deployed_by: actorOf(deployment.creator), deployed_on: day(deployment.created_at) }
      : {}),
    ...(pull.merge_commit_sha ? { artifact_digest: pull.merge_commit_sha } : {}),
    bypass,
    url: pull.html_url,
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
  if (!opts.repo || !/^[^/]+\/[^/]+$/.test(opts.repo)) {
    process.stderr.write(`error: --repo=<owner/name> is required\n${USAGE}`);
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

  const token = process.env.GITHUB_TOKEN;
  if (!token) {
    process.stderr.write(
      "error: GITHUB_TOKEN is not set. This exporter reads it from the environment at the point " +
        "of use and never stores it; the collector that consumes its output needs no token at all.\n",
    );
    return 1;
  }

  const out = opts.out ?? join(process.cwd(), OUT_REL);
  try {
    const repo = (await api(opts, token, `/repos/${opts.repo}`)).body;
    const branch = repo?.default_branch ?? "main";

    // The Actions token cannot read branch protection — that needs Administration: read — and
    // GitHub answers 403 rather than 404 for it. Tolerating both is what stops a missing optional
    // permission killing an export that is otherwise complete.
    const protectionProbe = await api(
      opts, token, `/repos/${opts.repo}/branches/${branch}/protection`, [403],
    );
    const codeownersProbe = await api(
      opts, token, `/repos/${opts.repo}/contents/.github/CODEOWNERS`, [403],
    );
    const environmentsBody = (await api(opts, token, `/repos/${opts.repo}/environments`, [403])).body;
    const environments = (environmentsBody?.environments ?? []).map((environment) => {
      const reviewers = (environment.protection_rules ?? []).find((r) => r.type === "required_reviewers");
      return {
        name: environment.name,
        required_reviewers: reviewers?.reviewers?.length ?? 0,
        prevent_self_review: Boolean(reviewers?.prevent_self_review),
      };
    });

    const { rows: pulls, truncated } = await paged(
      opts,
      token,
      `/repos/${opts.repo}/pulls?state=closed&base=${encodeURIComponent(branch)}` +
        "&sort=updated&direction=desc&per_page=100",
    );
    const merged = pulls.filter((pull) => {
      const on = day(pull.merged_at);
      return on && on >= opts.since && on <= opts.until;
    });

    const deployments = (
      await paged(opts, token, `/repos/${opts.repo}/deployments?per_page=100`, 5)
    ).rows;
    const deploymentBySha = new Map();
    for (const deployment of deployments) {
      if (!deploymentBySha.has(deployment.sha)) deploymentBySha.set(deployment.sha, deployment);
    }

    const changes = [];
    for (const pull of merged) {
      const reviews = (
        await paged(opts, token, `/repos/${opts.repo}/pulls/${pull.number}/reviews?per_page=100`, 5)
      ).rows;
      changes.push(normalizePull(pull, reviews, deploymentBySha.get(pull.merge_commit_sha)));
    }
    changes.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));

    const document = {
      _comment:
        "Written by plugins/change-control/scripts/export/github.mjs. Read by the offline " +
        "collector; not committed. `complete: false` means the listing was truncated and absence " +
        "of a change here is not evidence it did not happen.",
      forge: "github",
      repository: opts.repo,
      exported_at: new Date().toISOString(),
      window: { opens_on: opts.since, closes_on: opts.until, complete: !truncated },
      settings: normalizeProtection(
        repo, protectionProbe.body, !protectionProbe.missing, !codeownersProbe.missing, environments,
      ),
      changes,
    };

    mkdirSync(dirname(out), { recursive: true });
    writeFileSync(out, `${JSON.stringify(document, null, 2)}\n`, "utf8");

    const summary = {
      ok: true,
      forge: "github",
      repository: opts.repo,
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
          `${changes.length} change(s) merged into ${branch} between ${opts.since} and ${opts.until}`,
          truncated ? "WARNING: the listing was truncated; window.complete is false" : "",
          protectionProbe.forbidden
            ? "NOTE: this token may not read branch protection (403). The settings are omitted " +
              "rather than guessed; grant Administration: read to record them."
            : protectionProbe.missing
              ? "NOTE: no branch protection found on " + branch + " — either it has none or the " +
                "branch does not exist. The settings are omitted rather than guessed."
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

if (import.meta.url === `file://${process.argv[1]}`) {
  process.exit(await main(process.argv.slice(2)));
}
