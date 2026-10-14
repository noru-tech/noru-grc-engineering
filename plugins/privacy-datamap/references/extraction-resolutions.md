# Investigating extraction diagnostics

Extraction observations and decisions are separate. `coverage` in derived facts remains the raw
collector record. The canonical analysis stores each diagnostic, its resolution, source evidence
and earlier dispositions. `extraction_diagnostics` in reconciliation and review partitions these
into `observed_diagnostics`, `resolved_diagnostics` and `active_blockers`. Review, validation,
sealing, export and publication preflight use the shared resolution calculation. CI consumes the
same partition from the offline reconciler. Classification and human acceptance remain separate.

## Investigation loop

1. Collect and reconcile current source; run `field_review.py --repo=<repo>`.
2. Run `investigate.py --repo=<repo>`. Its extraction queue includes a decision template and the
   exact `covered_occurrence_ids` to investigate. Read every occurrence and its supporting context.
3. When extraction missed structure, add evidence-backed current definitions using the existing
   supplemental dataset/candidate workflow, or improve the extractor. Recollect and reconcile the
   candidate before referencing its canonical field IDs. A resolution cannot create missing fields.
4. Submit a JSON list with `investigate.py --repo=<repo> --gaps=<decisions.json>`.
5. Run review and validation. Complete classification, processing and human acceptance separately,
   then use the existing seal and export commands. Publication still requires its own request.

A decision identifies `gap_id`, `outcome`, `rationale`, attempted `investigation` and current
`evidence_ids` (or `additional_evidence` source spans). The plugin records provenance and a binding
covering the observation, occurrence set, evidence and represented structures.

- `modeled`: reference evidenced canonical `field_ids`. Definitions must cover the affected
  datastore and any enumerated required fields, including supplemental definitions when needed.
  Open content still needs its supported content-contract investigation. A structurally modeled
  field can still need privacy classification; resolving extraction does not accept its meaning.
- `not_applicable`: supply `scope_rationale` explaining, from cited current evidence, why the
  finding cannot change the represented data. Approval, a preferred outcome or an empty map is
  not evidence. The deterministic gate verifies recorded evidence and accounting, not prose truth.
- `unresolved`: record what was inspected and what remains missing. It remains a blocker.

For grouped diagnostics, submit every `covered_occurrence_ids` value. Partial coverage cannot
resolve the group. Conflicting observations sharing an ID remain blocking. Identical duplicate
observations receive one atomic disposition. Single-occurrence legacy submissions may omit the
occurrence list; the API records the one current occurrence explicitly.

Unchanged rescans retain valid dispositions. Changed occurrences, scope, evidence or represented
structure reopen them and retain the previous rationale in `resolution_history`. A disappeared
diagnostic moves to `diagnostic_history`; it is not silently erased. Rebuild current evidence before
reinvestigating a stale review. The existing conservative source-inventory and classification
freshness checks still apply; this change does not claim selective semantic invalidation.

## Migrations: identity is not completeness

The common `coverage.migration_coverage` contract reports `identity_status` separately from
`schema_status`, together with current structures, datastore bindings and evidence fingerprints.
Directory proximity and overlapping table names never establish coverage. Historical DDL and
migration operations never supply current fields.

The EF adapter reads bounded static `DbContext(typeof(...))` metadata, joins explicitly matching
partial migration declarations, and resolves context symbols through source namespaces and explicit
project references. Shared models and multiple contexts stay distinct. Ambiguous or unavailable
context identity remains unresolved. The Drizzle adapter uses the existing explicit configuration
link between schema paths and migration output. Other layouts require an evidenced investigation
or a new adapter; no repository-specific matching rules are used.

Even a unique identity match requires nonempty current model definitions and no known relevant
extraction limitations before automatic coverage is reported. Logical datastore identity does not
verify a deployed database. Migration metadata is fingerprinted separately from classification
source: it may establish a link, but cannot become field-classification evidence.

A modeled migration resolution additionally requires `identity_rationale` and `coverage_rationale`.
Cite current schema/producer/configuration evidence for both. Supply `coverage_binding` with the
`datastore`, covered `migration_paths`, complete target `field_ids` and cited binding `evidence_ids`. Supplemental fields must belong to
the affected datastore and cover required structure; an unrelated schema cannot satisfy the gap.
If binding is unknown, investigate it and record the supporting evidence rather than treating
identity matching alone as a completeness claim.

