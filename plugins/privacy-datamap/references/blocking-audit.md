# Privacy publication gate audit

Scope: local privacy-datamap collection, investigation, review, sealing, validation,
diff and push. This is a code-history audit, not a claim about why a person approved a PR.

Blocking did not start with PR #74. Its parent already enforced interpretation ownership/expiry,
needs-review flags, current observed structures, dependency freshness, relationship validity,
discovery changes and narrowing of accepted privacy meaning in validate_manifest.py.
Review structural blockers were introduced in 44cb3a4 (#54); semantic enforcement was extended
in c63355f (#56).

PR #74 (f960f7d) added the canonical classification contract and made it mandatory at publication:

| Rule | Implementation | Audit finding / disposition |
| --- | --- | --- |
| Every field/member investigated before acceptance/export | field_review.validate_contract | Added by #74; retained. Agent completes investigation. |
| Current citations, source inventory, taxonomy and decision bindings | field_review.validate_contract / row_errors | Added by #74; retained. A review must refer to current evidence. |
| Recorded semantic dimensions, person association and taxonomy fit | field_review.row_errors | Added by #74; retained. These are plugin policy requirements, not a demonstrated external obligation. |
| Open content must be bounded or runtime-closed | field_review.row_errors, review.member_coverage | Added by #74; removed as a universal requirement. Evidence-backed reviewed_open_content records residual uncertainty without pretending values are bounded. |
| Composite categories only on known members; personal composites need categorized descendants | field_review.row_errors | Added by #74; relaxed for reviewed open content so a genuinely arbitrary payload does not require invented members. Known members still need their own decisions. |
| Unresolved uncertainty / unknown personal association cannot become proposed | field_review.apply_decisions / row_errors | Added by #74; retained for unanswered questions without acceptance. Established openness uses residual_uncertainty; investigated field and gap uncertainty may also use the general accepted_uncertainty disposition. |
| Extraction diagnostics block until resolved | field_review.validate_contract | Added by #74; subsequently generalized by 30642d3 to honor modeled/not_applicable resolutions across gates. Raw observations remain visible. |
| Complete canonical contract required to seal | field_review.classification_certificate, reconcile | Added by #74; retained with the revised content outcomes. |
| Validate actual manifest before diff/push; reject changed payload | diff.validateCurrentManifest, push | Added by #74; retained. Previously diff read the parsed cache. |

The shared content outcomes now include reviewed_open_content. It requires rationale, cited
evidence and a residual uncertainty statement. Export preserves that statement in the field
description; projection validation rejects its omission. It does not mark the content closed,
assign non-personal meaning, waive known-member investigation or supply human acceptance.

Regression coverage: test_classification_gate.py exercises reviewed open content with and without
known members, evidence/disclosure rejection, certificate creation and cache restoration, and stale
source rejection. test_gap_resolution.py covers resolution-aware structural gates;
test_privacy_delivery_gate.py covers current-manifest diff/push enforcement.

The subsequent general acceptance workflow also permits explicitly accepted uncertainty about
personal association or taxonomy fit. Required investigation dimensions and source-inventory
freshness remain enforced. See accepted-uncertainty.md and test_uncertainty.py.
This change does not silently downgrade those checks or authorize publication.
