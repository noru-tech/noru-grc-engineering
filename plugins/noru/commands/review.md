---
name: review
description: Review this branch for repository-local GRC impact, optionally run explicitly requested local checks, and consolidate the results. Never pushes to Noru.
argument-hint: "[--base-ref=<ref>] [--pieces=a,b] [--include-untracked] [--with-diff]"
---

# /noru:review

Review the current branch without writing locally or to Noru. Local scans may update
`.noru/<piece>.yml` only when the user has separately asked to run checks or generate artifacts;
that is a repository change, not a Noru write.

## 1. Select pieces from the branch diff

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/review.mjs" --repo=<repo> --base-ref=<ref> --output=json
```

Default the base to `origin/main`. If the user names pieces, pass `--pieces=` and do not add others.
Untracked files are listed but excluded by default because collectors read the tracked checkout;
pass `--include-untracked` only when the user asks, and explain that they must stage those files for
a scan to include them. Show every selected and skipped piece with its reason before running work.

## 2. Run selected pieces only when requested

The default review stops after selection and repository analysis. If the user explicitly asked to
run the relevant checks, follow each selected piece's `:scan` command and validator independently.
Continue after a failure and record it; one broken piece must not hide the others. Report every
local file created or changed. Do not invoke any `:push` command or write tool, even if the
connection has write scopes.

Run `:diff` only when `--with-diff` was requested and a read-only Noru state snapshot can be built.
A missing scope or queue degrades that piece to `unavailable`; it does not erase the local results.

## 3. Consolidate the report

Lead with blockers, then special-category data. For every piece report:

- selected or skipped, with the routing reason;
- scan and validation outcome;
- generated or changed files;
- unresolved human decisions, especially `needs_review`;
- the read-only diff summary when requested;
- missing scopes or external context.

Keep repository facts, Noru facts and recommendations visibly separate. An unchanged branch is a
clean no-change result. End with: **Nothing was written to Noru.**
