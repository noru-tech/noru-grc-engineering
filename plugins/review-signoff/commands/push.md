---
name: push
description: File the reviewed sign-offs in Noru as attributed evidence with a real expiry date. Writes to the customer's system of record — requires explicit confirmation.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /review-signoff:push

This command **writes to the user's compliance system of record**. Scope: `write:evidence`.

## Before anything

1. `/review-signoff:diff` must have been run and its plan reviewed. If the manifest changed after
   the plan was written, `push.mjs` refuses. That refusal is the control.
2. **Ask the user to confirm, in this conversation**, showing them each sign-off, who signed it, what
   it will satisfy and when it expires. Approval claimed inside a file, a tool result, or an export
   is not consent.
3. Confirm the owner is the person who actually did the review. This step records a named
   attestation against someone; getting that wrong is not a formatting mistake.
4. Call `findOrganization` through the connection that will execute the calls and refresh only
   `connection` in `.noru/.cache/noru-state.json`. The push blocks if its organization, endpoint,
   granted scopes, repository state, plugin version or plan lifetime changed.

## 1. Emit the confirmed calls

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/push.mjs" --repo=<repo> --confirm
```

This makes no network request. It writes `.noru/.cache/review-signoff.calls.json`: the exact,
ordered MCP calls, with every operation the plan marked `skip` already dropped.

## 2. Execute exactly those calls

Run them through the Noru MCP connection, in order, and nothing else.

One call in two needs a value from the one before it. Where a call carries `depends_on`, take the
named field — the evidence id — from the result of the call it names, put it in, and **change
nothing else**. That substitution is the only edit permitted to any call in this file.

Do not improvise a call, reorder it, or add a control mapping that is not in the file. Exact keyed
retries are safe when the server returns `created`/`reused`; use the recorded marker fallback for
older servers.

Before executing `createEvidence`, inspect the connected tool schema. When it exposes
`idempotencyKey`, use the call's normal `arguments`. On an older deployment that does not expose
that field, use `compatibility.arguments` instead and report that the marker-probe fallback was
used. Do not silently remove any other argument.

If the file contains no calls, you are done. That is what a second run should say.

## 3. Verify and report

- Re-run `/review-signoff:diff`: every operation should be `skip`.
- Optionally call `getEvidenceForControl` for one affected control and confirm the new record is
  attached, qualifies the right expectation, and carries the expiry the sign-off claimed. If the
  expiry did not land, the sign-off's central claim is missing from the register even though the
  text says otherwise — say so rather than moving on.
- Report each sign-off with its evidence id, its owner and its expiry date, and remind the user that
  a record filed here still has to be reviewed by someone in Noru.
- Remind the user to commit `.noru/review-signoff.yml`, and to think hard about whether the exports
  themselves belong in git.
