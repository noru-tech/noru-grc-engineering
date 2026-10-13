#!/usr/bin/env python3
"""Independent consistency/freshness gates for the canonical classification contract."""
import copy
import importlib.util
import pathlib
import sys
from privacy_semantic_test_support import semantic_review
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'plugins/privacy-datamap/scripts'))
import field_review
import analysis_storage


class ClassificationGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = pathlib.Path(self.temp.name)
        self.source = 'export const records = pgTable("records", {email: text("email"), enabled: boolean("enabled"), createdAt: timestamp("created_at"), payload: jsonb("payload")});\ninterface Person { email: string; identifier: string }\nfunction receive(person: Person) { db.insert(records).values({email: person.email}); }\n'
        (self.repo / 'schema.ts').write_text(self.source)
        span = field_review.span_from_ref(self.repo, 'schema.ts:1')
        self.compiler = {'adapter': {'name': 'synthetic-facts', 'version': 1}, 'tables': [{'name': 'records', 'symbol_id': 'schema.ts#records', 'fields': [
            {'name': 'email', 'property': 'email', 'field_id': 'schema.ts#records.email', 'shape': 'text("email")', 'type': {'kind': 'scalar', 'name': 'string'}, 'evidence': [span], 'writes': [{'kind': 'insert', 'value': {'kind': 'reference', 'expression': 'person.email'}, 'evidence': [field_review.span_from_ref(self.repo, 'schema.ts:3')]}]},
            {'name': 'enabled', 'property': 'enabled', 'field_id': 'schema.ts#records.enabled', 'shape': 'boolean("enabled")', 'type': {'kind': 'scalar', 'name': 'boolean'}, 'evidence': [span]},
            {'name': 'created_at', 'property': 'createdAt', 'field_id': 'schema.ts#records.createdAt', 'shape': 'timestamp("created_at")', 'type': {'kind': 'scalar', 'name': 'Date'}, 'evidence': [span]},
            {'name': 'payload', 'property': 'payload', 'field_id': 'schema.ts#records.payload', 'shape': 'jsonb("payload")', 'type': {'kind': 'object', 'members': [{'name': 'actor'}, {'name': 'checksum'}]}, 'evidence': [span], 'members': [
                {'name': 'actor', 'shape': 'string', 'type': {'kind': 'scalar', 'name': 'string'}, 'evidence': [span]},
                {'name': 'checksum', 'shape': 'string', 'type': {'kind': 'scalar', 'name': 'string'}, 'evidence': [span]}]}]}], 'facts': [], 'gaps': []}
        self.derived = {'datasets': [{'fides_key': 'db', 'collections': [{'name': 'records', 'fields': [
            {'name': field['name'], 'entity_id': 'db/records/' + field['name'], 'shape': field['shape'], 'refs': ['schema.ts:1']}
            for field in self.compiler['tables'][0]['fields']]}]}]}
        self.document = {'observation_digest': 'synthetic-current-structure', 'preview': {'derived': self.derived}, 'proposals': []}
        field_review.build_contract(self.document, self.repo, self.compiler, fresh=True)

    def row(self, suffix):
        return next(row for row in field_review.all_rows(self.document['classification_review']['fields']) if row['entity_id'] == 'db/records/' + suffix)

    def decision(self, suffix, personal='no', categories=None, association=None):
        row = self.row(suffix)
        value = field_review.decision_template(row)
        value.update(personal_data=personal, categories=categories or [], meaning='A source-evidenced ' + suffix + ' value.', rationale='Examined the declaration and the recorded producer; this judgment follows that contract.',
                     uncertainty=None, resolution_owner=None, identification_basis='direct' if personal == 'yes' else 'operational',
                     person_association={'kind': association or ('natural_person' if personal == 'yes' else 'none'), 'target': 'Person' if personal == 'yes' else None, 'rationale': 'The producer contract establishes the subject independently of the storage name.', 'evidence_ids': row['evidence_ids']},
                     category_fit=[{'category': category, 'rationale': field_review.TAXONOMY[category] + ' The recorded producer supplies this value.', 'evidence_ids': row['evidence_ids']} for category in categories or []],
                     investigation=[{'question': 'Which value does the producer store?', 'checked_fact_ids': row['supporting_fact_ids'], 'outcome': 'The declaration and producer were inspected.'}], confidence='medium')
        value["semantic_review"] = semantic_review(field_review, row["evidence_ids"], value["rationale"])
        return value

    def resolve(self):
        decisions = [self.decision('email', 'yes', ['user.contact.email']), self.decision('enabled'), self.decision('created_at'), self.decision('payload.actor', 'yes', ['user.unique_id']), self.decision('payload.checksum')]
        self.assertEqual(field_review.apply_decisions(self.document, decisions, self.repo), [])

    def test_canonical_storage_and_fresh_ignores_prior_labels(self):
        self.document['proposals'] = [{'entity_id': 'db/records/email', 'proposed_categories': ['user.name']}]
        first = field_review.build_contract(self.document, self.repo, self.compiler, fresh=True)
        self.assertFalse(first['prior_field_decisions_used'])
        self.assertEqual(self.row('email')['personal_data'], 'unknown')
        self.assertEqual(self.row('email')['categories'], [])
        path = self.repo / 'analysis.json'
        analysis_storage.save(path, self.document)
        self.assertEqual(analysis_storage.load(path)['classification_review'], first)

    def test_all_nested_members_are_accounted_for(self):
        self.assertEqual(len(list(field_review.all_rows(self.document['classification_review']['fields']))), 6)
        self.assertEqual(self.row('payload.actor')['parent_id'], 'db/records/payload')
        errors = field_review.validate_contract(self.document, self.derived, self.repo)
        self.assertTrue(any('members need plugin investigation' in error for error in errors))

    def test_semantic_dimensions_are_required_and_atomic(self):
        for key in field_review.SEMANTIC_REVIEW_QUESTIONS:
            decision = self.decision('email', 'yes', ['user.contact.email'])
            del decision['semantic_review'][key]
            before = copy.deepcopy(self.document)
            errors = field_review.apply_decisions(self.document, [decision], self.repo)
            self.assertTrue(any(key in error for error in errors))
            self.assertEqual(self.document, before)

    def test_semantic_dimensions_need_current_citations(self):
        decision = self.decision('email', 'yes', ['user.contact.email'])
        decision['semantic_review']['security_and_transformations']['evidence_ids'] = ['missing']
        self.assertTrue(any('current evidence' in e for e in field_review.apply_decisions(self.document, [decision], self.repo)))

    def test_category_uncertainty_preserves_person_association(self):
        decision = self.decision('email', 'yes', [])
        decision.update(uncertainty='category_fit', resolution_owner='human',
                        business_context_question='Which contact protocol consumes this value?',
                        resolution_needed='The unavailable protocol contract.',
                        decision_impact='Determines category fit, not established personal association.')
        self.assertEqual(field_review.apply_decisions(self.document, [decision], self.repo), [])
        self.assertEqual(self.row('email')['personal_data'], 'yes')
        for personal in ['no', 'unknown']:
            changed = dict(decision, personal_data=personal)
            self.assertTrue(any('preserve established' in e for e in field_review.apply_decisions(self.document, [changed], self.repo)))

    def test_service_secret_can_be_security_sensitive_without_being_personal(self):
        decision = self.decision('enabled', association='service')
        decision['security_sensitivity'] = 'credential'
        self.assertEqual(field_review.apply_decisions(self.document, [decision], self.repo), [])
        self.assertEqual(self.row('enabled')['personal_data'], 'no')
        self.assertEqual(self.row('enabled')['security_sensitivity'], 'credential')

    def test_names_alone_do_not_classify(self):
        self.assertEqual(self.row('email')['status'], 'needs_investigation')
        self.assertEqual(self.row('email')['personal_data'], 'unknown')
        self.assertEqual(self.row('created_at')['personal_data'], 'unknown')
        self.assertEqual(self.row('created_at')['decision_origin'], 'unresolved')

    def test_atomic_apply_and_boolean_identifier_rejected(self):
        before = copy.deepcopy(self.document)
        errors = field_review.apply_decisions(self.document, [self.decision('email', 'yes', ['user.contact.email']), self.decision('enabled', 'yes', ['user.unique_id'])], self.repo)
        self.assertTrue(any('boolean state' in error for error in errors))
        self.assertEqual(self.document, before)

    def test_organization_or_service_is_not_person_by_secrecy(self):
        for association in ['organization', 'service']:
            errors = field_review.apply_decisions(self.document, [self.decision('email', 'yes', ['user.contact.email'], association)], self.repo)
            self.assertTrue(any('association' in error for error in errors))

    def test_empty_categories_do_not_mean_non_personal(self):
        decision = self.decision('email', 'yes', [])
        self.assertTrue(any('without category fit' in error for error in field_review.apply_decisions(self.document, [decision], self.repo)))
        self.assertEqual(self.row('email')['personal_data'], 'unknown')

    def test_complete_children_aggregate_and_export_exactly(self):
        self.resolve()
        self.assertEqual(self.row('payload')['personal_data'], 'mixed')
        self.assertEqual(self.row('payload')['categories'], [])
        manifest = {'dataset': [{'fides_key': 'db', 'collections': [{'name': 'records', 'fields': []}]}]}
        field_review.project_candidate(self.document, manifest)
        self.assertEqual(field_review.validate_contract(self.document, self.derived, self.repo, candidate=manifest), [])
        self.assertEqual(len(manifest['dataset'][0]['collections'][0]['fields'][-1]['fields']), 2)
        manifest['dataset'][0]['collections'][0]['fields'][-1]['fields'].pop()
        self.assertTrue(any('coverage differs' in error for error in field_review.validate_contract(self.document, self.derived, self.repo, candidate=manifest)))

    def test_parent_cannot_hide_unresolved_child(self):
        decision = self.decision('payload', 'no')
        errors = field_review.apply_decisions(self.document, [decision], self.repo)
        self.assertTrue(any('unresolved members' in error for error in errors))

    def test_parent_cannot_dismiss_known_personal_child(self):
        self.assertEqual(field_review.apply_decisions(self.document, [self.decision('payload.actor', 'yes', ['user.unique_id'])], self.repo), [])
        errors = field_review.apply_decisions(self.document, [self.decision('payload')], self.repo)
        self.assertTrue(any('contradicts personal members' in error for error in errors))

    def test_removed_nested_member_cannot_rewrite_inventory_digest(self):
        self.row('payload')['nested_members'].pop()
        contract = self.document['classification_review']
        contract['inventory_digest'] = field_review.digest([(row['entity_id'], row['observation_digest']) for row in field_review.all_rows(contract['fields'])])
        self.assertTrue(any('compiler type evidence' in error for error in field_review.validate_contract(self.document, self.derived, self.repo)))

    def test_changed_and_new_sources_invalidate(self):
        self.resolve()
        (self.repo / 'new-writer.ts').write_text('db.insert(records).values(input);\n')
        self.assertTrue(any('inventory changed' in error for error in field_review.validate_contract(self.document, self.derived, self.repo)))
        (self.repo / 'new-writer.ts').unlink()
        (self.repo / 'schema.ts').write_text(self.source + '// source changed\n')
        self.assertTrue(any('evidence changed' in error for error in field_review.validate_contract(self.document, self.derived, self.repo)))

    def test_unchanged_decisions_carry_but_fresh_discards_them(self):
        self.resolve()
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertEqual(self.row('email')['personal_data'], 'yes')
        self.assertTrue(self.document['classification_review']['prior_field_decisions_used'])
        field_review.build_contract(self.document, self.repo, self.compiler, fresh=True)
        self.assertEqual(self.row('email')['personal_data'], 'unknown')

    def test_code_questions_cannot_be_silently_sent_to_human(self):
        decision = field_review.decision_template(self.row('email'))
        decision['resolution_owner'] = 'human'
        self.assertTrue(any('human context' in error for error in field_review.apply_decisions(self.document, [decision], self.repo)))

    def test_migration_evidence_is_rejected(self):
        for name in ['migrations/001.sql', 'custom-migrations/001.sql', 'alembic/versions/001.py', 'drizzle/001.sql', 'V002__change.sql', 'db/changelog/001.xml', 'schema.migration.ts']:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('not current schema\n')
            self.assertIsNone(field_review.span_from_ref(self.repo, name + ':1'), name)

    def test_missing_canonical_contract_never_passes(self):
        self.document.pop('classification_review')
        self.assertTrue(field_review.validate_contract(self.document, self.derived, self.repo))

    def test_edited_decision_requires_rebinding(self):
        self.resolve()
        self.row('email')['categories'] = ['user.name']
        self.assertTrue(any('unbound or modified' in error for error in field_review.validate_contract(self.document, self.derived, self.repo)))

    def test_gap_resolution_is_an_evidenced_plugin_operation(self):
        report = copy.deepcopy(self.compiler)
        report['gaps'] = [{'kind': 'unsupported_consumer', 'subject': 'generated', 'message': 'A consumer was not parsed.', 'evidence': [field_review.span_from_ref(self.repo, 'schema.ts:3')]}]
        field_review.build_contract(self.document, self.repo, report, fresh=True)
        gap = self.document['classification_review']['gaps'][0]
        self.assertTrue(field_review.apply_gap_decisions(self.document, [{'gap_id': gap['gap_id'], 'outcome': 'not_applicable'}], self.repo))
        decision = {'gap_id': gap['gap_id'], 'outcome': 'not_applicable', 'rationale': 'Inspected the consumer contract.', 'scope_rationale': 'This consumer has no storage write; all stored members are declared.', 'evidence_ids': gap['evidence_ids'], 'investigation': [{'question': 'Does this consumer write?', 'outcome': 'Inspected its body.'}]}
        self.assertEqual(field_review.apply_gap_decisions(self.document, [decision], self.repo), [])
        self.assertEqual(self.document['classification_review']['gaps'][0]['status'], 'resolved')

    def duplicate_gap_fixture(self):
        report = copy.deepcopy(self.compiler)
        gap = {'kind': 'unsupported_consumer', 'subject': 'generated', 'message': 'A consumer was not parsed.',
               'evidence': [field_review.span_from_ref(self.repo, 'schema.ts:3')]}
        report['gaps'] = [gap, copy.deepcopy(gap)]
        field_review.build_contract(self.document, self.repo, report, fresh=True)
        gap = self.document['classification_review']['gaps'][0]
        decision = {'gap_id': gap['gap_id'], 'outcome': 'not_applicable',
                    'rationale': 'Inspected the complete consumer contract.',
                    'scope_rationale': 'This consumer has no storage write; stored members are declared.',
                    'evidence_ids': gap['evidence_ids'],
                    'investigation': [{'question': 'Does the consumer write?', 'outcome': 'Its body only reads.'}]}
        return report, decision

    def test_duplicate_gap_observations_receive_one_atomic_disposition(self):
        report, decision = self.duplicate_gap_fixture()
        before = copy.deepcopy(self.document)
        self.assertTrue(field_review.apply_gap_decisions(self.document,
            [decision, dict(decision, gap_id='gap:unknown')], self.repo))
        self.assertEqual(self.document, before)
        self.assertEqual(field_review.apply_gap_decisions(self.document, [decision], self.repo), [])
        gaps = self.document['classification_review']['gaps']
        self.assertEqual(len(gaps), 2)
        self.assertEqual([gap['status'] for gap in gaps], ['resolved', 'resolved'])
        self.assertEqual(gaps[0], gaps[1])
        self.assertIsNot(gaps[0]['resolution'], gaps[1]['resolution'])
        self.assertEqual(field_review.validate_contract(self.document, self.derived, self.repo, require_complete=False), [])
        field_review.build_contract(self.document, self.repo, report)
        self.assertEqual([gap['status'] for gap in self.document['classification_review']['gaps']], ['resolved', 'resolved'])
        decision['outcome'] = 'unresolved'
        self.assertEqual(field_review.apply_gap_decisions(self.document, [decision], self.repo), [])
        self.assertEqual([gap['status'] for gap in self.document['classification_review']['gaps']], ['unresolved', 'unresolved'])

    def test_conflicting_duplicate_gap_identity_cannot_hide_an_observation(self):
        _report, decision = self.duplicate_gap_fixture()
        self.document['classification_review']['gaps'][1]['message'] = 'A different source limitation.'
        before = copy.deepcopy(self.document)
        errors = field_review.apply_gap_decisions(self.document, [decision], self.repo)
        self.assertTrue(any('conflicting observations' in error for error in errors), errors)
        self.assertEqual(self.document, before)

    def accepted_fixture(self, resolve=True):
        import reconcile
        if resolve:
            self.resolve()
        manifest = {'dataset': [{'fides_key': 'db', 'collections': [{'name': 'records', 'fields': []}]}]}
        field_review.project_candidate(self.document, manifest)
        folder = self.repo / '.noru'
        folder.mkdir(exist_ok=True)
        path = folder / 'privacy-datamap.yml'
        path.write_text(reconcile.to_yaml(manifest))
        certificate = field_review.classification_certificate(self.document, self.derived, self.repo, manifest)
        import hashlib, json
        (folder / 'privacy-datamap.lock.json').write_text(json.dumps({'accepted_manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'classification_review': certificate}))
        return manifest

    def test_clean_cache_keeps_current_accepted_certificate(self):
        manifest = self.accepted_fixture()
        self.assertEqual(field_review.validate_repository({}, self.derived, self.repo, candidate=manifest), [])
        (self.repo / 'new.ts').write_text('const changed = true;')
        self.assertTrue(field_review.validate_repository({}, self.derived, self.repo, candidate=manifest))

    def test_present_fresh_contract_cannot_use_accepted_certificate(self):
        manifest = self.accepted_fixture()
        field_review.build_contract(self.document, self.repo, self.compiler, fresh=True)
        self.assertTrue(field_review.validate_repository(self.document, self.derived, self.repo, candidate=manifest))
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertEqual(self.row('email')['personal_data'], 'unknown')

    def test_incomplete_decisions_cannot_create_certificate(self):
        with self.assertRaises(ValueError):
            field_review.classification_certificate(self.document, self.derived, self.repo, {'dataset': []})

    def test_current_certificate_restores_only_matching_manifest(self):
        manifest = self.accepted_fixture()
        self.document.pop('classification_review')
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertEqual(self.row('email')['personal_data'], 'yes')
        self.assertEqual(field_review.validate_contract(self.document, self.derived, self.repo, candidate=manifest), [])
        manifest['dataset'][0]['collections'][0]['fields'][0]['data_categories'] = ['user.name']
        self.assertTrue(field_review.accepted_certificate_errors(self.derived, self.repo, manifest))

    def test_deleted_cache_restores_adjudicated_open_content_and_gaps(self):
        self.compiler['tables'][0]['fields'][-1]['type']['open'] = True
        self.compiler['gaps'] = [{'kind': 'external_contract', 'subject': 'records.payload', 'message': 'A bounded upstream payload needs investigation.', 'evidence': [field_review.span_from_ref(self.repo, 'schema.ts:3')]}]
        field_review.build_contract(self.document, self.repo, self.compiler, fresh=True)
        self.resolve()
        parent = self.decision('payload', 'mixed', [], 'mixed')
        parent['open_content_resolution'] = {'outcome': 'bounded_content_contract', 'rationale': 'The synthetic producer contract bounds additional keys to non-personal values.', 'evidence_ids': self.row('payload')['evidence_ids']}
        self.assertEqual(field_review.apply_decisions(self.document, [parent], self.repo), [])
        gap = self.document['classification_review']['gaps'][0]
        answer = {'gap_id': gap['gap_id'], 'outcome': 'modeled', 'field_ids': ['db/records/payload'], 'rationale': 'The producer contract and each represented member were investigated.', 'evidence_ids': gap['evidence_ids'], 'investigation': [{'question': 'Is the bounded content modeled?', 'outcome': 'All members and bounded extension content have recorded decisions.'}]}
        self.assertEqual(field_review.apply_gap_decisions(self.document, [answer], self.repo), [])
        manifest = self.accepted_fixture(resolve=False)
        self.document.pop('classification_review')
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertEqual(field_review.validate_contract(self.document, self.derived, self.repo, candidate=manifest), [])
        self.assertEqual(self.row('payload')['open_content_resolution']['outcome'], 'accepted_certificate')
        self.assertEqual(self.document['classification_review']['gaps'][0]['resolution']['outcome'], 'accepted_certificate')
        # Repeated normal discovery carries the attestation; fresh still discards all acceptance.
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertEqual(field_review.validate_contract(self.document, self.derived, self.repo, candidate=manifest), [])
        field_review.build_contract(self.document, self.repo, self.compiler, fresh=True)
        self.assertTrue(field_review.validate_contract(self.document, self.derived, self.repo, candidate=manifest))

    def test_accepted_certificate_cannot_restore_changed_extraction_gap_set(self):
        self.accepted_fixture()
        self.document.pop('classification_review')
        self.compiler['gaps'] = [{'kind': 'new_gap', 'subject': 'unknown', 'message': 'A different extracted coverage limitation.', 'evidence': [field_review.span_from_ref(self.repo, 'schema.ts:3')]}]
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertEqual(self.row('email')['personal_data'], 'unknown')
        self.assertTrue(field_review.validate_contract(self.document, self.derived, self.repo))

    def real_writer_case(self, declared, payload, parameter="email:string"):
        source="import {pgTable,jsonb} from 'drizzle-orm/pg-core';\nconst records=pgTable('records',{payload:jsonb('payload').$type<"+declared+">()});\nfunction store(db:any,"+parameter+"){const payload="+payload+";return db.insert(records).values({payload});}\n"
        (self.repo/'schema.ts').write_text(source)
        self.compiler=field_review.compiler_facts(self.repo)
        field=self.compiler['tables'][0]['fields'][0]
        self.derived={'datasets':[{'fides_key':'db','collections':[{'name':'records','fields':[{'name':'payload','entity_id':'db/records/payload','shape':field['shape'],'refs':['schema.ts:2']}]}]}]}
        self.document={'observation_digest':'real-writer-structure','preview':{'derived':self.derived}}
        field_review.build_contract(self.document,self.repo,self.compiler,fresh=True)

    def test_writer_members_missing_from_declared_type_are_modeled(self):
        self.real_writer_case('{checksum:string}',"{checksum:'fixed',email}")
        self.assertEqual({row['name'] for row in self.row('payload')['nested_members']},{'checksum','email'})
        self.assertEqual(field_review.apply_decisions(self.document,[self.decision('payload.checksum')],self.repo),[])
        self.assertEqual(self.row('payload')['status'],'needs_investigation')
        self.assertTrue(field_review.validate_contract(self.document,self.derived,self.repo))
        self.assertEqual(field_review.apply_decisions(self.document,[self.decision('payload.email','yes',['user.contact.email'])],self.repo),[])
        self.assertEqual(self.row('payload')['personal_data'],'mixed')
        self.assertEqual(field_review.validate_contract(self.document,self.derived,self.repo),[])

    def test_extra_writer_members_are_modeled_recursively(self):
        self.real_writer_case('{account:{checksum:string}}',"{account:{checksum:'fixed',email}}")
        self.assertEqual({row['name'] for row in self.row('payload.account')['nested_members']},{'checksum','email'})
        self.assertEqual(self.row('payload.account.email')['status'],'needs_investigation')

    def test_typed_parameter_is_not_exact_runtime_json_shape(self):
        self.real_writer_case('{checksum:string}','input',parameter='input:{checksum:string}')
        self.assertTrue(self.row('payload')['open_content'])
        self.assertIn('runtime_object_shape',{gap['kind'] for gap in self.document['classification_review']['gaps']})
        self.assertEqual(field_review.apply_decisions(self.document,[self.decision('payload.checksum')],self.repo),[])
        self.assertEqual(self.row('payload')['status'],'needs_investigation')
        self.assertTrue(field_review.validate_contract(self.document,self.derived,self.repo))

    def test_positive_composite_cannot_export_without_personal_member_categories(self):
        self.assertEqual(field_review.apply_decisions(self.document, [self.decision('payload.actor'), self.decision('payload.checksum')], self.repo), [])
        parent = self.decision('payload', 'yes', [], 'natural_person')
        errors = field_review.apply_decisions(self.document, [parent], self.repo)
        self.assertTrue(any('categorized personal members' in error for error in errors))

    def test_scalar_array_values_can_carry_supported_categories(self):
        for declaration in ["jsonb('emails').$type<string[]>()", "text('emails').array()"]:
            source = "import {pgTable,jsonb,text} from 'drizzle-orm/pg-core';\nconst records=pgTable('records',{emails:" + declaration + "});\ninterface Person { emails: string[] }; function store(db:any,person:Person){db.insert(records).values({emails:person.emails});}\n"
            (self.repo / 'schema.ts').write_text(source)
            report = field_review.compiler_facts(self.repo)
            field = report['tables'][0]['fields'][0]
            self.derived = {'datasets': [{'fides_key': 'db', 'collections': [{'name': 'records', 'fields': [{'name': 'emails', 'entity_id': 'db/records/emails', 'shape': field['shape'], 'refs': ['schema.ts:2']}]}]}]}
            self.document = {'observation_digest': 'array-structure', 'preview': {'derived': self.derived}}
            field_review.build_contract(self.document, self.repo, report, fresh=True)
            row = self.row('emails')
            self.assertTrue(row['scalar_array'], declaration)
            self.assertFalse(row['composite'], declaration)
            self.assertEqual(row['nested_members'], [])
            self.assertEqual(field_review.apply_decisions(self.document, [self.decision('emails', 'yes', ['user.contact.email'])], self.repo), [])
            manifest = {'dataset': [{'fides_key': 'db', 'collections': [{'name': 'records', 'fields': []}]}]}
            field_review.project_candidate(self.document, manifest)
            self.assertEqual(field_review.validate_contract(self.document, self.derived, self.repo, candidate=manifest), [])
            exported = manifest['dataset'][0]['collections'][0]['fields'][0]
            self.assertEqual(exported['data_categories'], ['user.contact.email'])
            self.assertFalse(exported.get('fields'))

    def test_object_array_requires_member_categories(self):
        self.real_writer_case('{email:string}[]', '[{email}]')
        self.assertTrue(self.row('payload')['composite'])
        self.assertFalse(self.row('payload')['scalar_array'])
        errors = field_review.apply_decisions(self.document, [self.decision('payload', 'yes', ['user.contact.email'])], self.repo)
        self.assertTrue(any('categories belong on evidenced members' in error for error in errors))
        self.assertEqual(field_review.apply_decisions(self.document, [self.decision('payload.email', 'yes', ['user.contact.email'])], self.repo), [])
        self.assertEqual(field_review.validate_contract(self.document, self.derived, self.repo), [])

    def test_unknown_array_items_remain_unresolved_content(self):
        self.real_writer_case('unknown[]', 'input', parameter='input:unknown[]')
        self.assertFalse(self.row('payload')['scalar_array'])
        self.assertTrue(self.row('payload')['composite'])
        self.assertTrue(self.row('payload')['open_content'])
        self.assertTrue(field_review.validate_contract(self.document, self.derived, self.repo))

    def supplemental_projection_fixture(self):
        import dependencies
        import shape_investigation
        (self.repo / 'writer.ts').write_text('export type Payload = { details: { format: string } };\n')
        (self.repo / 'schema.ts').write_text(self.source + "import {pgTable, text, boolean, timestamp, jsonb} from 'drizzle-orm/pg-core';\n")
        self.compiler = field_review.compiler_facts(self.repo)
        field_review.build_contract(self.document, self.repo, self.compiler, fresh=True)
        parent = self.row('payload')
        proposal = {'entity_id': parent['entity_id'], 'observation_digest': parent['observation_digest'],
                    'rationale': 'The inspected payload contract declares a nested format.', 'refs': ['writer.ts:1'],
                    'type': {'kind': 'object', 'open': False}, 'members': [
                        {'name': 'details', 'type': {'kind': 'object', 'open': False}, 'members': [
                            {'name': 'format', 'shape': 'string', 'type': {'kind': 'scalar', 'name': 'string'}}]}]}
        self.assertEqual(shape_investigation.apply(self.document, [proposal], self.repo), [])
        decisions = [self.decision('email', 'yes', ['user.contact.email']), self.decision('enabled'), self.decision('created_at'),
                     self.decision('payload.details.format')]
        self.assertEqual(field_review.apply_decisions(self.document, decisions, self.repo), [])
        spec = {'id': 'declared-schema', 'path': 'schema.ts', 'method': 'text', 'targets': ['db/records']}
        spec['fingerprint'] = dependencies.observe(self.repo, spec)['fingerprint']
        manifest = {'version': '0.9.0', 'piece': 'privacy-datamap', 'system': [],
                    'source': {'slug': 'fixture/storage', 'commit_sha': 'a' * 40, 'branch': 'main',
                               'generated_by': 'privacy-datamap@0.9.0', 'derived_digest': '0' * 64},
                    'dataset': [{'fides_key': 'db', 'name': 'Fixture storage',
                                 'meta': {'noru': {'origin': 'supplemental', 'refs': ['schema.ts:1']}},
                                 'collections': [{'name': 'records', 'refs': ['schema.ts:1'], 'fields': []}]}],
                    'evidence_dependencies': [spec]}
        return manifest

    def test_projected_supplemental_citations_get_bound_dependencies(self):
        import dataset_inputs
        import dependencies
        manifest = self.supplemental_projection_fixture()
        original = copy.deepcopy(manifest['evidence_dependencies'])
        field_review.project_candidate(self.document, manifest, derived=self.derived, repo=self.repo)
        self.assertEqual(dataset_inputs.dependency_gaps(manifest), [])
        self.assertEqual(manifest['evidence_dependencies'][:len(original)], original)
        added = manifest['evidence_dependencies'][len(original):]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['path'], 'writer.ts')
        self.assertEqual(added[0]['method'], 'text')
        self.assertIn('db/records/payload.details.format', added[0]['targets'])
        targets = {row['entity_id'] for row in field_review.all_rows(self.document['classification_review']['fields'])} | {'db/records'}
        self.assertEqual(dependencies.validate_specs(manifest['evidence_dependencies'], targets), [])
        self.assertEqual(dependencies.reconcile(self.repo, manifest['evidence_dependencies'], {})[1], [])
        before = copy.deepcopy(manifest)
        field_review.project_candidate(self.document, manifest, derived=self.derived, repo=self.repo)
        self.assertEqual(manifest, before)

    def test_projection_does_not_refresh_existing_stale_fingerprints(self):
        import dependencies
        manifest = self.supplemental_projection_fixture()
        manifest['evidence_dependencies'][0]['fingerprint'] = '0' * 64
        field_review.project_candidate(self.document, manifest, repo=self.repo)
        self.assertEqual(manifest['evidence_dependencies'][0]['fingerprint'], '0' * 64)
        changes = dependencies.reconcile(self.repo, manifest['evidence_dependencies'], {})[1]
        self.assertTrue(any(row['id'] == 'declared-schema' and row['action'] == 'investigate' for row in changes))

    def test_projection_rejects_changed_or_unbound_new_citations(self):
        manifest = self.supplemental_projection_fixture()
        original = copy.deepcopy(manifest['evidence_dependencies'])
        path = self.repo / 'writer.ts'
        source = path.read_text()
        path.write_text(source + '// changed after investigation\n')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            field_review.project_candidate(self.document, manifest, repo=self.repo)
        self.assertEqual(manifest['evidence_dependencies'], original)
        path.write_text(source)
        self.row('payload.details.format')['evidence'] = ['unbound.ts:1']
        with self.assertRaisesRegex(ValueError, 'unbound classification citation'):
            field_review.project_candidate(self.document, manifest, repo=self.repo)
        self.assertEqual(manifest['evidence_dependencies'], original)

    def test_projected_dependencies_remain_required_at_acceptance_and_export(self):
        import datetime
        import hashlib
        import json
        import reconcile
        import subprocess
        import validate_manifest
        manifest = self.supplemental_projection_fixture()
        field_review.project_candidate(self.document, manifest, repo=self.repo)
        collection = manifest['dataset'][0]['collections'][0]
        today = datetime.date.today()
        collection['interpretation'] = {'owner': 'Fixture Reviewer', 'decided_at': today.isoformat(),
                                        'expires_at': (today + datetime.timedelta(days=30)).isoformat(),
                                        'rationale': 'Reviewed the synthetic source-bound storage contract.'}
        collection['structure_digest'] = validate_manifest.structure_digest(collection['fields'], [])
        cache = self.repo / '.noru/.cache'
        cache.mkdir(parents=True)
        analysis_storage.save(cache / 'privacy-datamap.analysis.json', self.document)
        # The same validation gate is used by sealing, then the certificate binds the
        # projected dependency list for cache-independent export verification.
        report, _ = validate_manifest.validate(manifest, validate_manifest.load_vocabulary(), repo=self.repo,
                                               allow_discovery_review=True, allow_semantic_review=True)
        self.assertEqual(report.errors, [])
        certificate = field_review.classification_certificate(self.document, self.derived, self.repo, manifest)
        path = self.repo / '.noru/privacy-datamap.yml'
        path.write_text(reconcile.to_yaml(manifest))
        (self.repo / '.noru/privacy-datamap.lock.json').write_text(json.dumps({
            'accepted_manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'classification_review': certificate}))
        (cache / 'privacy-datamap.derived.json').write_text(json.dumps(self.derived))
        (cache / 'privacy-datamap.analysis.json').unlink()
        output = cache / 'privacy-datamap.parsed.json'
        command = [sys.executable, str(ROOT / 'plugins/privacy-datamap/scripts/validate_manifest.py'),
                   str(path), '--emit-parsed=' + str(output), '--quiet']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(output.read_text())['evidence_dependencies'], manifest['evidence_dependencies'])
        output.unlink()
        (self.repo / 'writer.ts').write_text('export type Payload = { details: { format: number } };\n')
        report, _ = validate_manifest.validate(manifest, validate_manifest.load_vocabulary(), repo=self.repo,
                                               allow_discovery_review=True, allow_semantic_review=True)
        self.assertTrue(any(path == 'evidence_dependencies' and 'investigate' in error for path, error in report.errors), report.errors)
        with self.assertRaisesRegex(ValueError, 'Cannot certify incomplete classification'):
            field_review.classification_certificate(self.document, self.derived, self.repo, manifest)
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        self.assertIn('evidence_dependencies', result.stdout)

    def test_schema_only_is_explicit_and_never_exports(self):
        import subprocess, json
        script = ROOT / 'plugins/privacy-datamap/scripts/validate_manifest.py'
        fixture = ROOT / 'plugins/privacy-datamap/fixtures/valid.privacy-datamap.yml'
        result = subprocess.run([sys.executable, str(script), str(fixture), '--schema-only', '--output=json'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['scope'], 'schema_only')
        output = self.repo / 'parsed.json'
        blocked = subprocess.run([sys.executable, str(script), str(fixture), '--schema-only', '--emit-parsed=' + str(output)], capture_output=True, text=True)
        self.assertEqual(blocked.returncode, 2)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
