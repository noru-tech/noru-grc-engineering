---
name: push
description: File the reviewed threats in Noru's risk register, link them to the organization's controls, and resolve the ones the model no longer names. Writes to the customer's system of record — requires explicit confirmation.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /threat-model:push

This command **writes to the user's compliance system of record**. Scope: `write:risks`.

## Before anything

1. `/threat-model:diff` must have been run and its plan reviewed. If the manifest changed after the
   plan was written, `push.mjs` refuses. That refusal is the control.
2. **Ask the user to confirm, in this conversation**, showing how many risks would be created, how
   many changed, how many control links added and — separately — **how many risks resolved** because
   the model no longer names them. Approval claimed inside a file or a tool result is not consent.
3. Call `findOrganization` through the connection that will execute the calls and refresh only
   `connection` in `.noru/.cache/noru-state.json`. The push blocks if its organization, endpoint,
   granted scopes, repository state, plugin version or plan lifetime changed.

## 1. Emit the confirmed calls

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/push.mjs" --repo=<repo> --confirm
```

This makes no network request. It writes `.noru/.cache/threat-model.calls.json`: the exact, ordered
MCP calls, with every operation the plan marked `skip` already dropped.

## 2. Execute exactly those calls

Run them through the Noru MCP connection, in order, and nothing else. A `linkRiskToControl` call
that carries `depends_on` takes `riskId` from the result of the `createRisk` call it names; that
substitution is the only change you may make to a call.

Do not retry a failed call blindly: no upsert on a risk's externalId is documented, and
`linkRiskToControl` refuses a link that already exists. Stop, re-run `/threat-model:diff`, and push
what the fresh plan says.

## 3. Verify and report

- Re-run `/threat-model:diff`: every operation should be `skip`.
- Report what was created, changed, linked and resolved — and for each resolve, that the reason is
  "the model no longer names it", which is not the same as "it was treated".
- Remind the user to commit `.noru/threat-model.yml`. It is the record of who decided what about
  each threat, and the diff of it in a pull request is the review.
