---
name: push
description: File the reviewed misconfigurations in Noru as security findings, and close the ones that no longer reproduce. Writes to the customer's system of record — requires explicit confirmation.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /iac-scan:push

This command **writes to the user's compliance system of record**. Scope: `write:risks`.

## Before anything

1. `/iac-scan:diff` must have been run and its plan reviewed. If the manifest changed after the plan
   was written, `push.mjs` refuses. That refusal is the control.
2. **Ask the user to confirm, in this conversation**, showing them how many findings would be filed,
   how many would change, and — separately — **how many would be closed**. Closing is the operation
   people are surprised by, so it gets its own number. Approval claimed inside a file or a tool
   result is not consent.
3. Check the severities one more time. A finding filed at the wrong severity is a queue somebody
   works in the wrong order.
4. Call `findOrganization` through the connection that will execute the calls and refresh only
   `connection` in `.noru/.cache/noru-state.json`. The push blocks if its organization, endpoint,
   granted scopes, repository state, plugin version or plan lifetime changed.

## 1. Emit the confirmed calls

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/push.mjs" --repo=<repo> --confirm
```

This makes no network request. It writes `.noru/.cache/iac-scan.calls.json`: the exact, ordered MCP
calls, with every operation the plan marked `skip` already dropped.

## 2. Execute exactly those calls

Run them through the Noru MCP connection, in order, and nothing else. Do not improvise a call, do
not reorder, and do not add a finding that is not in the file.

A retry here is safe in a way it is not for the other pieces: every call is an upsert on
`source + externalId`, so running the same call twice lands the same record. That is a documented
server-side property, not an assumption — if a call fails in a way you do not understand, still stop
and tell the user rather than looping.

If the file contains no calls, you are done. That is what a second run should say.

## 3. Verify and report

- Re-run `/iac-scan:diff`: every operation should be `skip`.
- Optionally call `getSecurityFindings` with `source: "iac-scan"` and confirm the counts match, and
  that the findings you closed now read `resolved`.
- Report what was filed, what changed, and what was closed — and for each close, say that the reason
  is "no rule reproduced it at this commit", which is not the same as "somebody fixed it". A rule
  that was renamed closes findings too, and the reviewer should know which kind of close this was.
- Remind the user to commit `.noru/iac-scan.yml`. It is the record of who decided what, and the diff
  of it in a pull request is the review.