## Cache and lock compatibility

The canonical review remains version 2 with optional `resolution_contract_version: 1` and history.
Existing manifests are unchanged. Legacy caches remain readable; old unbound gap dispositions
reopen during the next evidence rebuild instead of being silently accepted. Existing field decisions
are carried under the existing source, observation and policy freshness rules.

New sealed certificates include coverage fingerprints, audited gap resolutions and their evidence.
An absent cache can use those dispositions only while the accepted source, structure, policy and
manifest certificate remain current. A present unfinished review takes precedence. Old certificates
without audited coverage cannot waive observed diagnostics; recollect, investigate and reseal.
No acceptance flags are cleared during this upgrade.

## When investigation is insufficient

Keep a blocker when current source cannot establish a model, dynamic/custom conventions or converters
change unknown structure, project/context identity is ambiguous, a supplemental definition is
incomplete, or current evidence is unavailable. Add a generic tested extractor/adapter when these
patterns recur. Human approval cannot substitute for the missing evidence.

The automated demonstration in `scripts/test_privacy_delivery_gate.py` creates an isolated synthetic
repository, scans an unlinked migration, records a modeled resolution through the investigation CLI,
validates, seals, exports and runs publication preflight. It then changes evidence and verifies that
acceptance/export/preflight block again. No repository-specific plugin edits or Noru calls are used.

## Enrichment findings and known open records

Unresolved `store_investigation.findings` enter the same blocker partition as collector diagnostics.
They remain agent-owned technical work, including when the collector reports no diagnostics.
Rebuild the canonical contract after recording a finding, then investigate its gap decision normally.
A present enrichment gap cannot be bypassed by an older sealed certificate.

A modeled resolution may account for a known open record without accepting its meaning. Supply
`open_structure`, keyed by canonical field ID, with `additional_entries: true`, the extracted
`key_type` and `value_type`, a rationale, and cited resolution `evidence_ids`. The type must already
be an evidenced record with those types. Unknown object shapes do not qualify. This closes only
the structural diagnostic: field classification, open-content semantic review and human acceptance
remain mandatory. An evidenced `reviewed_open_content` assessment can complete content review
without bounding arbitrary values; its `residual_uncertainty` is retained in export. Neither arbitrary values nor incoming baggage become non-personal by default.

For a runtime-only finding, a modeled resolution can supply `system_ids` and `runtime_rationale`
instead of inventing a dataset. Cite the runtime definition's evidence and explain how its flows
cover the finding. `required_system_ids` on a finding must all be covered. Definitions and flow
changes reopen the resolution, including when restoring a sealed certificate. A runtime alone
cannot resolve a datastore-scoped finding; runtime targets require an explicit runtime scope or
`required_system_ids`. Older caches remain readable; rebuild the canonical
contract to obtain runtime definitions before recording this kind of resolution.

## Agent fallback for collector limitations

`investigate.py` counts active gaps alongside pending semantic fields and gives every gap an
agent-owned fallback task. A zero-length field batch does not mean investigation is finished.
Use the existing supplemental dataset/system/relationship or shape workflows when automatic
extraction falls short. Investigation always remains the agent's responsibility. Recollect the candidate and resolve
against current definitions and evidence; approval and classification remain separate.

An unresolved decision may record `fallback_review` using the supplied template. The API validates
its inspected-source account, cited evidence, supplemental attempt, exact missing fact, required
evidence, next evidence-gathering action and decision impact. Current records use
`evidence_unavailable`; implementation limitations may be documented but are not scan outcomes.
Missing or stale records, including legacy records labeled `representation_limitation`, remain
readable and return to `investigation_required`. New submissions must include the next action.
Neither state clears a blocker or substitutes for acceptance.
The record proves that an attempt was documented, not that prose is true or all evidence exhausted.
Never delegate investigation to the plugin or halt evidence gathering for development work.
Continue source investigation, relationship tracing and supplemental modeling; report the exact
missing evidence and its map impact when facts remain unavailable. Continue independent tasks.

## Accepted uncertainty

An investigated finding can use `accepted_uncertainty` as an explicit human disposition.
It remains visible but is excluded from active blockers while its evidence is current. See
[accepted-uncertainty.md](accepted-uncertainty.md) for scoped records, export disclosures and the
convenience command. This does not claim the absent structure was extracted.
