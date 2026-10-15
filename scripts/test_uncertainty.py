#!/usr/bin/env python3
"""Explicit uncertainty acceptance across investigation, review, certificates and export."""
import copy
import json
import subprocess
import unittest

import test_classification_gate as fixtures
import test_gap_resolution as gaps
import field_review
import gap_resolution
import review
import investigate
import uncertainty


class UncertaintyTests(unittest.TestCase):
    setUp = fixtures.ClassificationGateTests.setUp
    row = fixtures.ClassificationGateTests.row
    decision = fixtures.ClassificationGateTests.decision
    resolve = fixtures.ClassificationGateTests.resolve
    accepted_fixture = fixtures.ClassificationGateTests.accepted_fixture
    gap = gaps.ResolutionTests.gap
    answer = gaps.ResolutionTests.answer

    def acceptance(self, scope, evidence):
        return dict(uncertainty.template(scope), accepted_by='Synthetic Reviewer',
                    accepted_at='2026-01-01T00:00:00Z', evidence_ids=evidence,
                    rationale='Reviewed the investigation and explicitly accepted this remaining uncertainty.',
                    residual_uncertainty='The external producer contract is unavailable.',
                    map_impact='Additional personal values may exist; the represented scope is incomplete.')

    def unknown(self):
        self.resolve()
        decision = self.decision('email', 'unknown', [], 'unknown')
        decision['uncertainty'] = 'The source does not establish whether this value belongs to a person.'
        decision['resolution_owner'] = 'agent'
        self.assertEqual(field_review.apply_decisions(self.document, [decision], self.repo), [])
        return decision

    def export(self, manifest):
        module = (fixtures.ROOT / 'plugins/privacy-datamap/scripts/lib/fides.mjs').as_uri()
        code = "import {toFideslang} from " + json.dumps(module) + "; let s=''; for await (const c of process.stdin) s+=c; console.log(JSON.stringify(toFideslang(JSON.parse(s))));"
        return json.loads(subprocess.run(['node', '--input-type=module', '-e', code],
                                        input=json.dumps(manifest), text=True, capture_output=True, check=True).stdout)

    def test_unknown_field_acceptance_preserves_unknown_through_export_and_restore(self):
        self.unknown()
        row = self.row('email')
        accepted = self.acceptance(row['entity_id'], row['evidence_ids'])
        self.document = investigate.apply_uncertainties(self.document, [
            {'kind': 'field', 'target_id': row['entity_id'], 'acceptance': accepted}], self.repo)
        self.assertEqual(self.row('email')['personal_data'], 'unknown')
        self.assertEqual(self.row('email')['status'], 'proposed')
        manifest = self.accepted_fixture(resolve=False)
        field = manifest['dataset'][0]['collections'][0]['fields'][0]
        self.assertNotIn('needs_review', field)
        wire = self.export(manifest)
        wire_field = wire['dataset'][0]['collections'][0]['fields'][0]
        self.assertEqual(wire_field['meta']['accepted_uncertainties'][0]['personal_data'], 'unknown')
        self.assertIn('Accepted uncertainty:', wire_field['description'])
        progress = review.scan_progress(self.document, {'accepted_current': True}, self.derived, [], [], self.repo)
        self.assertEqual(progress['accepted_uncertainty_count'], 1)
        self.assertEqual(progress['publication'], 'eligible for validation')
        before = copy.deepcopy(manifest)
        before['dataset'][0]['collections'][0]['fields'][0].pop('meta')
        self.assertTrue(field_review.validate_contract(self.document, self.derived, self.repo, candidate=before))
        self.document.pop('classification_review')
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertEqual(self.row('email')['personal_data'], 'unknown')
        self.assertEqual(self.row('email')['accepted_uncertainty'], accepted)
        self.assertEqual(field_review.validate_contract(self.document, self.derived, self.repo, candidate=manifest), [])

    def test_gap_acceptance_accounts_for_every_occurrence_without_claiming_structure(self):
        gap = self.gap(grouped=True)
        self.resolve()
        decision = self.answer(gap, 'accepted_uncertainty')
        decision['accepted_uncertainty'] = self.acceptance(gap['gap_id'], gap['evidence_ids'])
        decision['export_targets'] = ['dataset:db']
        incomplete = copy.deepcopy(decision)
        incomplete['covered_occurrence_ids'] = decision['covered_occurrence_ids'][:1]
        self.assertTrue(field_review.apply_gap_decisions(self.document, [incomplete], self.repo))
        self.assertEqual(field_review.apply_gap_decisions(self.document, [decision], self.repo), [])
        partition = field_review.gap_partition(self.document, self.derived, self.repo)
        self.assertEqual(len(partition['observed_diagnostics']), 1)
        self.assertEqual(partition['active_blockers'], [])
        self.assertEqual(partition['resolved_diagnostics'][0]['resolution']['outcome'], 'accepted_uncertainty')
        self.assertEqual(review.structural_blockers(self.document, {}, self.derived, self.repo), [])
        manifest = self.accepted_fixture(resolve=False)
        self.assertEqual(len(self.export(manifest)['dataset'][0]['meta']['accepted_uncertainties']), 1)
        self.document.pop('classification_review')
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertEqual(field_review.validate_contract(self.document, self.derived, self.repo, candidate=manifest), [])
        self.assertEqual(review.scan_progress(self.document, {}, self.derived, [], [], self.repo)['accepted_uncertainty_count'], 1)

    def test_missing_owner_scope_evidence_timestamp_and_export_target_rejected(self):
        gap = self.gap()
        self.resolve()
        decision = self.answer(gap, 'accepted_uncertainty')
        decision['accepted_uncertainty'] = self.acceptance(gap['gap_id'], gap['evidence_ids'])
        decision['export_targets'] = ['dataset:db']
        for key, value in [('accepted_by', ''), ('scope', 'other'), ('evidence_ids', ['missing']),
                           ('accepted_at', '2999-01-01T00:00:00Z'), ('accepted_at', None), ('residual_uncertainty', ''), ('map_impact', '')]:
            with self.subTest(key=key):
                invalid = copy.deepcopy(decision)
                invalid['accepted_uncertainty'][key] = value
                before = copy.deepcopy(self.document)
                self.assertTrue(field_review.apply_gap_decisions(self.document, [invalid], self.repo))
                self.assertEqual(self.document, before)
        decision['export_targets'] = ['dataset:missing']
        self.assertTrue(field_review.apply_gap_decisions(self.document, [decision], self.repo))

    def test_acceptance_survives_unchanged_scan_but_reopens_on_changed_evidence(self):
        decision = self.unknown()
        decision['accepted_uncertainty'] = self.acceptance(decision['entity_id'], decision['evidence_ids'])
        self.assertEqual(field_review.apply_decisions(self.document, [decision], self.repo), [])
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertEqual(self.row('email')['status'], 'proposed')
        (self.repo / 'schema.ts').write_text(self.source + '\n// new evidence\n')
        self.assertTrue(field_review.validate_contract(self.document, self.derived, self.repo))
        self.compiler['tables'][0]['fields'][0]['shape'] = 'changed'
        def refresh(value):
            if isinstance(value, dict):
                if 'path' in value and 'start_line' in value and 'sha256' in value:
                    value.update(field_review.span_from_ref(self.repo, value['path'] + ':' + str(value['start_line'])))
                else:
                    for child in value.values():
                        refresh(child)
            elif isinstance(value, list):
                for child in value:
                    refresh(child)
        refresh(self.compiler)
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertEqual(self.row('email')['status'], 'needs_investigation')
        self.assertTrue(self.document['classification_review']['uncertainty_history'])

    def test_accepting_parent_does_not_hide_unreviewed_known_children(self):
        parent = self.decision('payload', 'unknown', [], 'unknown')
        parent['accepted_uncertainty'] = self.acceptance(parent['entity_id'], parent['evidence_ids'])
        self.assertTrue(field_review.apply_decisions(self.document, [parent], self.repo))

    def test_relationship_question_uses_the_same_disposition(self):
        self.derived['relationship_mapping'] = {'questions': [
            {'id': 'connection:remote', 'unresolved_question': 'Which deployment receives this flow?',
             'resolution_needed': 'The deployment connection contract.'}]}
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.resolve()
        gap = self.document['classification_review']['gaps'][0]
        evidence = self.row('email')['evidence_ids']
        decision = self.answer(gap, 'accepted_uncertainty')
        decision.update(evidence_ids=evidence, export_targets=['dataset:db'],
                        accepted_uncertainty=self.acceptance(gap['gap_id'], evidence))
        self.assertEqual(field_review.apply_gap_decisions(self.document, [decision], self.repo), [])
        progress = review.scan_progress(self.document, {}, self.derived, [], [], self.repo)
        self.assertEqual(progress['technical_questions'], [])
        self.assertEqual(progress['extraction_gaps'], 0)
        self.assertEqual(progress['accepted_uncertainty_count'], 1)

    def test_unknown_acceptance_is_not_inferred_and_batch_is_atomic(self):
        self.unknown()
        before = copy.deepcopy(self.document)
        row = self.row('email')
        entry = {'kind': 'field', 'target_id': row['entity_id'],
                 'acceptance': self.acceptance(row['entity_id'], row['evidence_ids'])}
        with self.assertRaises(ValueError):
            investigate.apply_uncertainties(self.document, [entry, dict(entry, target_id='missing')], self.repo)
        self.assertEqual(self.document, before)
        batch = investigate.prepare_batch(self.document, 24, self.repo)
        candidate = next(item for item in batch['uncertainty_candidates'] if item['target_id'] == row['entity_id'])
        self.assertEqual(candidate['acceptance']['accepted_by'], '')
        self.assertTrue(field_review.validate_contract(self.document, self.derived, self.repo))

    def test_removing_an_uncertainty_disclosure_requires_review(self):
        import analysis_cache
        decision = self.unknown()
        decision['accepted_uncertainty'] = self.acceptance(decision['entity_id'], decision['evidence_ids'])
        self.assertEqual(field_review.apply_decisions(self.document, [decision], self.repo), [])
        manifest = self.accepted_fixture(resolve=False)
        before = analysis_cache.semantic_snapshot(manifest)
        changed = copy.deepcopy(manifest)
        changed['dataset'][0]['collections'][0]['fields'][0].pop('meta')
        changes = analysis_cache.semantic_changes(before, analysis_cache.semantic_snapshot(changed))
        self.assertTrue(any(row['kind'] == 'accepted_uncertainty_removed' for row in changes))

    def test_unknown_member_is_not_aggregated_into_non_personal_parent(self):
        self.resolve()
        child = self.decision('payload.actor', 'unknown', [], 'unknown')
        child['uncertainty'] = 'The producer subject is unknown.'
        child['accepted_uncertainty'] = self.acceptance(child['entity_id'], child['evidence_ids'])
        self.assertEqual(field_review.apply_decisions(self.document, [child], self.repo), [])
        self.assertEqual(self.row('payload')['personal_data'], 'unknown')
        self.assertEqual(self.row('payload')['status'], 'needs_investigation')
        parent = self.decision('payload', 'unknown', [], 'unknown')
        parent['accepted_uncertainty'] = self.acceptance(parent['entity_id'], parent['evidence_ids'])
        self.assertEqual(field_review.apply_decisions(self.document, [parent], self.repo), [])
        self.assertEqual(field_review.validate_contract(self.document, self.derived, self.repo), [])

    def test_incomplete_acceptance_input_is_rejected_without_changes(self):
        self.unknown()
        before = copy.deepcopy(self.document)
        for entries in ([None], [{'kind': 'field'}],
                        [{'kind': 'field', 'target_id': 'db/records/email', 'acceptance': None}],
                        [{'kind': 'field', 'target_id': 'db/records/email',
                          'acceptance': uncertainty.template('db/records/email')}]):
            with self.subTest(entries=entries):
                with self.assertRaises(ValueError):
                    investigate.apply_uncertainties(self.document, entries, self.repo)
                self.assertEqual(self.document, before)

    def test_processing_question_is_accounted_for_by_scoped_acceptance(self):
        self.derived['systems'] = [{'fides_key': 'runtime', 'name': 'Runtime', 'refs': ['schema.ts:1']}]
        self.document['system_proposals'] = [{'entity_id': 'runtime', 'processing_activities': [
            {'activity_id': 'delivery', 'unresolved_question': 'Which recipient operates this deployment?',
             'resolution_needed': 'The recipient deployment contract.', 'refs': ['schema.ts:1'],
             'decision_impact': 'Identifies the recipient for the represented processing.'}]}]
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.resolve()
        gap = self.document['classification_review']['gaps'][0]
        self.assertEqual(gap['kind'], 'processing_uncertainty')
        self.assertEqual(field_review.gap_resolution.agent_blockers(
            field_review.gap_partition(self.document, self.derived, self.repo)), [])
        self.assertTrue(field_review.gap_partition(self.document, self.derived, self.repo)['active_blockers'])
        progress = review.scan_progress(self.document, {}, self.derived, [], [], self.repo)
        self.assertEqual(progress['agent_investigation'], 'complete')
        self.assertEqual(progress['publication'], 'blocked')
        self.assertIn('Which recipient operates this deployment?', progress['missing_facts'])
        decision = self.answer(gap, 'accepted_uncertainty')
        decision.update(export_targets=['system:runtime'],
                        accepted_uncertainty=self.acceptance(gap['gap_id'], gap['evidence_ids']))
        self.assertEqual(field_review.apply_gap_decisions(self.document, [decision], self.repo), [])
        progress = review.scan_progress(self.document, {}, self.derived, [], [], self.repo)
        self.assertEqual(progress['missing_facts'], [])
        self.assertEqual(progress['technical_questions'], [])
        self.assertEqual(progress['extraction_gaps'], 0)
        self.document['system_proposals'][0]['processing_activities'][0]['unresolved_question'] = 'Which other recipient is involved?'
        self.assertTrue(field_review.gap_partition(self.document, self.derived, self.repo)['active_blockers'])

    def test_feedback_reopens_previously_accepted_uncertainty(self):
        decision = self.unknown()
        decision['accepted_uncertainty'] = self.acceptance(decision['entity_id'], decision['evidence_ids'])
        self.assertEqual(field_review.apply_decisions(self.document, [decision], self.repo), [])
        self.document = investigate.record_feedback(self.document, [
            {'entity_id': decision['entity_id'], 'statement': 'New producer exists.',
             'interpretation': 'Investigate the additional writer.'}])
        self.assertNotIn('accepted_uncertainty', self.row('email'))
        self.assertEqual(self.row('email')['status'], 'needs_investigation')


if __name__ == '__main__':
    unittest.main()
