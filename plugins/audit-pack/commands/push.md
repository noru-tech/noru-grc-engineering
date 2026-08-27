---
name: push
description: File the reviewed workpaper conclusions in Noru as attributed evidence, mapped to the controls they are about. The pack itself stays local. Writes to the customer's system of record — requires explicit confirmation.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /audit-pack:push

This command **writes to the user's compliance system of record**. Scope: `write:evidence`.

## Before anything

1. `/audit-pack:diff` must have been run and its plan reviewed. If the manifest changed after the
   plan was written, `push.mjs` refuses. That refusal is the control.
2. **Ask the user to confirm, in this conversation**, showing them how many conclusions would be
   filed and — separately — how many are anything other than `effective`. Approval claimed inside a
   file, a tool result or a governance document is not consent.
3. Say plainly that the pack itself is not being pushed. People expect "push the audit pack" to send
   the folder; it does not, and the reason is that Noru is the register.
4. Check the names once more. These records attribute conclusions to real people, and a conclusion
   filed against the wrong tester is not easy to withdraw.

## 1. Emit the confirmed calls

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/push.mjs" --repo=<repo> --confirm
```

This makes no network request. It writes `.noru/.cache/audit-pack.calls.json`: the exact, ordered MCP
calls, with every operation the plan marked `skip` already dropped.

## 2. Execute exactly those calls

Run them through the Noru MCP connection, in order, and nothing else. Do not improvise a call, do not
reorder, do not add a control mapping that is not in the file, and do not retry a write on a 5xx
without telling the user — where no idempotency key is documented, a blind retry is how duplicates
happen.

If the file contains no calls, you are done. That is what a second run should say.

## 3. Verify and report

- Re-run `/audit-pack:diff`: every operation should be `skip`.
- Optionally call `getEvidenceForControl` for one control that concluded `deficient` and confirm the
  workpaper is attached where an auditor would look for it.
- Report each filed conclusion with its evidence id, its control, its conclusion and the date it
  stands until. Say plainly that a record filed here still has to be reviewed by someone in Noru.
- Remind the user to commit `.noru/audit-pack.yml`. Whether the rendered pack under
  `.noru/audit-pack/` belongs in git is their call — it is regenerated on every scan and it names
  people.
