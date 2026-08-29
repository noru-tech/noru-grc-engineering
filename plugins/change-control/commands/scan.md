---
name: scan
description: Export the forge's change history and branch protection, compute which separations did not hold, and write a reviewable .noru/change-control.yml. Writes nothing to Noru.
argument-hint: "[window, e.g. 2026-07-01..2026-09-30]"
---

# /change-control:scan

Account for one window: who wrote each change, who approved it, who merged it, who deployed it —
and every separation that did not hold. Nothing is written to Noru by this command. Read scopes
only: `read:organization`, `read:controls`, `read:evidence`, `read:risks`.

## 1. Ask Noru what is due

**This piece never ships its own opinion of what a control needs.** Which change-management
separations this organization must hold to is Noru's answer, not this plugin's. Call
`getOrganizationControls`, then `getControlContext` for the change-management controls it returns,
then `getEvidenceForControl` to see whether last period's record has expired. Write what comes back
into `queue_snapshot` — the control ids must be the lowercase canonical ones Noru returns, never the
uppercase display ids.

If you find yourself typing a control id the queue did not offer, stop: the validator rejects it,
and it is right to.

## 2. Export from the forge

This step needs a **forge** token and is the only part that touches a network. Run it yourself; it
reads the token from the environment at the point of use and never stores it.

```bash
GITHUB_TOKEN=… node "${CLAUDE_PLUGIN_ROOT}/scripts/export/github.mjs" \
  --repo=<owner/name> --since=<YYYY-MM-DD> --until=<YYYY-MM-DD>

GITLAB_TOKEN=… node "${CLAUDE_PLUGIN_ROOT}/scripts/export/gitlab.mjs" \
  --project=<group/name> --since=<YYYY-MM-DD> --until=<YYYY-MM-DD>
```

Both write `.noru/.cache/change-events.json`. Read the exporter's output: if it says
`window.complete` is false the listing was truncated, and absence of a change is not evidence it did
not happen. Say so in the rationale rather than letting a round number imply completeness.

## 3. Collect

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/collect.mjs" --repo=<repo> --output=json
```

Offline and deterministic. It computes the separations by comparing names and writes each one into
the skeleton as an exception with `needs_review: true` and no disposition.

## 4. The part only a person can do

The collector proposes; you do not get to skip the deciding. For each exception:

- **`disposition`** — `remediated` (and say when, in `resolved_on`), `accepted_risk`,
  `false_positive`, or `deferred`. A `deferred` exception shortens the record's whole horizon to
  120 days, which is deliberate.
- **`owner`** — a person. An exception nobody owns will still be there next quarter.
- **`note`** — what happened and why it was allowed to stand. This is the sentence an auditor reads.

Two things to get right, because the exporter cannot:

- **`agent_operator`** on any change the exporter marked `author_kind: agent`. The forge knows a bot
  opened the pull request; nothing in its API knows who ran it. Name them. An agent-authored change
  approved only by the person who ran it is one human wearing two hats, and that is the rule this
  piece exists to make visible.
- **`bypass.reason`** where the exporter guessed `admin_merge` from the shape — a merge with no
  approving review. It recorded the shape, not the cause. Replace it with what actually happened.

Then fill `control_mappings` from the queue snapshot, and the `interpretation` block on every change
and on `controls`. `expires_at` is required and measured from the end of the window, at most 400
days — 120 where anything is deferred.

## 5. Commit it

`.noru/change-control.yml` is the reviewable artifact. Keep `.noru/.cache/` out of git and think
hard about the export: it is a list of who reviewed what and when, which is personal data about
colleagues.

Do not edit the manifest to make a check pass. The manifest records what happened; if a separation
did not hold, say so and own it.
