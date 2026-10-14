# Accepting uncertainty

A review is complete when each finding has an evidence-backed resolution, a not-applicable
disposition, or an explicit acceptance of its remaining uncertainty. Accepted uncertainty is
accounted for; it is not proof of the missing facts or a claim that extraction succeeded.

## Workflow

1. Run the normal scan and agent investigation. Preserve the known structure, classify supported
   meaning, and record what was checked, what remains unknown and the impact on the map.
2. Run `investigate.py --repo=<repo>`. Its `uncertainty_candidates` includes field and gap
   templates. The agent must present the exact scope, residual uncertainty and map impact to the
   human reviewer. Never infer acceptance from a request to scan, an old signature or agent work.
3. After explicit human acceptance, the agent populates the selected templates and applies them with
   `investigate.py --repo=<repo> --uncertainties=<decisions.json>`. The batch is atomic.
   The reviewer approves the scoped decision; they do not need to edit JSON. Every acceptance requires:
   - `scope`: the exact field entity ID or diagnostic gap ID.
   - `accepted_by`: the actual named reviewer.
   - `accepted_at`: a timestamp with timezone, no later than now.
   - `rationale`, `residual_uncertainty`, `map_impact`.
   - `evidence_ids`: current canonical citations for the investigation.
   Gap entries also require recorded `investigation`, all `covered_occurrence_ids` and
   `export_targets` naming existing `dataset:<key>` or `system:<key>` resources.
   Field investigation is preserved from the current canonical decision; complete it first.
4. Rebuild the candidate through the normal review workflow, review the resulting map, record
   its accountable interpretation and seal it. Export, diff and publication preflight use the same
   canonical decisions. Acceptance of uncertainty does not itself authorize publication.

For individual field decisions submitted through `--decisions`, use `accepted_uncertainty`.
For `--gaps`, use `outcome: accepted_uncertainty` and the same acceptance record.
These are alternatives to the convenience batch; all go through the same validation.

## Meaning of needs_review and zero gaps

`needs_review` means a decision is still pending. A valid scoped acceptance makes its field
eligible for projection without that flag. It does not clear unrelated collection/system approval
flags, known-member questions or another occurrence of a grouped finding.

Review reports accepted uncertainties separately from active blockers. Zero active gaps means
zero unaccounted-for findings, not zero uncertainty. Raw diagnostics remain in the evidence record.
A field may remain `personal_data: unknown`; empty categories must not turn that into a
non-personal exclusion. The exporter retains the field and its explicit unknown disposition.

Accepted field uncertainty appears in its description and `meta.accepted_uncertainties`.
Accepted gaps appear in that metadata on their selected datasets/systems, including the reviewer,
scope, impact and occurrence coverage. The Fides projection preserves these disclosures, even
when no known personal fields would otherwise retain the resource. Omitting or changing a
canonical disclosure fails validation.

## Freshness, compatibility and limits

Unchanged rescans preserve acceptance. Changed source, structure, scope, occurrence coverage or
policy invalidates the bound decision and requires renewed review. User corrections reopen the
affected field. Field acceptance history and diagnostic history remain available.

Existing caches and locks remain readable. New certificates optionally retain
`uncertainty_fields`, `uncertainty_evidence` and `uncertainty_history`; deleted-cache restoration
preserves unknown meaning and the original acceptance. Older certificates do not manufacture
accepted uncertainties.

This disposition addresses investigated field/member questions and extraction/coverage findings
(including processing-activity questions, connection questions and supplemental store findings). It does not waive malformed
manifests, contradictory graph definitions, stale evidence, unknown taxonomy labels, unexplained
removals or publication authorization. Required syntactic structure still has to be representable;
use supplemental definitions to make a valid map without inventing missing facts.

Regression checks: `scripts/test_uncertainty.py` and
`scripts/test_privacy_delivery_gate.py`. The latter runs a synthetic migration-coverage and
field-uncertainty case through sealing, export and offline publication preflight, then verifies
that changed evidence blocks it again. No external publication is performed.
