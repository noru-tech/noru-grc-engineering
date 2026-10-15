#!/usr/bin/env python3
"""Cross-gate extraction dispositions and current-schema coverage contracts."""
import copy
import hashlib
import json
import pathlib
import subprocess
import sys
import unittest
import test_classification_gate as fixtures
import test_dotnet as dotnet_fixtures
sys.path.insert(0,str(fixtures.ROOT/'plugins/privacy-datamap/scripts'))
import field_review
import gap_resolution
import migration_coverage
import review
import analysis_storage
import dotnet


class ResolutionTests(unittest.TestCase):
    setUp = fixtures.ClassificationGateTests.setUp
    row = fixtures.ClassificationGateTests.row
    decision = fixtures.ClassificationGateTests.decision
    resolve = fixtures.ClassificationGateTests.resolve
    accepted_fixture = fixtures.ClassificationGateTests.accepted_fixture

    def gap(self, grouped=False, migration=False):
        self.derived['coverage'] = {'migration_gaps' if migration else 'unparsed_candidates':[
            {'format':'synthetic','code':'SOURCE_CONTRACT', 'diagnostic_id':'synthetic:contract', 'ref':'schema.ts:3',
             'scope':{'datastore':'db'}, 'reason':'The current producer contract requires investigation.',
             **({'occurrences':[{'ref':'schema.ts:1'}, {'ref':'schema.ts:3'}]} if grouped else {})}]}
        field_review.build_contract(self.document,self.repo,self.compiler)
        return self.document['classification_review']['gaps'][-1]

    def test_gap_only_batch_remains_agent_work_after_field_decisions(self):
        import investigate
        self.gap(); self.resolve()
        batch=investigate.prepare_batch(self.document,24,self.repo)
        self.assertEqual(batch['requests'],[])
        self.assertEqual(batch['counts']['agent_pending'],1)
        self.assertEqual(batch['counts']['agent_pending_gaps'],1)
        self.assertEqual(batch['counts']['included'],1)
        task=batch['extraction_gaps'][0]
        self.assertEqual(task['resolution_owner'],'agent')
        self.assertEqual(task['investigation_state'],'investigation_required')
        self.assertTrue(task['investigation_steps'])
        self.assertEqual(batch['human_questions'],[])

    def test_unrelated_structure_preserves_resolution_but_affected_structure_reopens(self):
        gap = self.gap()
        self.assertEqual(field_review.apply_gap_decisions(self.document, [self.answer(gap)], self.repo), [])
        self.derived['datasets'].append({'fides_key': 'other', 'collections': [
            {'name': 'metrics', 'fields': [{'name': 'count', 'shape': 'integer', 'refs': ['schema.ts:1']}]}]})
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertFalse(field_review.gap_partition(self.document, self.derived, self.repo)['active_blockers'])
        self.derived['datasets'][0]['collections'][0]['fields'].append(
            {'name': 'new_member', 'shape': 'text', 'refs': ['schema.ts:1']})
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertTrue(field_review.gap_partition(self.document, self.derived, self.repo)['active_blockers'])

    def test_legacy_binding_migrates_without_refreshing_changed_evidence(self):
        gap = self.gap()
        self.assertEqual(field_review.apply_gap_decisions(self.document, [self.answer(gap)], self.repo), [])
        contract = self.document['classification_review']
        stored = contract['gaps'][-1]
        stored['resolution_binding'] = gap_resolution.binding(stored, contract, legacy=True)
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertFalse(field_review.gap_partition(self.document, self.derived, self.repo)['active_blockers'])
        (self.repo / 'schema.ts').write_text((self.repo / 'schema.ts').read_text() + '\n// producer changed\n')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertTrue(field_review.gap_partition(self.document, self.derived, self.repo)['active_blockers'])

    def test_source_scoped_gap_survives_unrelated_candidate_addition(self):
        (self.repo / 'other.ts').write_text('export const count = 1;\n')
        self.gap()
        self.derived['coverage']['unparsed_candidates'][0]['scope'] = {'path': 'schema.ts', 'kind': 'source'}
        field_review.build_contract(self.document, self.repo, self.compiler)
        gap = self.document['classification_review']['gaps'][-1]
        self.assertEqual(field_review.apply_gap_decisions(self.document, [self.answer(gap, 'not_applicable')], self.repo), [])
        # A valid old global signature can be migrated to the evidenced source scope.
        contract = self.document['classification_review']
        gap = contract['gaps'][-1]
        gap['resolution_binding'] = gap_resolution.binding(gap, contract, legacy=True)
        self.derived['datasets'].append({'fides_key': 'other', 'collections': [{'name': 'metrics',
            'fields': [{'name': 'count', 'shape': 'integer', 'refs': ['other.ts:1']}]}]})
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertFalse(field_review.gap_partition(self.document, self.derived, self.repo)['active_blockers'])
        self.derived['datasets'][0]['collections'][0]['fields'].append({'name': 'new', 'shape': 'text', 'refs': ['schema.ts:1']})
        field_review.build_contract(self.document, self.repo, self.compiler)
        self.assertTrue(field_review.gap_partition(self.document, self.derived, self.repo)['active_blockers'])

    def test_fallback_attempt_is_audited_but_never_clears_a_gap(self):
        import investigate
        gap=self.gap(); self.resolve()
        answer=self.answer(gap,'unresolved')
        answer['fallback_review']=gap_resolution.fallback_template()
        self.assertTrue(field_review.apply_gap_decisions(self.document,[answer],self.repo))
        answer['fallback_review'].update(source_review='Read the producer and current source contract.',
            evidence_ids=gap['evidence_ids'],supplemental_attempt='Known fields fit the supplemental dataset; the external payload definition is absent.',
            missing_fact='Fields emitted by the configured external producer.',required_evidence='The versioned producer payload contract.',
            decision_impact='Determines additional stored members and their classifications.',
            next_evidence_action='Request the versioned producer payload contract from its owner.')
        self.assertEqual(field_review.apply_gap_decisions(self.document,[answer],self.repo),[])
        task=investigate.prepare_batch(self.document,24,self.repo)['extraction_gaps'][0]
        self.assertEqual(task['investigation_state'],'evidence_unavailable')
        self.assertTrue(field_review.gap_partition(self.document,self.derived,self.repo)['active_blockers'])
        progress=review.scan_progress(self.document,{},self.derived,[],[],self.repo)
        self.assertEqual(progress['publication'],'blocked')
        answer['fallback_review']['remaining_reason']='representation_limitation'
        self.assertTrue(field_review.apply_gap_decisions(self.document,[answer],self.repo))
        self.assertEqual(task['next_evidence_action'],answer['fallback_review']['next_evidence_action'])
        self.assertTrue(any('Never delegate investigation' in step for step in task['investigation_steps']))
        self.assertIn('next evidence-gathering action', ' '.join(progress['next_actions']))
        # Old cached implementation-limit records stay visible but require renewed investigation.
        stored=self.document['classification_review']['gaps'][-1]['resolution']['fallback_review']
        stored['remaining_reason']='representation_limitation'
        legacy_task=investigate.prepare_batch(self.document,24,self.repo)['extraction_gaps'][0]
        self.assertEqual(legacy_task['investigation_state'],'investigation_required')
        self.assertEqual(legacy_task['resolution_owner'],'agent')
        stored['remaining_reason']='evidence_unavailable'
        next_action=stored.pop('next_evidence_action')
        self.assertEqual(investigate.prepare_batch(self.document,24,self.repo)['extraction_gaps'][0]['investigation_state'],'investigation_required')

        stored['next_evidence_action']=next_action
        self.assertEqual(investigate.prepare_batch(self.document,24,self.repo)['extraction_gaps'][0]['investigation_state'],'evidence_unavailable')
        with (self.repo/'schema.ts').open('a') as source:source.write('\n// changed evidence\n')
        self.assertEqual(investigate.prepare_batch(self.document,24,self.repo)['extraction_gaps'][0]['investigation_state'],'investigation_required')

    def test_legacy_unresolved_gap_is_not_an_exhausted_investigation(self):
        import investigate
        gap=self.gap()
        self.assertEqual(field_review.apply_gap_decisions(self.document,[self.answer(gap,'unresolved')],self.repo),[])
        self.assertEqual(investigate.prepare_batch(self.document,24,self.repo)['extraction_gaps'][0]['investigation_state'],'investigation_required')

    def answer(self, gap, outcome='not_applicable'):
        return {'gap_id':gap['gap_id'],'outcome':outcome,'rationale':'The inspected producer is represented by the current field definitions.',
                'scope_rationale':'This diagnostic concerns an unused read-only helper; it does not alter represented storage.',
                'investigation':[{'question':'Which persistent structure is affected?', 'outcome':'Traced the producer and current schema.'}],
                'evidence_ids':gap['evidence_ids'],'field_ids':['db/records/email'],
                'covered_occurrence_ids':gap_resolution.occurrence_ids(gap),
                'coverage_binding':{'datastore':'db','migration_paths':[gap.get('ref','').rsplit(':',1)[0]],'field_ids':['db/records/email'],'evidence_ids':gap['evidence_ids']},
                'identity_rationale':'The explicit connection binds this producer to the represented datastore.',
                'coverage_rationale':'The current declaration and producer enumerate the complete relevant schema.'}

    def test_enrichment_resolution_is_honored_by_progress_without_erasing_finding(self):
        self.document['store_investigation']={'findings':[{'store':'auxiliary','status':'gap','refs':['schema.ts:3'],
            'unresolved_question':'Does the auxiliary producer persist records?', 'resolution_needed':'Trace the writer.'}]}
        field_review.build_contract(self.document,self.repo,self.compiler)
        gap=self.document['classification_review']['gaps'][-1]
        self.resolve()
        self.assertEqual(field_review.apply_gap_decisions(self.document,[self.answer(gap)],self.repo),[])
        progress=review.scan_progress(self.document,{},self.derived,[],[],self.repo)
        self.assertEqual(progress['agent_investigation'],'complete')
        self.assertEqual(progress['technical_questions'],[])
        self.assertEqual(progress['extraction_gaps'],0)
        self.assertEqual(self.document['store_investigation']['findings'][0]['status'],'gap')
        self.assertFalse(field_review.validate_contract(self.document,self.derived,self.repo))

    def test_enrichment_gap_cannot_use_cacheless_certificate_to_bypass_review(self):
        self.resolve()
        manifest=self.accepted_fixture(resolve=False)
        document={'store_investigation':{'findings':[{'store':'new','status':'gap','refs':['schema.ts:3'],
            'unresolved_question':'A new producer needs a contract.'}]}}
        self.assertTrue(field_review.gap_partition(document,self.derived,self.repo)['active_blockers'])
        self.assertTrue(field_review.validate_repository(document,self.derived,self.repo,candidate=manifest))

    def test_runtime_only_resolution_survives_rescan_and_reopens_on_flow_change(self):
        self.derived['systems']=[{'fides_key':'edge','refs':['schema.ts:3'],
                                 'dataset_references':[], 'egress':[{'type':'system','fides_key':'service'}]}]
        self.document['store_investigation']={'findings':[{'store':'edge','status':'gap',
            'refs':['schema.ts:3'],'required_system_ids':['edge'],
            'unresolved_question':'Where does the provisioned runtime forward requests?'}]}
        field_review.build_contract(self.document,self.repo,self.compiler)
        gap=self.document['classification_review']['gaps'][-1]
        answer=self.answer(gap,'modeled')
        answer.update(field_ids=[],system_ids=['edge'],runtime_rationale='The evidenced runtime forwards to the represented service; it has no persistent dataset.')
        answer['evidence_ids']=sorted(set(answer['evidence_ids']+self.document['classification_review']['runtime_definitions']['edge']['evidence_ids']))
        self.assertEqual(field_review.apply_gap_decisions(self.document,[answer],self.repo),[])
        field_review.build_contract(self.document,self.repo,self.compiler)
        self.assertEqual(field_review.gap_partition(self.document,self.derived,self.repo)['active_blockers'],[])
        self.derived['systems'][0]['egress'][0]['fides_key']='other-service'
        self.assertTrue(field_review.gap_partition(self.document,self.derived,self.repo)['active_blockers'])
        field_review.build_contract(self.document,self.repo,self.compiler)
        self.assertTrue(field_review.gap_partition(self.document,self.derived,self.repo)['active_blockers'])

    def test_unrelated_runtime_cannot_resolve_datastore_or_required_runtime(self):
        self.derived['systems']=[{'fides_key':'edge','refs':['schema.ts:3']}]
        gap=self.gap()
        answer=self.answer(gap,'modeled');answer.update(field_ids=[],system_ids=['edge'],runtime_rationale='Forwarding only.')
        answer['evidence_ids']=sorted(set(answer['evidence_ids']+self.document['classification_review']['runtime_definitions']['edge']['evidence_ids']))
        self.assertTrue(field_review.apply_gap_decisions(self.document,[answer],self.repo))
        answer['system_ids']=['missing']
        self.assertTrue(field_review.apply_gap_decisions(self.document,[answer],self.repo))
        self.document['classification_review']['gaps'][-1]['scope']={}
        answer['system_ids']=['edge']
        self.assertTrue(field_review.apply_gap_decisions(self.document,[answer],self.repo))

    def test_sealed_certificate_rejects_changed_runtime_flows(self):
        self.resolve(); self.accepted_fixture(resolve=False)
        self.assertEqual(field_review.accepted_certificate_errors(self.derived,self.repo),[])
        self.derived['systems']=[{'fides_key':'new-edge','egress':[]}]
        self.assertIn('Sealed runtime definitions or flows changed.',field_review.accepted_certificate_errors(self.derived,self.repo))

    def test_known_open_record_resolves_structure_without_accepting_meaning(self):
        gap=self.gap()
        row=self.document['classification_review']['fields'][0]
        row.update(open_content=True, type={'kind':'record','key_type':'string','value_type':'string','open':True})
        row['open_content_resolution']=None
        answer=self.answer(gap,'modeled')
        self.assertTrue(field_review.apply_gap_decisions(self.document,[answer],self.repo))
        answer['open_structure']={row['entity_id']:{'additional_entries':True,'key_type':'string','value_type':'string',
            'rationale':'The producer permits arbitrary string entries.', 'evidence_ids':gap['evidence_ids']}}
        self.assertEqual(field_review.apply_gap_decisions(self.document,[answer],self.repo),[])
        self.assertEqual(field_review.gap_partition(self.document,self.derived,self.repo)['active_blockers'],[])
        self.assertTrue(field_review.validate_contract(self.document,self.derived,self.repo))
        self.assertEqual(row['status'],'needs_investigation')

    def test_resolved_raw_diagnostic_is_visible_but_not_a_blocker(self):
        gap=self.gap(); self.resolve()
        self.assertEqual(field_review.apply_gap_decisions(self.document,[self.answer(gap)],self.repo),[])
        partition=field_review.gap_partition(self.document,self.derived,self.repo)
        self.assertEqual(len(partition['observed_diagnostics']),1)
        self.assertEqual(len(partition['resolved_diagnostics']),1)
        self.assertEqual(partition['active_blockers'],[])
        self.assertEqual(review.structural_blockers(self.document,{},self.derived),[])
        self.assertEqual(field_review.validate_contract(self.document,self.derived,self.repo),[])
        manifest=self.accepted_fixture(resolve=False)
        self.assertEqual(field_review.validate_repository({},self.derived,self.repo,candidate=manifest),[])
        self.assertEqual(field_review.gap_partition({},self.derived,self.repo)['active_blockers'],[])
        self.assertEqual(review.structural_blockers({}, {}, self.derived, self.repo), [])
        self.assertIn('rationale',json.loads((self.repo/'.noru/privacy-datamap.lock.json').read_text())['classification_review']['gap_resolutions'][0]['resolution'])

    def test_ci_uses_the_shared_active_coverage_partition(self):
        import ci_check
        from types import SimpleNamespace
        gap=self.gap()
        path=self.repo/'.noru/.cache/privacy-datamap.derived.json';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(self.derived))
        events=[];report=SimpleNamespace(step=lambda *a,**k:events.append(a),find=lambda *a,**k:events.append(a))
        summary={'derived_facts':str(path.relative_to(self.repo))};decl={'artifact':'.noru/privacy-datamap.yml'}
        partition=field_review.gap_partition(self.document,self.derived,self.repo)
        self.assertEqual(ci_check.check_coverage(report,self.repo,summary,decl,{'extraction_diagnostics':partition}),'tooling')
        self.assertEqual(field_review.apply_gap_decisions(self.document,[self.answer(gap)],self.repo),[])
        events.clear();partition=field_review.gap_partition(self.document,self.derived,self.repo)
        self.assertEqual(ci_check.check_coverage(report,self.repo,summary,decl,{'extraction_diagnostics':partition}),'ok')
        self.assertEqual(events,[])

    def test_modeled_requires_target_structure_and_matching_scope(self):
        gap=self.gap(); self.resolve(); answer=self.answer(gap,'modeled')
        answer['field_ids']=['unrelated/records/email']
        self.assertTrue(field_review.apply_gap_decisions(self.document,[answer],self.repo))
        answer['field_ids']=['db/records/email']
        self.assertEqual(field_review.apply_gap_decisions(self.document,[answer],self.repo),[])
        self.row('email')['observation_digest']='changed'
        self.assertTrue(field_review.gap_partition(self.document,self.derived,self.repo)['active_blockers'])

    def test_group_requires_all_occurrences_and_conflicts_remain_blocked(self):
        gap=self.gap(grouped=True); answer=self.answer(gap)
        answer['covered_occurrence_ids']=answer['covered_occurrence_ids'][:1]
        self.assertTrue(field_review.apply_gap_decisions(self.document,[answer],self.repo))
        self.assertEqual(field_review.apply_gap_decisions(self.document,[self.answer(gap)],self.repo),[])
        changed=copy.deepcopy(self.document['classification_review']['gaps'][0]); changed['message']='Other scope';self.document['classification_review']['gaps'].append(changed)
        self.assertTrue(field_review.gap_partition(self.document,self.derived,self.repo)['active_blockers'])

    def test_unchanged_rescan_preserves_and_changed_occurrences_reopen(self):
        gap=self.gap(grouped=True);self.resolve()
        self.assertEqual(field_review.apply_gap_decisions(self.document,[self.answer(gap)],self.repo),[])
        prior=copy.deepcopy(self.row('email'))
        field_review.build_contract(self.document,self.repo,self.compiler)
        self.assertEqual(self.row('email'),prior)
        self.assertFalse(field_review.gap_partition(self.document,self.derived,self.repo)['active_blockers'])
        self.derived['coverage']['unparsed_candidates'][0]['occurrences'].append({'ref':'schema.ts:2'})
        self.assertTrue(field_review.gap_partition(self.document,self.derived,self.repo)['active_blockers'])
        field_review.build_contract(self.document,self.repo,self.compiler)
        reopened=self.document['classification_review']['gaps'][0]
        self.assertEqual(reopened['status'],'unresolved');self.assertTrue(reopened['resolution_history'])

    def test_stale_and_unresolved_resolutions_block(self):
        gap=self.gap(); answer=self.answer(gap,'unresolved')
        self.assertEqual(field_review.apply_gap_decisions(self.document,[answer],self.repo),[])
        self.assertTrue(field_review.gap_partition(self.document,self.derived,self.repo)['active_blockers'])
        self.assertEqual(field_review.apply_gap_decisions(self.document,[self.answer(gap)],self.repo),[])
        (self.repo/'schema.ts').write_text(self.source+'// changed source\n')
        self.assertTrue(field_review.gap_partition(self.document,self.derived,self.repo)['active_blockers'])
        def refresh(value):
            if isinstance(value,dict):
                if value.get('path')=='schema.ts' and 'sha256' in value: value['sha256']=hashlib.sha256((self.repo/'schema.ts').read_bytes()).hexdigest()
                for child in value.values(): refresh(child)
            elif isinstance(value,list):
                for child in value: refresh(child)
        refresh(self.compiler)
        field_review.build_contract(self.document,self.repo,self.compiler)
        self.assertEqual(self.document['classification_review']['gaps'][0]['status'],'unresolved')

    def test_missing_registry_and_legacy_unbound_resolution_fail_closed(self):
        gap=self.gap();self.assertTrue(field_review.gap_partition({},self.derived,self.repo)['active_blockers'])
        gap.update(status='resolved',resolution={'outcome':'not_applicable','rationale':'old cache'})
        self.assertTrue(field_review.gap_partition(self.document,self.derived,self.repo)['active_blockers'])
        field_review.build_contract(self.document,self.repo,self.compiler)
        self.assertEqual(self.document['classification_review']['gaps'][0]['status'],'unresolved')

    def test_migration_identity_and_completeness_are_separate(self):
        gap=self.gap(migration=True);self.resolve();answer=self.answer(gap,'modeled');answer.pop('coverage_rationale')
        self.assertTrue(field_review.apply_gap_decisions(self.document,[answer],self.repo))
        self.assertEqual(field_review.apply_gap_decisions(self.document,[self.answer(gap,'modeled')],self.repo),[])
        self.assertFalse(review.structural_blockers(self.document,{},self.derived))

    def test_supplemental_targets_require_evidence_scope_and_complete_coverage(self):
        (self.repo/'current.sql').write_text('CREATE TABLE supplement (value TEXT);\n')
        self.derived['datasets'].append({'fides_key':'other','collections':[{'name':'supplement','fields':[
            {'name':'value','shape':'TEXT','refs':['current.sql:1']}]}]})
        gap=self.gap(migration=True)
        answer=self.answer(gap,'modeled');answer['field_ids']=['other/supplement/value'];answer['coverage_binding'].update(datastore='other',field_ids=answer['field_ids'])
        self.assertTrue(field_review.apply_gap_decisions(self.document,[answer],self.repo))
        raw=self.derived['coverage']['migration_gaps'][0]
        raw['scope']['datastore']='other';raw['required_field_ids']=['other/supplement/value']
        field_review.build_contract(self.document,self.repo,self.compiler)
        gap=self.document['classification_review']['gaps'][0]
        answer=self.answer(gap,'modeled');answer['field_ids']=['other/supplement/value'];answer['coverage_binding'].update(datastore='other',field_ids=answer['field_ids'])
        self.assertEqual(field_review.apply_gap_decisions(self.document,[answer],self.repo),[])
        self.assertTrue(field_review.validate_contract(self.document,self.derived,self.repo), 'Structure resolution must not imply privacy acceptance')
        raw['required_field_ids'].append('other/supplement/missing')
        field_review.build_contract(self.document,self.repo,self.compiler)
        gap=self.document['classification_review']['gaps'][0]
        answer=self.answer(gap,'modeled');answer['field_ids']=['other/supplement/value'];answer['coverage_binding'].update(datastore='other',field_ids=answer['field_ids'])
        self.assertTrue(field_review.apply_gap_decisions(self.document,[answer],self.repo))

    def test_removed_diagnostic_remains_in_history(self):
        gap=self.gap();self.assertEqual(field_review.apply_gap_decisions(self.document,[self.answer(gap)],self.repo),[])
        self.derived['coverage']={};field_review.build_contract(self.document,self.repo,self.compiler)
        self.assertEqual(self.document['classification_review']['gaps'],[])
        self.assertTrue(self.document['classification_review']['diagnostic_history'][0]['resolution'])


