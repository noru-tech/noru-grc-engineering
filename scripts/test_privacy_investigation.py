#!/usr/bin/env python3
"""The agent queue is bounded, evidence-bound and writes only the canonical cache."""
import copy
import json
import pathlib
import subprocess
import sys
from privacy_semantic_test_support import semantic_review
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'plugins/privacy-datamap/scripts'
sys.path.insert(0, str(SCRIPTS))
import analysis_storage
import field_review
import investigate


class InvestigationTests(unittest.TestCase):
    def test_progress_separates_investigation_missing_facts_and_approval(self):
        import review
        result = {'actions': [], 'collection_review_required': ['db/records'], 'observation_digest': 'fixture'}
        progress = review.scan_progress(self.document, result, self.derived, [], [])
        self.assertEqual(progress['collection'], 'complete')
        self.assertEqual(progress['agent_investigation'], 'incomplete')
        self.assertEqual(progress['human_approval'], 'deferred')
        for row in self.document['classification_review']['fields']:
            row.update(status='proposed', resolution_owner=None)
        progress = review.scan_progress(self.document, result, self.derived, [], [])
        self.assertEqual(progress['agent_investigation'], 'complete')
        self.assertEqual(progress['human_approval'], 'pending')
        self.assertEqual(progress['publication'], 'blocked')
        row.update(status='needs_investigation', resolution_owner='human', business_context_question='Is this an individual account?')
        progress = review.scan_progress(self.document, result, self.derived, [], [])
        self.assertEqual(progress['agent_investigation'], 'complete')
        self.assertEqual(progress['source_information'], 'missing')
        self.assertEqual(progress['human_approval'], 'deferred')
        self.assertEqual(progress['missing_facts'], ['Is this an individual account?'])
        row.update(status='proposed', resolution_owner=None)
        result['accepted_current'] = True
        self.assertEqual(review.scan_progress(self.document, result, self.derived, [], [])['human_approval'], 'recorded')

    def test_feedback_is_deduplicated_and_does_not_accept_or_classify(self):
        entries = [{'entity_id': 'db/records/code', 'statement': 'This is not PII.',
                    'interpretation': 'User means it does not directly identify a person; investigate remaining person association.'}]
        before = copy.deepcopy(self.document)
        updated = investigate.record_feedback(self.document, entries)
        self.assertEqual(before, self.document)
        row = updated['classification_review']['fields'][0]
        self.assertEqual(row['personal_data'], 'unknown')
        self.assertEqual(row['resolution_owner'], 'agent')
        self.assertEqual(row['uncertainty'], 'user_feedback')
        self.assertEqual(len(row['user_feedback']), 1)

        self.assertEqual(investigate.record_feedback(updated, entries), updated)
        self.assertFalse(field_review.validate_contract(updated, self.derived, self.repo, require_complete=False))
        self.assertTrue(field_review.validate_contract(updated, self.derived, self.repo, require_complete=True))
        with self.assertRaises(ValueError):
            investigate.record_feedback(updated, entries + [{'entity_id': 'missing'}])
        self.assertEqual(len(row['user_feedback']), 1)

    def test_feedback_survives_decision_and_reextraction_without_acceptance(self):
        entries = [{'entity_id': 'db/records/code', 'statement': 'It is only an internal record key.',
                    'interpretation': 'Investigate whether the key still relates to an individual.'}]
        self.document = investigate.record_feedback(self.document, entries)
        row = self.document['classification_review']['fields'][0]
        decision = self.decision(row)
        self.assertEqual(field_review.apply_decisions(self.document, [decision], self.repo), [])
        row = self.document['classification_review']['fields'][0]
        self.assertEqual(row['user_feedback'][0]['statement'], entries[0]['statement'])
        bad = self.decision(row)
        bad['user_feedback'] = []
        self.assertTrue(field_review.apply_decisions(self.document, [bad], self.repo))
        (self.repo/'schema.sql').write_text((self.repo/'schema.sql').read_text()+'\n')
        field_review.build_contract(self.document, self.repo, compiler_report={'tables': [], 'gaps': []})
        row = self.document['classification_review']['fields'][0]
        self.assertEqual(row['user_feedback'][0]['statement'], entries[0]['statement'])
        self.assertEqual(row['status'], 'needs_investigation')

    def test_report_preserves_known_json_members_and_explains_extraction_gap(self):
        import review
        parent = self.document['classification_review']['fields'][0]
        child = copy.deepcopy(parent)
        child.update(entity_id=parent['entity_id']+'.email', name='email', categories=['user.contact.email'], personal_data='yes', status='proposed')
        parent.update(composite=True, open_content=True, nested_members=[child])
        self.document['classification_review']['gaps'] = [{'kind': 'runtime_object_shape', 'subject': parent['entity_id'], 'message': 'Writer shape is unresolved.', 'status': 'unresolved'}]
        result = {'actions': [], 'collection_review_required': [], 'observation_digest': 'fixture'}
        output = review.build_outputs(self.document, result, self.derived, {'dataset': [], 'system': []}, ['Shape investigation required'])
        report = '\n'.join(review.render_outputs(output))
        self.assertIn('known members: email', report)
        self.assertIn('Additional content: open or unresolved', report)
        self.assertIn('Extraction limitation', report)
        self.assertIn('Writer shape is unresolved', report)
        self.assertIn('user.contact.email', report)
        self.assertEqual(output['progress']['extraction_gaps'], 1)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = pathlib.Path(self.temporary.name)
        (self.repo/'schema.sql').write_text('CREATE TABLE records (\n  code TEXT,\n  label TEXT\n);\n')
        self.path = self.repo/'.noru/.cache/privacy-datamap.analysis.json'
        self.path.parent.mkdir(parents=True)
        fields = [{'name': name, 'entity_id': 'db/records/'+name, 'shape':'TEXT', 'refs':['schema.sql:'+str(line)]} for name,line in [('code',2),('label',3)]]
        self.derived = {'datasets':[{'fides_key':'db','collections':[{'name':'records','fields':fields}]}]}
        self.document = {'observation_digest':'fixture', 'preview':{'derived':self.derived}}
        field_review.build_contract(self.document, self.repo, compiler_report={'adapter':{'name':'fixture','version':1},'tables':[],'gaps':[]}, fresh=True)
        analysis_storage.save(self.path, self.document)

    def command(self, *args):
        return subprocess.run([sys.executable,str(SCRIPTS/'investigate.py'),'--repo='+str(self.repo),*args],text=True,capture_output=True)

    def test_bounded_batch_preserves_remaining_work(self):
        before=self.path.read_bytes()
        result=self.command('--limit=1')
        self.assertEqual(result.returncode,0,result.stderr)
        batch=json.loads(result.stdout)
        self.assertEqual(batch['counts'],{'agent_pending':2,'agent_pending_fields':2,'agent_pending_gaps':0,
                                         'human_pending':0,'included':1,'remaining_agent':1})
        self.assertEqual(len(batch['requests'][0]['evidence']),1)
        self.assertEqual(before,self.path.read_bytes())
        self.assertIn('not completion',batch['instructions'])

    def test_invalid_decision_batch_does_not_modify_cache(self):
        row=self.document['classification_review']['fields'][0]
        decision=field_review.decision_template(row)
        decision.update(categories=['not.a.taxonomy.category'], personal_data='yes')
        path=self.repo/'decision-input.json';path.write_text(json.dumps([decision]))
        # Source inventory includes ordinary JSON; rebuild after adding the input file. Real agents
        # should pass stdin or a temporary file outside the scanned source tree.
        field_review.build_contract(self.document,self.repo,compiler_report={'tables':[],'gaps':[]},fresh=True)
        analysis_storage.save(self.path,self.document)
        before=self.path.read_bytes()
        result=self.command('--decisions='+str(path))
        self.assertEqual(result.returncode,2)
        self.assertEqual(self.path.read_bytes(),before)

    def test_missing_contract_is_not_an_empty_successful_queue(self):
        del self.document['classification_review']
        analysis_storage.save(self.path,self.document)
        result=self.command()
        self.assertEqual(result.returncode,2)
        self.assertIn('build current evidence',result.stderr)

    def test_human_context_is_separate_from_agent_work(self):
        row=self.document['classification_review']['fields'][0]
        row['resolution_owner']='human'
        batch=investigate.prepare_batch(self.document,24)
        self.assertEqual(batch['counts']['agent_pending'],1)
        self.assertEqual(batch['counts']['human_pending'],1)
        self.assertEqual(batch['human_questions'][0]['entity_id'],row['entity_id'])

    def test_changed_source_requires_new_evidence_before_retrieval(self):
        before=self.path.read_bytes()
        (self.repo/'schema.sql').write_text('CREATE TABLE records (code TEXT, label INTEGER);\n')
        result=self.command()
        self.assertEqual(result.returncode,2)
        self.assertIn('changed',result.stderr)
        self.assertEqual(before,self.path.read_bytes())

    def test_taxonomy_hints_never_become_verdicts(self):
        row=copy.deepcopy(self.document['classification_review']['fields'][0])
        row['name']='email'
        candidates=investigate.category_candidates(row,{})
        self.assertEqual(candidates[0]['category'],'user.contact.email')
        self.assertEqual(candidates[0]['status'],'candidate_requires_evidence')
        self.assertEqual(row['categories'],[])
        self.assertEqual(row['personal_data'],'unknown')

    def test_queue_requires_semantic_scope_without_populating_findings(self):
        batch = investigate.prepare_batch(self.document, 1)
        request = batch['requests'][0]
        self.assertEqual(set(request['semantic_review_questions']), set(field_review.SEMANTIC_REVIEW_QUESTIONS))
        self.assertTrue(all(entry['finding'] == '' for entry in request['decision_template']['semantic_review'].values()))
        self.assertIn('mocks', request['semantic_review_questions']['behavior_and_scope'])
        self.assertIn('correlation', request['semantic_review_questions']['association_and_identifiers'])

    def test_blocked_report_preserves_sensitive_findings_and_processing_scope(self):
        import review
        outputs = {
            'status': 'enrichment incomplete',
            'data_map': {'stores': [], 'processing_activities': [{'system': 'fixture', 'purpose': 'Model care',
                'decision_state': 'proposed', 'processing_mode': 'unknown',
                'recipient_rationale': 'Simulated write; no deployed transfer.', 'proposed_categories': ['user.health_and_medical.record_id']}]},
            'evidence_record': {'reconciliation': {}, 'agent_analysis': {}, 'coverage': {},
                'fields': [{'entity_id': 'db/patients/record', 'classification': ['user.health_and_medical.record_id'], 'proposal': {}}]},
            'review_queue': {'structural_blockers': [{'kind': 'connection', 'detail': 'No binding', 'resolution_needed': 'Inspect configuration'}],
                             'analysis_errors': ['missing'], 'field_groups': []}}
        data_map, decisions = review.render_outputs(outputs)
        self.assertIn('Simulated write; no deployed transfer.', data_map)
        self.assertIn('not semantic truth', data_map)
        self.assertIn('user.health_and_medical.record_id', decisions)
        self.assertIn('Possible Article 9 or Article 10', decisions)

    def test_scalar_extraction_resolution_does_not_resolve_privacy_uncertainty(self):
        row = self.document['classification_review']['fields'][0]
        decision = self.decision(row)
        decision.update(personal_data='yes', categories=[], uncertainty='category_fit', resolution_owner='human',
            person_association={'kind':'natural_person', 'rationale':'The record has an individual subject.', 'evidence_ids':row['evidence_ids']},
            business_context_question='Which individual action or system outcome does this scalar encode?',
            resolution_needed='The unavailable consuming contract.', decision_impact='Determines category fit.')
        self.assertEqual(field_review.apply_decisions(self.document, [decision], self.repo), [])
        contract = self.document['classification_review']
        gap = {'gap_id':'gap:test', 'kind':'write_value', 'status':'unresolved', 'subject':row['entity_id'], 'evidence_ids':row['evidence_ids']}
        contract['gaps'] = [gap]
        resolution = {'gap_id':'gap:test', 'outcome':'modeled', 'field_ids':[row['entity_id']],
            'rationale':'The scalar expression was inspected independently of its business meaning.',
            'investigation':[{'outcome':'Known scalar; privacy meaning remains unresolved.'}], 'evidence_ids':row['evidence_ids']}
        self.assertEqual(field_review.apply_gap_decisions(self.document, [resolution], self.repo), [])
        self.assertEqual(self.document['classification_review']['gaps'][0]['status'], 'resolved')
        current = self.document['classification_review']['fields'][0]
        self.assertEqual(current['status'], 'needs_investigation')
        self.assertEqual(current['personal_data'], 'yes')
        self.assertTrue(field_review.validate_contract(self.document, self.derived, self.repo))

    def test_budget_validation(self):
        for value in ['0','201','-1']:
            self.assertEqual(self.command('--limit='+value).returncode,2)

    def test_readonly_command_creates_no_sidecar_artifacts(self):
        before={str(path.relative_to(self.repo)) for path in self.repo.rglob('*') if path.is_file()}
        self.assertEqual(self.command().returncode,0)
        after={str(path.relative_to(self.repo)) for path in self.repo.rglob('*') if path.is_file()}
        self.assertEqual(before,after)



    def test_shape_investigation_adds_evidenced_members_without_categories(self):
        import shape_investigation
        row=self.document['classification_review']['fields'][0]
        proposal={'entity_id':row['entity_id'],'observation_digest':row['observation_digest'],
            'rationale':'The inspected contract declares an object with a contact member.',
            'refs':['schema.sql:2'],'type':{'kind':'object','open':True},
            'members':[{'name':'contact','type':{'kind':'scalar','name':'string'},'shape':'string'}]}
        self.assertEqual(shape_investigation.apply(self.document,[proposal],self.repo),[])
        row=self.document['classification_review']['fields'][0]
        self.assertEqual(row['nested_members'][0]['name'],'contact')
        self.assertEqual(row['nested_members'][0]['categories'],[])
        self.assertEqual(row['nested_members'][0]['personal_data'],'unknown')
        self.assertTrue(row['open_content'])
        self.assertEqual(field_review.validate_contract(self.document,self.derived,self.repo,require_complete=False),[])
        field_review.build_contract(self.document,self.repo)
        self.assertEqual(self.document['classification_review']['fields'][0]['nested_members'][0]['name'],'contact')
        field_review.build_contract(self.document,self.repo,fresh=True)
        self.assertEqual(self.document['classification_review']['fields'][0]['nested_members'],[])

    def test_shape_can_establish_a_primitive_array_and_is_source_bound(self):
        import shape_investigation
        row=self.document['classification_review']['fields'][0]
        proposal={'entity_id':row['entity_id'],'observation_digest':row['observation_digest'],
            'rationale':'The inspected writer maps values to strings.','refs':['schema.sql:2'],
            'type':{'kind':'array','items':[{'kind':'scalar','name':'string'}],'open':False},'members':[]}
        self.assertEqual(shape_investigation.apply(self.document,[proposal],self.repo),[])
        row=self.document['classification_review']['fields'][0]
        self.assertTrue(row['scalar_array'])
        self.assertFalse(row['composite'])
        (self.repo/'schema.sql').write_text('CREATE TABLE different (code TEXT);')
        self.assertTrue(field_review.validate_contract(self.document,self.derived,self.repo,require_complete=False))
        field_review.build_contract(self.document,self.repo)
        self.assertEqual(self.document['classification_review']['shape_observations'],{})

    def test_invalid_shapes_are_atomic_and_cannot_classify_or_use_migrations(self):
        import shape_investigation
        row=self.document['classification_review']['fields'][0]
        base={'entity_id':row['entity_id'],'observation_digest':row['observation_digest'],
            'rationale':'Investigated source.','refs':['schema.sql:2'], 'type':{'kind':'object','open':True},'members':[]}
        for patch in ({'observation_digest':'stale'},{'categories':['user.name']},{'refs':['migrations/001.sql:1']}, {'type':{'kind':'made-up'}}):
            before=copy.deepcopy(self.document)
            self.assertTrue(shape_investigation.apply(self.document,[dict(base,**patch)],self.repo))
            self.assertEqual(self.document,before)


    def shape(self, members):
        row = self.document['classification_review']['fields'][0]
        return {'entity_id': row['entity_id'], 'observation_digest': row['observation_digest'],
                'rationale': 'The current typed payload contract declares these members.',
                'refs': ['schema.sql:2'], 'type': {'kind': 'object', 'open': False}, 'members': members}

    def decision(self, row):
        value = field_review.decision_template(row)
        value.update(personal_data='no', categories=[], uncertainty=None, resolution_owner=None,
                     meaning='A non-personal operational value from the fixture contract.',
                     rationale='The typed fixture contract establishes an operational value.',
                     identification_basis='operational', confidence='medium',
                     person_association={'kind': 'none', 'target': None, 'rationale': 'The contract has no person linkage.', 'evidence_ids': row['evidence_ids']},
                     category_fit=[], investigation=[{'question': 'What does the producer store?', 'outcome': 'The typed contract contains operational values.', 'checked_fact_ids': row['supporting_fact_ids']}])
        value["semantic_review"] = semantic_review(field_review, row["evidence_ids"], value["rationale"])
        return value

    def test_repeated_shape_refinement_serializes_preserves_members_and_replays(self):
        import shape_investigation
        first = {'name': 'format', 'type': {'kind': 'scalar', 'name': 'string'}}
        second = {'name': 'version', 'type': {'kind': 'scalar', 'name': 'number'}}
        self.assertEqual(shape_investigation.apply(self.document, [self.shape([first])], self.repo), [])
        leaf = self.document['classification_review']['fields'][0]['nested_members'][0]
        self.assertEqual(field_review.apply_decisions(self.document, [self.decision(leaf)], self.repo), [])
        self.assertEqual(shape_investigation.apply(self.document, [self.shape([second])], self.repo), [])
        rows = self.document['classification_review']['fields'][0]['nested_members']
        self.assertEqual([row['name'] for row in rows], ['format', 'version'])
        self.assertEqual(rows[0]['personal_data'], 'no')
        before = self.document['classification_review']['inventory_digest']
        analysis_storage.save(self.path, self.document)
        self.document = analysis_storage.load(self.path)
        field_review.build_contract(self.document, self.repo)
        self.assertEqual(before, self.document['classification_review']['inventory_digest'])
        self.assertEqual(field_review.validate_contract(self.document, self.derived, self.repo, require_complete=False), [])

    def test_nested_shape_cannot_erase_members_or_hide_classification(self):
        import shape_investigation
        nested = {'name': 'details', 'type': {'kind': 'object', 'open': False},
                  'members': [{'name': 'format', 'type': {'kind': 'scalar', 'name': 'string'}}]}
        self.assertEqual(shape_investigation.apply(self.document, [self.shape([nested])], self.repo), [])
        invalid = self.shape([{'name': 'details', 'type': {'kind': 'scalar', 'name': 'string'}}])
        before = copy.deepcopy(self.document)
        self.assertTrue(shape_investigation.apply(self.document, [invalid], self.repo))
        self.assertEqual(self.document, before)
        for patch in ({'type': {'kind': 'object'}},
                      {'type': {'kind': 'array', 'open': False, 'items': [{'kind': 'scalar', 'name': 'string', 'categories': ['user.name']}]}},
                      {'members': [{'name': [], 'type': {'kind': 'scalar'}}]}):
            invalid = dict(self.shape([]), **patch)
            self.assertTrue(shape_investigation.apply(self.document, [invalid], self.repo))
            self.assertEqual(self.document, before)
        shape = self.document['classification_review']['shape_observations']['db/records/code']['structure']
        shape['categories'] = ['user.name']
        self.assertTrue(field_review.validate_contract(self.document, self.derived, self.repo, require_complete=False))

    def test_shape_members_cannot_be_removed_by_rewriting_inventory_digest(self):
        import shape_investigation
        self.assertEqual(shape_investigation.apply(self.document, [self.shape([
            {'name': 'contact', 'type': {'kind': 'scalar', 'name': 'string'}}])], self.repo), [])
        contract = self.document['classification_review']
        parent = contract['fields'][0]
        parent['nested_members'] = []
        parent['type'].pop('members', None)
        contract['inventory_digest'] = field_review.digest([(row['entity_id'], row['observation_digest'])
                                                           for row in field_review.all_rows(contract['fields'])])
        errors = field_review.validate_contract(self.document, self.derived, self.repo, require_complete=False)
        self.assertTrue(any('missing from classification coverage' in error for error in errors), errors)
        self.assertTrue(field_review.apply_decisions(self.document, [self.decision(parent)], self.repo))

    def test_refined_shapes_restore_from_accepted_certificate(self):
        import hashlib
        import reconcile
        import shape_investigation
        for name in ('format', 'version'):
            self.assertEqual(shape_investigation.apply(self.document, [self.shape([
                {'name': name, 'type': {'kind': 'scalar', 'name': 'string'}}])], self.repo), [])
        decisions = [self.decision(row) for row in field_review.all_rows(self.document['classification_review']['fields']) if not row['nested_members']]
        self.assertEqual(field_review.apply_decisions(self.document, decisions, self.repo), [])
        manifest = {'dataset': [{'fides_key': 'db', 'collections': [{'name': 'records', 'fields': []}]}]}
        field_review.project_candidate(self.document, manifest)
        path = self.repo / '.noru/privacy-datamap.yml'
        path.write_text(reconcile.to_yaml(manifest))
        certificate = field_review.classification_certificate(self.document, self.derived, self.repo, manifest)
        (self.repo / '.noru/privacy-datamap.lock.json').write_text(json.dumps({
            'accepted_manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'classification_review': certificate}))
        del self.document['classification_review']
        field_review.build_contract(self.document, self.repo)
        self.assertEqual([row['name'] for row in self.document['classification_review']['fields'][0]['nested_members']], ['format', 'version'])
        self.assertEqual(field_review.validate_contract(self.document, self.derived, self.repo, candidate=manifest), [])
        from jsonschema_mini import validate
        schema = json.loads((ROOT / 'contract/privacy-datamap-analysis.schema.json').read_text())
        contract_schema = dict(schema['$defs']['classification_review'], **{'$defs': schema['$defs']})
        self.assertEqual(validate(self.document['classification_review'], contract_schema), [])
        schema = json.loads((ROOT / 'contract/privacy-datamap-lock.schema.json').read_text())
        contract_schema = dict(schema['properties']['classification_review'], **{'$defs': schema['$defs']})
        self.assertEqual(validate(certificate, contract_schema), [])
        field_review.build_contract(self.document, self.repo, fresh=True)
        self.assertEqual(self.document['classification_review']['shape_observations'], {})
        self.assertTrue(field_review.validate_contract(self.document, self.derived, self.repo))

    def test_complete_parent_and_leaf_batch_aggregates_before_validation(self):
        import shape_investigation
        proposal = self.shape([{'name': 'details', 'type': {'kind': 'object', 'open': False},
                               'members': [{'name': 'format', 'type': {'kind': 'scalar', 'name': 'string'}}]}])
        proposal['type']['open'] = True
        self.assertEqual(shape_investigation.apply(self.document, [proposal], self.repo), [])
        rows = {row['entity_id']: row for row in field_review.all_rows(self.document['classification_review']['fields'])}
        parent = self.decision(rows['db/records/code'])
        parent['open_content_resolution'] = {'outcome': 'bounded_content_contract', 'rationale': 'The source contract bounds additional keys to operational extensions.', 'evidence_ids': rows['db/records/code']['evidence_ids']}
        leaf = self.decision(rows['db/records/code.details.format'])
        before = copy.deepcopy(self.document)
        # A malformed leaf still fails atomically before aggregation can use it.
        invalid = dict(leaf, categories=[{}])
        self.assertTrue(field_review.apply_decisions(self.document, [parent, invalid], self.repo))
        self.assertEqual(self.document, before)
        self.assertEqual(field_review.apply_decisions(self.document, [parent, leaf], self.repo), [])
        rows = {row['entity_id']: row for row in field_review.all_rows(self.document['classification_review']['fields'])}
        self.assertEqual(rows['db/records/code.details']['decision_origin'], 'member-aggregation-v1')
        self.assertEqual(rows['db/records/code']['status'], 'proposed')

    def dotted_review_fixture(self, shorter_prefix_present=False):
        import shape_investigation
        first_name = 'payload' if shorter_prefix_present else 'other'
        (self.repo / 'schema.sql').write_text(
            f'CREATE TABLE records (\n  {first_name} TEXT,\n  "payload.details" JSON\n);\n')
        fields = self.derived['datasets'][0]['collections'][0]['fields']
        for field, name in zip(fields, [first_name, 'payload.details']):
            field.update(name=name, entity_id='db/records/' + name)
        fields[1]['shape'] = 'JSON'
        field_review.build_contract(self.document, self.repo,
            compiler_report={'tables': [], 'gaps': []}, fresh=True)
        root = next(row for row in field_review.all_rows(self.document['classification_review']['fields'])
                    if row['entity_id'] == 'db/records/payload.details')
        proposal = {'entity_id': root['entity_id'], 'observation_digest': root['observation_digest'],
            'rationale': 'The fixture declares a nested object under a literal dotted field name.',
            'refs': ['schema.sql:3'], 'type': {'kind': 'object', 'open': False},
            'members': [{'name': 'entry', 'type': {'kind': 'object', 'open': False},
                         'members': [{'name': 'value', 'type': {'kind': 'scalar', 'name': 'string'}}]}]}
        self.assertEqual(shape_investigation.apply(self.document, [proposal], self.repo), [])
        return {'actions': [], 'collection_review_required': [], 'observation_digest': 'fixture'}

    def test_review_members_inherit_nearest_observed_canonical_ancestor(self):
        import review
        for shorter_prefix_present in [True, False]:
            with self.subTest(shorter_prefix_present=shorter_prefix_present):
                result = self.dotted_review_fixture(shorter_prefix_present)
                before = copy.deepcopy(self.document)
                output = review.build_outputs(self.document, result, self.derived, {'dataset': [], 'system': []}, [])
                inventory = {row['entity_id']: row for row in output['evidence_record']['fields']}
                for identity in ['db/records/payload.details', 'db/records/payload.details.entry',
                                 'db/records/payload.details.entry.value']:
                    self.assertEqual(inventory[identity]['dataset_key'], 'db')
                    self.assertEqual(inventory[identity]['collection_id'], 'db/records')
                    self.assertEqual(inventory[identity]['field'], 'payload.details')
                    self.assertEqual(inventory[identity]['shape'], 'JSON')
                    self.assertEqual(inventory[identity]['refs'], ['schema.sql:3'])
                self.assertEqual(output['data_map']['stores'][0]['field_count'], len(inventory))
                self.assertEqual(self.document, before)

    def test_review_rejects_missing_observed_root_even_with_a_shorter_prefix(self):
        import review
        result = self.dotted_review_fixture()
        document = copy.deepcopy(self.document)
        document['classification_review']['fields'] = [row for row in document['classification_review']['fields']
                                                       if row['entity_id'] == 'db/records/payload.details']
        derived = copy.deepcopy(self.derived)
        fields = derived['datasets'][0]['collections'][0]['fields']
        fields.pop(1)
        fields[0].update(name='payload', entity_id='db/records/payload')
        with self.assertRaisesRegex(ValueError, r'db/records/payload\.details: canonical field has no matching observed field'):
            review.build_outputs(document, result, derived, {'dataset': [], 'system': []}, [])

    def test_recursive_container_coverage_is_explicit_and_keeps_unknown_members(self):
        import review
        child = {'entity_id': 'db/records/preferences.theme', 'status': 'proposed',
                 'personal_data': 'yes', 'categories': ['user.account.settings']}
        parent = {'entity_id': 'db/records/preferences', 'composite': True, 'status': 'proposed',
                  'categories': [], 'nested_members': [child]}
        before = copy.deepcopy(parent)
        self.assertEqual(review.member_coverage(parent)['state'], 'reviewed members')
        self.assertEqual(review.member_coverage(parent)['categories'], ['user.account.settings'])
        self.assertEqual(parent, before)
        metadata = dict(parent, nested_members=[
            dict(child, entity_id='db/records/metadata.uploader_name', categories=['user.name']),
            dict(child, entity_id='db/records/metadata.uploaded_at', categories=['user.behavior'])])
        self.assertEqual(review.member_coverage(metadata)['categories'], ['user.behavior', 'user.name'])
        self.assertEqual(review.member_coverage(metadata)['state'], 'reviewed members')
        parent['nested_members'] = [dict(parent, entity_id='db/records/preferences.interface')]
        self.assertEqual(review.member_coverage(parent)['state'], 'reviewed members')
        child['status'] = 'needs_investigation'
        self.assertEqual(review.member_coverage(parent)['pending_members'], [child['entity_id']])
        self.assertEqual(review.member_coverage(parent)['state'], 'member review incomplete')
        child['status'] = 'proposed'
        child.update(open_content=True, open_content_resolution={'rationale': 'Not a resolution.'})
        self.assertEqual(review.member_coverage(parent)['pending_members'], [child['entity_id']])
        child['open_content'] = False
        parent.update(open_content=True)
        self.assertEqual(review.member_coverage(parent)['state'], 'member review incomplete')
        parent.update(nested_members=[], open_content=False)
        self.assertEqual(review.member_coverage(parent)['state'], 'member review incomplete')

    def test_rendered_review_explains_container_categories(self):
        import review
        root = self.document['classification_review']['fields'][0]
        root.update(composite=True, status='proposed', personal_data='yes', categories=[], open_content=False,
                    nested_members=[dict(root, entity_id=root['entity_id']+'.theme', composite=False,
                        status='proposed', personal_data='yes', categories=['user.account.settings'], nested_members=[])])
        result = {'actions': [], 'collection_review_required': [], 'observation_digest': 'fixture'}
        output = review.build_outputs(self.document, result, self.derived, {'dataset': [], 'system': []}, [])
        rendered, _ = review.render_outputs(output)
        self.assertIn('Nested field coverage', rendered)
        self.assertIn('reviewed members (1 members)', rendered)
        self.assertIn('Member categories: user.account.settings', rendered)
        self.assertEqual(output['evidence_record']['fields'][0]['classification'], [])
        root['nested_members'][0]['status'] = 'needs_investigation'
        output = review.build_outputs(self.document, result, self.derived, {'dataset': [], 'system': []}, [])
        self.assertIn('member review incomplete', review.render_outputs(output)[0])
        self.assertIn('Pending members:', review.render_outputs(output)[0])

    def test_grouped_review_retains_each_question_resolution_and_impact(self):
        import review
        for row, question, answer, impact in zip(self.document['classification_review']['fields'],
                ['Does this value identify a person?', 'Can this text contain personal content?'],
                ['Confirm the identifier assignment policy.', 'Confirm the permitted text contract.'],
                ['Changes the identifier classification.', 'Changes the content classification.']):
            row.update(decision_id='shared-decision', decision_summary='Resolve the remaining contract questions.',
                       resolution_owner='human', business_context_question=question,
                       resolution_needed=answer, decision_impact=impact)
        result = {'actions': [], 'collection_review_required': [], 'observation_digest': 'fixture'}
        output = review.build_outputs(self.document, result, self.derived, {'dataset': [], 'system': []}, [])
        groups = output['review_queue']['field_groups']
        self.assertEqual(len(groups), 1)
        lines = []
        review.question(lines, groups[0])
        rendered = '\n'.join(lines)
        for row in self.document['classification_review']['fields']:
            for key in ('business_context_question', 'resolution_needed', 'decision_impact'):
                self.assertEqual(rendered.count(row[key]), 1)
        # Duplicate text is emitted once even if several fields share it.
        self.document['classification_review']['fields'][1]['business_context_question'] = self.document['classification_review']['fields'][0]['business_context_question']
        output = review.build_outputs(self.document, result, self.derived, {'dataset': [], 'system': []}, [])
        lines = []
        review.question(lines, output['review_queue']['field_groups'][0])
        self.assertEqual('\n'.join(lines).count('Does this value identify a person?'), 1)

if __name__=='__main__':
    unittest.main()
