#!/usr/bin/env python3
"""Enrichment reuse never implies classification or human approval."""
import copy
import collections
import datetime
import json
import pathlib
import sys
import tempfile
import subprocess
import unittest
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'plugins/privacy-datamap/scripts'))
import candidate_apply
import investigate
import field_review
import reconcile
import review
import test_classification_gate as fixtures


class WorkflowTests(unittest.TestCase):
    setUp = fixtures.ClassificationGateTests.setUp
    row = fixtures.ClassificationGateTests.row
    decision = fixtures.ClassificationGateTests.decision
    resolve = fixtures.ClassificationGateTests.resolve

    def test_skill_keeps_uncertainty_acceptance_available(self):
        text = (ROOT / 'plugins/privacy-datamap/skills/privacy-datamap/SKILL.md').read_text()
        self.assertIn('scoped uncertainty acceptance can account', text)
        self.assertNotIn('cannot supply missing facts or close an extraction gap', text)

    def test_shared_reasoning_keeps_field_outcomes_and_overrides(self):
        self.resolve()
        original = [field_review.decision_template(row) for row in field_review.all_rows(self.document['classification_review']['fields']) if not row.get('nested_members')]
        group = copy.deepcopy(original[0]['semantic_review'])
        decision = copy.deepcopy(original[0]); decision.pop('semantic_review'); decision['reasoning_group'] = 'operational'
        expanded = investigate.expand_reasoning({'reasoning_groups': {'operational': {'semantic_review': group}}, 'decisions': [decision]})
        self.assertEqual(expanded[0], original[0])
        self.assertEqual(field_review.apply_decisions(self.document, expanded, self.repo), [])
        dimension = next(iter(group))
        decision['semantic_review'] = {dimension: {'finding': 'Specific exception', 'evidence_ids': original[0]['evidence_ids']}}
        self.assertEqual(investigate.expand_reasoning({'reasoning_groups': {'operational': {'semantic_review': group}}, 'decisions': [decision]})[0]['semantic_review'][dimension]['finding'], 'Specific exception')
        with self.assertRaises(ValueError):
            investigate.expand_reasoning({'reasoning_groups': {'operational': {'personal_data': 'no'}}, 'decisions': [decision]})

    def test_nested_projection_signature_matches_validator(self):
        candidate = {'dataset': [{'fides_key': 'db', 'collections': [{'name': 'records', 'fields': []}]}]}
        field_review.project_candidate(self.document, candidate, self.derived, self.repo)
        collection = candidate['dataset'][0]['collections'][0]
        self.assertEqual(collection['structure_digest'], reconcile.VALIDATOR.structure_digest(collection['fields']))
        self.assertTrue(next(f for f in collection['fields'] if f['name'] == 'payload')['fields'])

    def test_non_personal_projection_does_not_create_taxonomy_or_acceptance(self):
        self.document['system_proposals'] = [{'entity_id': 'host', 'personal_data': 'no', 'non_personal_rationale': 'Provisioning only.',
            'refs': ['schema.ts:1'], 'investigation': {'runtime_processing': 'Inspected startup.'}, 'processing_activities': []}]
        candidate = {'system': [{'fides_key': 'host', 'privacy_declarations': []}]}
        field_review.project_candidate(self.document, candidate, self.derived, self.repo)
        system = candidate['system'][0]
        self.assertEqual(system['privacy_declarations'], [])
        self.assertTrue(system['meta']['non_personal_processing']['needs_review'])
        self.assertEqual(review.requested_systems(candidate), [system])

    def test_non_personal_acceptance_reopens_on_source_and_flow_changes(self):
        import system_review
        validator = reconcile.VALIDATOR
        system = {'fides_key': 'host', 'name': 'Host', 'privacy_declarations': [], 'dataset_references': []}
        record = {'rationale': 'Inspected provisioning only; no personal payload processing.', 'refs': ['schema.ts:1'],
                  'interpretation': {'owner': 'Synthetic Reviewer', 'decided_at': '2026-01-01', 'rationale': 'Reviewed the provisioning evidence.'}}
        record['evidence_binding'] = system_review.binding(system, record['refs'], self.repo)
        system['meta'] = {'non_personal_processing': record}
        def errors():
            report = validator.Report()
            validator.check_systems(report, {'system': [system]}, validator.load_vocabulary(), set(), collections.Counter(), datetime.date(2026, 1, 1), self.repo)
            return report.errors
        self.assertEqual(errors(), [])
        system['egress'] = [{'fides_key': 'host', 'type': 'system'}]
        self.assertTrue(errors())
        system.pop('egress')
        (self.repo / 'schema.ts').write_text('changed source')
        self.assertTrue(errors())

    def test_candidate_apply_archives_proposals_and_preserves_review(self):
        cache = self.repo / '.noru/.cache'; cache.mkdir(parents=True, exist_ok=True)
        path = cache / 'privacy-datamap.analysis.json'
        path.write_text('{}')
        document = {'classification_review': self.document['classification_review'], 'structural_proposal': {'dataset': []},
                    'relationship_proposal': {'graph': {}}, 'preview': {'derived': {}}, 'collection_mode': 'candidate'}
        candidate = {'dataset': [], 'system': [], 'needs_review': True}
        candidate_apply.apply(self.repo, candidate, document, reconcile.to_yaml)
        stored = json.loads(path.read_text())
        self.assertNotIn('structural_proposal', stored)
        self.assertIn('structural_proposal', stored['applied_proposals'])
        self.assertEqual(stored['classification_review'], document['classification_review'])
        self.assertFalse((self.repo / '.noru/privacy-datamap.lock.json').exists())
        self.assertIn('needs_review: true', (self.repo / '.noru/privacy-datamap.yml').read_text())

    def test_apply_candidate_cli_writes_draft_and_rejects_stale_source(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            (repo / 'schema.sql').write_text('CREATE TABLE records (code TEXT);')
            scripts = ROOT / 'plugins/privacy-datamap/scripts'
            for command in (['node', str(scripts / 'collect.mjs'), '--repo=' + directory],
                            [sys.executable, str(scripts / 'reconcile.py'), '--repo=' + directory],
                            [sys.executable, str(scripts / 'reconcile.py'), '--repo=' + directory, '--apply-candidate']):
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            manifest = repo / '.noru/privacy-datamap.yml'
            before = manifest.read_bytes()
            self.assertIn(b'needs_review: true', before)
            self.assertFalse((repo / '.noru/privacy-datamap.lock.json').exists())
            (repo / 'schema.sql').write_text('CREATE TABLE records (code TEXT, extra TEXT);')
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(manifest.read_bytes(), before)

    def test_candidate_recovery_refuses_intervening_edits(self):
        cache = self.repo / '.noru/.cache'; cache.mkdir(parents=True, exist_ok=True)
        journal = {'files': [{'before': None, 'after': 'new manifest'}, {'before': None, 'after': '{}'}]}
        path = cache / 'privacy-datamap.apply.json'; path.write_text(json.dumps(journal))
        manifest = self.repo / '.noru/privacy-datamap.yml'; manifest.write_text('user edit')
        with self.assertRaises(ValueError): candidate_apply.recover(self.repo)
        self.assertEqual(manifest.read_text(), 'user edit')
        manifest.write_text('new manifest')
        self.assertTrue(candidate_apply.recover(self.repo))
        self.assertFalse(path.exists())

    def test_migration_group_submission_covers_every_individual_finding(self):
        import gap_resolution
        from test_gap_resolution import ResolutionTests
        self.derived['coverage'] = {'migration_gaps': [
            {'diagnostic_id': 'migration:' + str(i), 'code': 'MIGRATION_CURRENT_COVERAGE',
             'scope': {'datastore': 'db', 'context': 'Store'}, 'ref': 'schema.ts:1',
             'reason': 'Investigate current coverage', 'evidence': [field_review.span_from_ref(self.repo, 'schema.ts:1')]}
            for i in range(3)]}
        field_review.build_contract(self.document, self.repo, self.compiler)
        gaps = field_review.gap_partition(self.document, self.derived, self.repo)['active_blockers']
        groups = investigate.migration_groups(gaps)
        self.assertEqual(len(groups), 1)
        answer = ResolutionTests.answer(self, gaps[0], 'not_applicable')
        entry = {**groups[0], 'resolution': answer}
        expanded = investigate.expand_gap_groups(self.document, [entry], self.repo)
        self.assertEqual(len(expanded), 3)
        self.assertEqual(field_review.apply_gap_decisions(self.document, expanded, self.repo), [])
        self.assertFalse(field_review.gap_partition(self.document, self.derived, self.repo)['active_blockers'])
        self.assertEqual(len(self.document['classification_review']['gaps']), 3)
        # A changed membership cannot be silently covered by a prior group decision.
        self.derived['coverage']['migration_gaps'].append({**self.derived['coverage']['migration_gaps'][0], 'diagnostic_id': 'migration:new'})
        field_review.build_contract(self.document, self.repo, self.compiler)
        with self.assertRaises(ValueError): investigate.expand_gap_groups(self.document, [entry], self.repo)


if __name__ == '__main__': unittest.main()