class MigrationTests(unittest.TestCase):
    setUp=dotnet_fixtures.DotnetTests.setUp
    put=dotnet_fixtures.DotnetTests.put

    def fixture(self):
        self.put('models/Models.csproj','<Project />')
        self.put('models/Row.cs','namespace Shared; public class Row { public int Id { get; set; } }')
        self.put('storage/Storage.csproj','<Project><ItemGroup><ProjectReference Include="../models/Models.csproj" /></ItemGroup></Project>')
        self.put('storage/Contexts.cs','using Microsoft.EntityFrameworkCore; using Shared; namespace Data; class First : DbContext { public DbSet<Row> Rows { get; set; } } class Second : DbContext { public DbSet<Row> Rows { get; set; } }')
        self.put('storage/Runtime.cs','using Data; builder.AddNpgsqlDbContext<First>("first"); builder.AddNpgsqlDbContext<Second>("second");')
        self.put('history/History.csproj','<Project><ItemGroup><ProjectReference Include="../storage/Storage.csproj" /></ItemGroup></Project>')
        self.put('history/Migrations/Create.cs','namespace History; partial class Create { void Up() { } }')
        self.put('history/Migrations/Create.Designer.cs','using Microsoft.EntityFrameworkCore.Infrastructure; using Data; namespace History; [DbContext(typeof(First))] partial class Create {}')
        self.paths=[p for p in self.files if '/Migrations/' in p]

    def coverage(self):
        report=dotnet.discover(self.repo,[p for p in self.files if p not in self.paths])
        return migration_coverage.inspect(self.repo,{'migrations':self.paths,'dotnet':report})

    def test_cross_project_models_and_partial_migration_match_one_context(self):
        self.fixture();report=self.coverage()
        self.assertEqual(report['gaps'],[])
        self.assertEqual({r['scope']['context'] for r in report['records']},{'Data.First'})
        self.assertTrue(all(r['schema_status']=='covered' for r in report['records']))
        self.assertEqual(report,self.coverage())

    def test_context_identity_does_not_hide_incomplete_model(self):
        self.fixture();self.put('models/Row.cs','namespace Shared; public class Row { public Missing Value { get; set; } }')
        report=self.coverage();self.assertTrue(report['gaps'])
        self.assertTrue(all(r['identity_status']=='matched' for r in report['records']))
        self.assertTrue(all(r['schema_status']=='incomplete' for r in report['records']))

    def test_ambiguous_companion_and_missing_project_reference_block(self):
        self.fixture();self.put('history/Migrations/Other.cs','using Microsoft.EntityFrameworkCore.Infrastructure; using Data; namespace History; [DbContext(typeof(Second))] partial class Create {}');self.paths.append('history/Migrations/Other.cs')
        self.assertTrue(all(r['identity_status']=='ambiguous' for r in self.coverage()['records']))
        self.fixture();self.put('history/History.csproj','<Project />')
        self.assertTrue(all(r['identity_status']=='unresolved' for r in self.coverage()['records']))

    def test_declared_schema_completeness_and_normalized_identity(self):
        self.put('history/001.sql','CREATE TABLE obsolete (old TEXT);')
        request={'migrations':['history/001.sql'],'topology':{'history/001.sql':{'ref':'config.ts:1'}},
                 'current_schema_claims':{'models/current.ts':{'ref':'config.ts:1'},'models/missing.ts':{'ref':'config.ts:1'}},
                 'datastore_keys':{'models/current':'models_current'},
                 'observations':[{'path':'models/current.ts','boundary':'models/current','topology_ref':'config.ts:1',
                                  'collections':[{'name':'rows','fields':[{'name':'value'}]}]}]}
        result=migration_coverage.inspect(self.repo,request)
        self.assertEqual(result['records'][0]['identity_status'],'matched')
        self.assertEqual(result['records'][0]['scope']['datastore'],'models_current')
        self.assertEqual(result['records'][0]['schema_status'],'incomplete')
        request['current_schema_claims'].pop('models/missing.ts')
        self.assertEqual(migration_coverage.inspect(self.repo,request)['gaps'],[])

    def test_local_attribute_shadow_does_not_establish_context_identity(self):
        self.fixture();self.put('history/Fake.cs','namespace History; class DbContextAttribute {}')
        self.assertTrue(all(row['identity_status']=='unresolved' for row in self.coverage()['records']))

    def test_proximity_and_misleading_attribute_never_prove_coverage(self):
        self.fixture();self.put('history/Migrations/Create.Designer.cs','using Data; namespace History; [DbContext(typeof(First))] partial class Create {}')
        self.assertTrue(self.coverage()['gaps'])
        self.assertTrue(all(r['schema_status']=='incomplete' for r in self.coverage()['records']))


if __name__=='__main__': unittest.main()
