#!/usr/bin/env python3
"""Compiler adapter tests use synthetic source and never execute fixture code."""
import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ADAPTER = ROOT / 'plugins/privacy-datamap/scripts/typescript_evidence.mjs'
IMPORT = "import { pgTable, text, jsonb, boolean, timestamp } from 'drizzle-orm/pg-core';\n"


class CompilerEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = pathlib.Path(self.temporary.name)

    def write(self, name, content):
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def extract(self, options=None):
        if options:
            script = 'import {extractRepository} from '+json.dumps(ADAPTER.as_uri())+'; process.stdout.write(JSON.stringify(extractRepository('+json.dumps(str(self.repo))+','+json.dumps(options)+')));'
            command = ['node', '--input-type=module', '-e', script]
        else:
            command = ['node', str(ADAPTER), '--repo='+str(self.repo)]
        result = subprocess.run(command, text=True, capture_output=True, check=True)
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)

    def test_alias_import_and_namespaced_constructor(self):
        self.write('schema.ts', "import {pgTable as defineTable, text} from 'drizzle-orm/pg-core';\nimport * as sqlite from 'drizzle-orm/sqlite-core';\nexport const entries=defineTable('entries',{identity:text('identity_key')}); export const copies=sqlite.sqliteTable('copies',{id:sqlite.text('id')});")
        result=self.extract()
        self.assertEqual({table['name'] for table in result['tables']},{'entries','copies'})
        field=next(table for table in result['tables'] if table['name']=='entries')['fields'][0]
        self.assertEqual((field['name'],field['property']),('identity_key','identity'))

    def test_local_function_called_pgtable_is_not_a_schema(self):
        self.write('schema.ts', "function pgTable(name:string, values:object){return values;} const x=pgTable('x',{id:'example'});")
        self.assertEqual(self.extract()['tables'],[])

    def test_unbound_schema_constructor_records_gap(self):
        self.write('schema.ts', "const x=pgTable('x',{id:text('id')});")
        self.assertIn('unresolved_schema_constructor',{gap['kind'] for gap in self.extract()['gaps']})

    def test_imported_types_nested_arrays_union_and_open_content(self):
        self.write('types.ts','export interface Contact { address: string; optional?: { code: string }; }')
        self.write('schema.ts',IMPORT+"import type {Contact as Entry} from './types'; const rows=pgTable('rows',{data:jsonb('data').$type<{contacts:Entry[]; mode:'person'|'company'; extra:Record<string,unknown>}>()});")
        field=self.extract()['tables'][0]['fields'][0]
        contacts=next(member for member in field['members'] if member['name']=='contacts')
        address=contacts['members'][0]
        self.assertEqual(address['path'],'data.contacts[].address')
        self.assertEqual(address['evidence'][0]['path'],'types.ts')
        extra=next(member for member in field['members'] if member['name']=='extra')
        self.assertTrue(extra['type']['open'])
        mode=next(member for member in field['members'] if member['name']=='mode')
        self.assertEqual(mode['type']['kind'],'union')
        self.assertEqual({variant['literal'] for variant in mode['type']['variants']},{'person','company'})

    def test_tsconfig_paths_are_data_only(self):
        self.write('tsconfig.json', '{"compilerOptions":{"baseUrl":".","paths":{"@models/*":["models/*"]},"plugins":[{"name":"./malicious.cjs"}]}}')
        self.write('malicious.cjs', "require('fs').writeFileSync('EXECUTED','yes')")
        self.write('models/types.ts','export type Person = { mailbox:string };')
        self.write('schema.ts',IMPORT+"import type {Person} from '@models/types'; const rows=pgTable('rows',{person:jsonb('person').$type<Person>()});")
        result=self.extract()
        self.assertEqual(result['tables'][0]['fields'][0]['members'][0]['name'],'mailbox')
        self.assertFalse((self.repo/'EXECUTED').exists())
        self.assertIn('tsconfig.json',{source['path'] for source in result['sources']})

    def test_workspace_package_exports_are_static(self):
        self.write('packages/model/package.json','{"name":"@sample/model","exports":{"./types":"./src/types.ts"}}')
        self.write('packages/model/src/types.ts','export interface Contact { address:string; }')
        self.write('schema.ts',IMPORT+"import type {Contact} from '@sample/model/types';const rows=pgTable('rows',{data:jsonb('data').$type<Contact>()});")
        self.assertEqual(self.extract()['tables'][0]['fields'][0]['members'][0]['name'],'address')

    def test_workspace_export_replaces_every_wildcard_literally(self):
        self.write('packages/model/package.json', json.dumps({
            'name': '@sample/model', 'exports': {'./*': './src/*/copy-*.ts'}}))
        self.write('packages/model/src/$&/copy-$&.ts', 'export interface Contact { address:string; }')
        self.write('schema.ts', IMPORT+"import type {Contact} from '@sample/model/$&';const rows=pgTable('rows',{data:jsonb('data').$type<Contact>()});")
        self.assertEqual(self.extract()['tables'][0]['fields'][0]['members'][0]['name'], 'address')

    def test_tsconfig_path_substitution_preserves_literal_dollar_sequences(self):
        self.write('tsconfig.json', json.dumps({'compilerOptions': {
            'baseUrl': '.', 'paths': {'@models/*': ['models/*']}}}))
        self.write('models/$&.ts', 'export interface Contact { address:string; }')
        self.write('schema.ts', IMPORT+"import type {Contact} from '@models/$&';const rows=pgTable('rows',{data:jsonb('data').$type<Contact>()});")
        self.assertEqual(self.extract()['tables'][0]['fields'][0]['members'][0]['name'], 'address')

    def test_tsconfig_paths_reject_multiple_wildcards(self):
        for pattern, replacement in (('@models/*/*', 'models/*'), ('@models/*', 'models/*/*')):
            with self.subTest(pattern=pattern, replacement=replacement):
                self.write('tsconfig.json', json.dumps({'compilerOptions': {
                    'baseUrl': '.', 'paths': {pattern: [replacement]}}}))
                self.write('models/types/*.ts', 'export interface Contact { address:string; }')
                self.write('schema.ts', IMPORT+"import type {Contact} from '@models/types';const rows=pgTable('rows',{data:jsonb('data').$type<Contact>()});")
                report = self.extract()
                self.assertIn('configuration_paths', {gap['kind'] for gap in report['gaps']})
                self.assertEqual(report['tables'][0]['fields'][0]['members'], [])

    def test_foreign_key_is_fact_without_person_verdict(self):
        self.write('principals.ts',IMPORT+"export const principals=pgTable('principals',{id:text('id'),kind:text('kind'),email:text('email')});")
        self.write('schema.ts',IMPORT+"import {principals as entities} from './principals';const rows=pgTable('rows',{owner:text('owner').references(()=>entities.id)});")
        result=self.extract();field=next(table for table in result['tables'] if table['name']=='rows')['fields'][0]
        self.assertEqual(field['foreign_key']['table_symbol_id'],'principals.ts#principals')
        self.assertEqual(field['foreign_key']['column'],'id')
        self.assertNotIn('personal_data',json.dumps(result))
        self.assertNotIn('data_categories',json.dumps(result))

    def test_migrations_excluded_even_if_explicitly_included_as_test(self):
        self.write('schema.ts',IMPORT+"const rows=pgTable('current',{id:text('id')});")
        self.write('db/migrations/old.ts',IMPORT+"const old=pgTable('historical',{email:text('email')});")
        result=self.extract({'includeTests':['db/migrations/old.ts']})
        self.assertEqual([table['name'] for table in result['tables']],['current'])
        self.assertNotIn('db/migrations/old.ts',{source['path'] for source in result['sources']})

    def test_custom_migration_paths_excluded(self):
        self.write('schema.ts',IMPORT+"const rows=pgTable('current',{id:text('id')});")
        self.write('history/001.ts',IMPORT+"const rows=pgTable('historical',{id:text('id')});")
        result=self.extract({'excludedPaths':['history/001.ts']})
        self.assertEqual([table['name'] for table in result['tables']],['current'])

    def test_test_schema_only_explicitly_included(self):
        self.write('fixture.test.ts',IMPORT+"const rows=pgTable('fixture',{id:text('id')});")
        self.assertEqual(self.extract()['tables'],[])
        self.assertEqual(self.extract({'includeTests':['fixture.test.ts']})['tables'][0]['name'],'fixture')

    def test_direct_writes_and_transforms_preserve_lineage(self):
        self.write('schema.ts',IMPORT+"export const rows=pgTable('rows',{digest:text('digest'),owner:text('owner')});")
        self.write('writer.ts',"import {rows} from './schema'; import {createHash} from 'node:crypto'; function store(db:any,person:{id:string;email:string}){const actor=person.id;return db.insert(rows).values({owner:actor,digest:createHash('sha256').update(person.email).digest('hex')});}")
        result=self.extract();fields={field['name']:field for field in result['tables'][0]['fields']}
        owner=fields['owner']['writes'][0]['value']
        self.assertEqual(owner['origin']['source_property'],'id')
        digest=fields['digest']['writes'][0]['value']
        self.assertEqual(digest['receiver']['arguments'][0]['source_property'],'email')
        self.assertEqual(digest['kind'],'call')
        self.assertNotIn('categories',digest)

    def test_local_function_summary_and_argument_bindings(self):
        self.write('schema.ts',IMPORT+"const rows=pgTable('rows',{value:text('value')}); function normalize(input:string){return input.trim();} function store(db:any,input:string){return db.update(rows).set({value:normalize(input)});}")
        value=self.extract()['tables'][0]['fields'][0]['writes'][0]['value']
        self.assertEqual(value['parameter_bindings'],[{'parameter':'input','argument_index':0}])
        self.assertEqual(value['local_returns'][0]['callee'],'input.trim')

    def test_unknown_write_payload_is_visible(self):
        self.write('schema.ts',IMPORT+"const rows=pgTable('rows',{id:text('id')}); function store(db:any,input:unknown){return db.insert(rows).values(input);}")
        self.assertIn('dynamic_write',{gap['kind'] for gap in self.extract()['gaps']})

    def test_missing_imported_type_remains_unknown(self):
        self.write('schema.ts',IMPORT+"import type {Opaque} from './missing';const rows=pgTable('rows',{data:jsonb('data').$type<Opaque>()});")
        field=self.extract()['tables'][0]['fields'][0]
        self.assertEqual(field['type']['kind'],'unknown')
        self.assertEqual(field['members'],[])

    def test_recursive_types_are_bounded_with_gaps(self):
        self.write('schema.ts',IMPORT+"interface Link { value:string; next:Link; } const rows=pgTable('rows',{data:jsonb('data').$type<Link>()});")
        self.assertIn('recursive_type',{gap['kind'] for gap in self.extract()['gaps']})

    def test_source_spans_and_fingerprints_change_with_writer(self):
        self.write('schema.ts',IMPORT+"export const rows=pgTable('rows',{id:text('id')});")
        writer=self.write('writer.ts',"import {rows} from './schema';const write=(db:any)=>db.insert(rows).values({id:'first'});")
        before=self.extract(); writer.write_text(writer.read_text().replace('first','second'));after=self.extract()
        before_sources={source['path']:source['sha256'] for source in before['sources']};after_sources={source['path']:source['sha256'] for source in after['sources']}
        self.assertNotEqual(before_sources['writer.ts'],after_sources['writer.ts'])
        evidence=after['tables'][0]['fields'][0]['writes'][0]['evidence'][0]
        self.assertEqual(evidence['sha256'],hashlib.sha256(writer.read_bytes()).hexdigest())
        self.assertEqual(writer.read_text()[evidence['start']:evidence['end']],"id:'second'")

    def test_structure_only_omits_writer_pass(self):
        self.write('schema.ts',IMPORT+"const rows=pgTable('rows',{id:text('id')});const write=(db:any)=>db.insert(rows).values({id:'value'});")
        self.assertEqual(self.extract({'structureOnly':True})['tables'][0]['fields'][0]['writes'],[])

    def test_external_symlink_is_not_read(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outside=pathlib.Path(elsewhere)/'outside.ts';outside.write_text(IMPORT+"const hidden=pgTable('hidden',{id:text('id')});")
            (self.repo/'link.ts').symlink_to(outside)
            self.assertEqual(self.extract()['tables'],[])

    def test_timestamp_options_are_not_stored_members(self):
        self.write('schema.ts',IMPORT+"const rows=pgTable('rows',{created:timestamp('created',{withTimezone:true})});")
        field=self.extract()['tables'][0]['fields'][0]
        self.assertEqual(field['members'],[])
        self.assertEqual(field['type']['name'],'Date')

    def test_mixed_union_members_keep_unique_paths_and_conditions(self):
        self.write('schema.ts',IMPORT+"type Subject={kind:'person';contact:string}|{kind:'company';legalName:string};const rows=pgTable('rows',{subject:jsonb('subject').$type<Subject>()});")
        members=self.extract()['tables'][0]['fields'][0]['members']
        self.assertEqual(len({member['path'] for member in members}),len(members))
        kind=next(member for member in members if member['name']=='kind')
        self.assertEqual(len(kind['branch_variants']),2)
        contact=next(member for member in members if member['name']=='contact')
        self.assertTrue(contact['optional'])
        self.assertEqual(contact['branch_variants'][0]['conditions'],[{'member':'kind','value':'person'}])

    def test_drizzle_output_directory_is_read_without_execution(self):
        self.write('drizzle.config.ts', "throw new Error('must not execute');const output='./history';export default defineConfig({schema:'./schema.ts',out:output});")
        self.write('schema.ts',IMPORT+"const rows=pgTable('current',{id:text('id')});")
        self.write('history/old.ts',IMPORT+"const rows=pgTable('historical',{id:text('id')});")
        self.assertEqual([table['name'] for table in self.extract()['tables']],['current'])

    def test_dynamic_drizzle_output_is_a_gap(self):
        self.write('drizzle.config.ts',"export default defineConfig({schema:'./schema.ts',out:process.env.HISTORY});")
        self.write('schema.ts',IMPORT+"const rows=pgTable('current',{id:text('id')});")
        self.assertIn('migration_configuration',{gap['kind'] for gap in self.extract()['gaps']})

    def test_vendor_cannot_be_explicitly_enabled(self):
        self.write('vendor/fixture.ts',IMPORT+"const rows=pgTable('excluded',{id:text('id')});")
        self.assertEqual(self.extract({'includeTests':['vendor/fixture.ts']})['tables'],[])

    def test_syntax_gap_has_a_complete_source_span(self):
        self.write('broken.ts',"export const broken = ;")
        span=next(gap for gap in self.extract()['gaps'] if gap['kind']=='syntax')['evidence'][0]
        self.assertTrue({'start_line','end_line','start_column','end_column','sha256','start','end'} <= set(span))

    def test_missing_integrity_entry_fails_before_compiler_loading(self):
        self.write('adapter.mjs', ADAPTER.read_text().replace("'./collect.mjs'", json.dumps((ADAPTER.parent / 'collect.mjs').as_uri())))
        self.write('vendor/typescript/INTEGRITY.json',json.dumps({'version':'6.0.2','files':{}}))
        result=subprocess.run(['node',str(self.repo/'adapter.mjs'),'--repo='+str(self.repo)],text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('integrity entry missing',result.stderr)

    def test_corrupt_compiler_fails_before_execution(self):
        self.write('adapter.mjs', ADAPTER.read_text().replace("'./collect.mjs'", json.dumps((ADAPTER.parent / 'collect.mjs').as_uri())))
        code="require('fs').writeFileSync("+json.dumps(str(self.repo/'EXECUTED'))+",'yes');"
        self.write('vendor/typescript/lib/typescript.js',code)
        names=['lib/typescript.js','lib/lib.esnext.d.ts','LICENSE.txt','package.json']
        self.write('vendor/typescript/INTEGRITY.json',json.dumps({'version':'6.0.2','files':{name:'0'*64 for name in names}}))
        result=subprocess.run(['node',str(self.repo/'adapter.mjs'),'--repo='+str(self.repo)],text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('integrity mismatch',result.stderr)
        self.assertFalse((self.repo/'EXECUTED').exists())

    def test_workspace_package_tsconfig_inheritance_is_static(self):
        self.write('packages/config/package.json','{"name":"@sample/config","exports":{"./base":"./base.json"}}')
        self.write('packages/config/base.json','{"compilerOptions":{"baseUrl":"../..","paths":{"@models/*":["models/*"]}}}')
        self.write('tsconfig.json','{"extends":"@sample/config/base"}')
        self.write('models/types.ts','export interface Contact { address:string; }')
        self.write('schema.ts',IMPORT+"import type {Contact} from '@models/types';const rows=pgTable('rows',{data:jsonb('data').$type<Contact>()});")
        result=self.extract()
        self.assertEqual(result['tables'][0]['fields'][0]['members'][0]['name'],'address')
        self.assertFalse(any(gap['kind']=='configuration_extends' for gap in result['gaps']))
        self.assertIn('packages/config/base.json',{source['path'] for source in result['sources']})

    def test_tsconfig_inheritance_cycle_is_a_gap(self):
        self.write('tsconfig.json','{"extends":"./base.json"}')
        self.write('base.json','{"extends":"./tsconfig.json"}')
        self.write('schema.ts',IMPORT+"const rows=pgTable('rows',{id:text('id')});")
        self.assertIn('configuration_cycle',{gap['kind'] for gap in self.extract()['gaps']})

    def collect(self):
        collector=ROOT/'plugins/privacy-datamap/scripts/collect.mjs'
        script='import {collectFacts} from '+json.dumps(collector.as_uri())+';process.stdout.write(JSON.stringify(collectFacts('+json.dumps(str(self.repo))+')));'
        result=subprocess.run(['node','--input-type=module','-e',script],text=True,capture_output=True,check=True)
        return json.loads(result.stdout)

    def test_collector_ignores_local_function_and_recognizes_import_alias(self):
        self.write('fake.ts',"function pgTable(name:string, values:object){return values;} const fake=pgTable('fake',{id:'example'});")
        self.write('schema.ts',"import {pgTable as define,text} from 'drizzle-orm/pg-core';const actual=define('actual',{id:text('id')});")
        report=self.collect()
        self.assertEqual([table['name'] for dataset in report['datasets'] for table in dataset['collections']],['actual'])
        self.assertFalse(any(gap['ref'].startswith('fake.ts:') for gap in report['coverage']['unparsed_candidates']))

    def test_collector_bare_fallback_retains_partial_coverage_gap(self):
        self.write('schema.ts',"const rows=pgTable('rows',{id:text('id'),...unknownColumns});")
        report=self.collect()
        self.assertEqual([table['name'] for dataset in report['datasets'] for table in dataset['collections']],['rows'])
        self.assertTrue(report['coverage']['unparsed_candidates'])

    def test_collector_and_adapter_agree_on_custom_migration_output(self):
        self.write('drizzle.config.ts',"export default defineConfig({schema:'./schema.ts',out:'./history'});")
        self.write('schema.ts',IMPORT+"const rows=pgTable('current',{id:text('id')});")
        self.write('history/old.ts',IMPORT+"const rows=pgTable('historical',{id:text('id')});")
        compiler=self.extract();collected=self.collect()
        self.assertEqual([table['name'] for table in compiler['tables']],['current'])
        self.assertEqual([table['name'] for dataset in collected['datasets'] for table in dataset['collections']],['current'])
        self.assertIn('history/old.ts',compiler['coverage']['migration_sources'])

    def test_security_literals_and_defaults_are_redacted_but_discriminators_remain(self):
        fixture_value='synthetic-redaction-marker-that-must-not-appear'
        self.write('schema.ts',IMPORT+"const rows=pgTable('rows',{apiToken:text('api_token').default('"+fixture_value+"'),kind:text('kind'),payload:jsonb('payload')});function store(db:any){return db.insert(rows).values({apiToken:'"+fixture_value+"',kind:'person',payload:{secretKey:'"+fixture_value+"',kind:'service'}});}")
        report=self.extract()
        self.assertNotIn(fixture_value,json.dumps(report))
        fields={field['property']:field for field in report['tables'][0]['fields']}
        self.assertIn('<redacted>',fields['apiToken']['shape'])
        self.assertEqual(fields['kind']['writes'][0]['value']['literal'],'person')
        payload=fields['payload']['writes'][0]['value']['members']
        self.assertEqual(next(member for member in payload if member['name']=='kind')['value']['literal'],'service')

    def test_static_reexport_and_local_constructor_alias(self):
        self.write('bridge.ts',"export {pgTable as table,text} from 'drizzle-orm/pg-core';")
        self.write('barrel.ts',"export * from './bridge';")
        self.write('schema.ts',"import {table as imported,text} from './barrel';const define=imported;const rows=define('actual',{id:text('id')});")
        compiler=self.extract();collected=self.collect()
        self.assertEqual([table['name'] for table in compiler['tables']],['actual'])
        self.assertEqual([table['name'] for dataset in collected['datasets'] for table in dataset['collections']],['actual'])

    def test_wrapped_constructor_has_an_explicit_coverage_gap(self):
        self.write('builder.ts',IMPORT+"export function define(name:string){return pgTable(name,{id:text('id')});}")
        self.write('schema.ts',"import {define} from './builder';const rows=define('records');")
        compiler=self.extract()
        self.assertIn('schema_wrapper',{gap['kind'] for gap in compiler['gaps']})
        self.assertEqual(compiler['tables'],[])

    def test_collector_unbound_security_defaults_are_redacted(self):
        fixture_value='synthetic-redaction-marker-in-unbound-builder'
        self.write('schema.ts',"const rows=pgTable('rows',{token:text('token').default('"+fixture_value+"')});")
        self.assertNotIn(fixture_value,json.dumps(self.collect()))

    def test_rails_migrate_directory_is_always_excluded(self):
        self.write('db/migrate/old.ts',IMPORT+"const old=pgTable('historical',{id:text('id')});")
        result=self.extract({'includeTests':['db/migrate/old.ts']})
        self.assertEqual(result['tables'],[])
        self.assertIn('db/migrate/old.ts',result['coverage']['migration_sources'])

    def test_flyway_repeatable_script_is_a_migration(self):
        self.write('sql/R__refresh_views.sql','CREATE TABLE historical (email TEXT);')
        self.assertIn('sql/R__refresh_views.sql',self.extract()['coverage']['migration_sources'])

    def test_shorthand_writer_follows_the_value_symbol(self):
        self.write('schema.ts',IMPORT+"const rows=pgTable('rows',{payload:jsonb('payload')});function store(db:any,email:string){const payload={email:email};return db.insert(rows).values({payload});}")
        value=self.extract()['tables'][0]['fields'][0]['writes'][0]['value']
        self.assertEqual(value['origin']['kind'],'object')
        self.assertEqual(value['origin']['members'][0]['name'],'email')

    def test_reviewed_migration_support_remains_compiler_evidence(self):
        source = IMPORT + "export const records=pgTable('records',{email:text('email')});"
        name = 'src/migrations/current.ts'
        self.write(name, source)
        self.write('src/migration-report.ts', 'export const report = true;')
        self.write('.noru/privacy-datamap.yml',
                   'migration_support:\n  - path: ' + name + '\n    sha256: "' + hashlib.sha256(source.encode()).hexdigest()
                   + '"\n    rationale: Synthetic current schema helper, not migration history.\n')
        result = self.extract()
        self.assertEqual([row['name'] for row in result['tables']], ['records'])
        self.assertIn(name, result['coverage']['migration_support'])
        self.assertIn('src/migration-report.ts', {row['path'] for row in result['sources']})
        import sys
        sys.path.insert(0, str(ADAPTER.parent))
        import field_review
        exclusions = {'paths': result['coverage']['migration_sources'],
                      'support_paths': result['coverage']['migration_support']}
        self.assertIn(name, field_review.source_inventory(self.repo, exclusions))
        self.assertEqual(field_review.evidence_errors(result['tables'][0]['source'], self.repo, exclusions), [])
        self.write(name, source + '// changed')
        with self.assertRaises(subprocess.CalledProcessError):
            self.extract()

    def test_repeated_run_is_deterministic(self):
        self.write('schema.ts',IMPORT+"const rows=pgTable('rows',{id:text('id')});")
        self.assertEqual(self.extract(),self.extract())


if __name__=='__main__':
    unittest.main()
