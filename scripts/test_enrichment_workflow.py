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

    def test_structural_shape_quotes_roundtrip_offline(self):
        for shape in ['text("email")', 'path\\member', 'quoted "value"', "owner's contract"]:
            self.assertEqual(reconcile.VALIDATOR._scalar(json.dumps(shape)), shape)
        self.assertEqual(reconcile.VALIDATOR._scalar("'owner''s contract'"), "owner's contract")

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

    def test_new_supplemental_members_receive_structural_provenance(self):
        candidate = {'dataset': [{'fides_key': 'db', 'meta': {'noru': {'origin': 'supplemental'}},
            'collections': [{'name': 'records', 'fields': [{'name': 'payload',
                'meta': {'noru': {'shape': 'object', 'evidence_kind': 'serializer'}}}]}]}]}
        field_review.project_candidate(self.document, candidate, self.derived)
        payload = next(f for f in candidate['dataset'][0]['collections'][0]['fields'] if f['name'] == 'payload')
        self.assertEqual(payload['meta']['noru']['evidence_kind'], 'serializer')
        for child in payload['fields']:
            self.assertEqual(child['meta']['noru'], {'shape': 'string', 'evidence_kind': 'typed_contract'})
            self.assertTrue(child['needs_review'])

    def test_investigated_nested_contract_remains_collectable(self):
        import dataset_inputs
        fixture = fixtures.ClassificationGateTests()
        fixture.setUp()
        try:
            manifest = fixture.supplemental_projection_fixture()
            field_review.project_candidate(fixture.document, manifest, repo=fixture.repo)
            observed = dataset_inputs.extract(fixture.repo, document=manifest)
            nested = next(f for f in observed['datasets'][0]['collections'][0]['fields'] if f['name'] == 'payload.details')
            self.assertEqual(nested['shape'], 'object')
        finally:
            fixture.doCleanups()

    def test_non_personal_projection_does_not_create_taxonomy_or_acceptance(self):
        self.document['system_proposals'] = [{'entity_id': 'host', 'personal_data': 'no', 'non_personal_rationale': 'Provisioning only.',
            'refs': ['schema.ts:1'], 'investigation': {'runtime_processing': 'Inspected startup.'}, 'processing_activities': []}]
        candidate = {'system': [{'fides_key': 'host', 'privacy_declarations': []}]}
        field_review.project_candidate(self.document, candidate, self.derived, self.repo)
        system = candidate['system'][0]
        self.assertEqual(system['privacy_declarations'], [])
        self.assertTrue(system['meta']['non_personal_processing']['needs_review'])
        self.assertEqual(review.requested_systems(candidate), [system])

    def processing_candidate(self, uses):
        activity = {'purpose': 'Provide access', 'proposed_data_uses': uses,
                    'proposed_data_subjects': ['customer'], 'proposed_categories': ['user.unique_id'],
                    'refs': ['schema.ts:1'], 'recipient_systems': []}
        self.document['system_proposals'] = [{'entity_id': 'service', 'processing_activities': [activity]}]
        candidate = {'dataset': [], 'system': [{'fides_key': 'service', 'name': 'Service', 'privacy_declarations': []}]}
        field_review.project_candidate(self.document, candidate, self.derived)
        return candidate

    def test_multi_use_projection_export_and_snapshot_share_names(self):
        candidate = self.processing_candidate(['essential.service.authentication', 'essential.service'])
        names = [d['name'] for d in candidate['system'][0]['privacy_declarations']]
        self.assertEqual(names, ['Provide access (essential.service.authentication)', 'Provide access (essential.service)'])
        cache = reconcile.analysis_cache
        self.assertEqual(cache.semantic_snapshot(candidate)['activities'], cache.semantic_snapshot(candidate, self.document)['activities'])
        for declaration in candidate['system'][0]['privacy_declarations']:
            declaration['needs_review'] = False
            declaration['interpretation'] = {'owner': 'Synthetic Reviewer', 'decided_at': '2026-10-15', 'rationale': 'Synthetic export regression.'}
        script = "import {toFideslang} from './plugins/privacy-datamap/scripts/lib/fides.mjs'; import {readFileSync} from 'node:fs'; console.log(JSON.stringify(toFideslang(JSON.parse(readFileSync(0,'utf8')))));"
        exported = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT,
                                  input=json.dumps(candidate), text=True, capture_output=True, check=True)
        declarations = json.loads(exported.stdout)['system'][0]['privacy_declarations']
        self.assertEqual([d['name'] for d in declarations], names)
        self.assertEqual(len({d['name'] for d in declarations}), len(declarations))

    def test_single_use_name_and_semantics_remain_unchanged(self):
        candidate = self.processing_candidate(['essential.service'])
        self.assertEqual(candidate['system'][0]['privacy_declarations'][0]['name'], 'Provide access')
        cache = reconcile.analysis_cache
        self.assertEqual(cache.semantic_changes(cache.semantic_snapshot(candidate), cache.semantic_snapshot(candidate, self.document)), [])

    def test_legacy_multi_use_names_migrate_without_hiding_narrowing(self):
        candidate = self.processing_candidate(['essential.service.authentication', 'essential.service'])
        cache = reconcile.analysis_cache
        current = cache.semantic_snapshot(candidate)
        for uses in [['essential.service'], ['essential.service', 'essential.service.authentication']]:
            old = copy.deepcopy(next(iter(current['activities'].values())))
            old['purposes'] = uses
            lock = {'semantic_snapshot': {'activities': {'service/Provide access': old}}}
            result = {'removal_required': []}
            reconcile.add_semantic_review(result, lock, candidate)
            self.assertEqual(result['removal_required'], [])
            self.assertEqual(result['mechanical_declaration_renames'][0]['previous'], 'service/Provide access')
            narrowed = copy.deepcopy(current)
            narrowed['activities']['service/Provide access (essential.service)']['subjects'] = []
            self.assertTrue(cache.semantic_changes(lock['semantic_snapshot'], narrowed))

    def test_duplicate_declarations_rejected_before_export(self):
        candidate = self.processing_candidate(['essential.service', 'essential.service'])
        report = reconcile.VALIDATOR.Report()
        reconcile.VALIDATOR.check_systems(report, candidate, reconcile.VALIDATOR.load_vocabulary(), set(), collections.Counter(), datetime.date.today())
        self.assertTrue(any('Names must be unique' in message for _, message in report.errors))
        # Exercise the review entry point too, even though this incomplete fixture has other review errors.
        result = {'removal_required': [], 'investigation_required': [], 'observation_digest': self.document['observation_digest']}
        errors = review.validate(self.document, result, candidate, self.repo)
        self.assertTrue(any('privacy declaration names must be unique' in message for message in errors))

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

    def test_nested_supplemental_apply_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            (repo / 'storage.ts').write_text('export const payload = {member: "value"};\n')
            manifest = repo / '.noru/privacy-datamap.yml'
            manifest.parent.mkdir()
            field = {'name': 'payload', 'refs': ['storage.ts:1'], 'data_categories': [],
                     'meta': {'noru': {'shape': 'JSON object', 'evidence_kind': 'serializer'}},
                     'fields': [{'name': 'member', 'refs': ['storage.ts:1'], 'data_categories': [],
                                 'meta': {'noru': {'shape': 'string', 'evidence_kind': 'serializer'}}}]}
            doc = {'version': '0.13.0', 'piece': 'privacy-datamap',
                   'source': {'slug': 'fixture/contracts', 'commit_sha': 'a'*40, 'branch': 'main', 'generated_by': 'privacy-datamap', 'derived_digest': '0'*64},
                   'dataset': [{'fides_key': 'objects', 'name': 'Objects', 'meta': {'noru': {'origin': 'supplemental', 'refs': ['storage.ts:1']}},
                                'collections': [{'name': 'records', 'refs': ['storage.ts:1'], 'fields': [field]}]}], 'system': []}
            manifest.write_text(reconcile.to_yaml(doc))
            scripts = ROOT / 'plugins/privacy-datamap/scripts'
            def run(name, *args):
                result = subprocess.run(['node' if name.endswith('.mjs') else sys.executable, str(scripts / name), '--repo=' + directory, *args], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result
            run('collect.mjs')
            run('reconcile.py')
            run('reconcile.py', '--apply-candidate')
            applied = reconcile.load_manifest(manifest)
            collection = applied['dataset'][0]['collections'][0]
            self.assertEqual(collection['fields'][0]['fields'][0]['meta'], field['fields'][0]['meta'])
            self.assertEqual(collection['structure_digest'], reconcile.VALIDATOR.structure_digest(collection['fields']))
            run('collect.mjs')
            _, scan = reconcile.load_observations(repo)
            self.assertEqual(applied['source']['derived_digest'], scan['derived_digest'])
            # Applying newly modeled structure must bind the resulting ordinary scan, not its predecessor.
            prior_digest = applied['source']['derived_digest']
            member = copy.deepcopy(field['fields'][0]); member['name'] = 'additional'
            collection['fields'][0]['fields'].append(member)
            collection['structure_digest'] = reconcile.structure_digest(collection['fields'])
            candidate_apply.apply(repo, applied, {}, reconcile.to_yaml)
            run('collect.mjs')
            _, scan = reconcile.load_observations(repo)
            applied = reconcile.load_manifest(manifest)
            self.assertNotEqual(applied['source']['derived_digest'], prior_digest)
            self.assertEqual(applied['source']['derived_digest'], scan['derived_digest'])
            before = manifest.read_bytes()
            del applied['dataset'][0]['collections'][0]['fields'][0]['fields'][0]['meta']
            with self.assertRaisesRegex(ValueError, 'Supplemental fields require'):
                candidate_apply.apply(repo, applied, {}, reconcile.to_yaml)
            self.assertEqual(manifest.read_bytes(), before)
            self.assertFalse((repo / '.noru/.cache/privacy-datamap.apply.json').exists())

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
