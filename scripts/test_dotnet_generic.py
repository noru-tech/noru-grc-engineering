#!/usr/bin/env python3
"""Generic .NET extraction, registration provenance and diagnostic contract tests."""
import json
import pathlib
import subprocess
import sys
import unittest
import test_dotnet as fixtures
SCRIPTS = fixtures.SCRIPTS
sys.path.insert(0,str(SCRIPTS))
import dotnet


class GenericTests(unittest.TestCase):
    setUp = fixtures.DotnetTests.setUp
    put = fixtures.DotnetTests.put
    scan = fixtures.DotnetTests.scan
    fixture = fixtures.DotnetTests.fixture

    def context(self, body, extra=''):
        self.put('App.csproj','<Project />')
        self.put('Data.cs','using Microsoft.EntityFrameworkCore; namespace Synthetic; class Row { public int Id { get; set; } } class Store : DbContext { protected override void OnModelCreating(ModelBuilder b) { '+body+' } } '+extra)
        self.put('Runtime.cs','using Synthetic; builder.AddNpgsqlDbContext<Store>("store");')

    def test_entity_and_applied_configuration_register_without_dbsets(self):
        self.context('b.Entity<Row>().ToTable("rows"); b.ApplyConfiguration(new OtherConfiguration());',
                     'class Other { public string Value { get; set; } } class OtherConfiguration : IEntityTypeConfiguration<Other> { public void Configure(EntityTypeBuilder<Other> c) { c.ToTable("others"); } } class Unused { public string Hidden { get; set; } } class UnusedConfiguration : IEntityTypeConfiguration<Unused> { public void Configure(EntityTypeBuilder<Unused> c) { c.ToTable("unused"); } }')
        report=self.scan()
        self.assertEqual({t['name'] for t in report['tables']},{'rows','others'})
        self.assertEqual({r['registrations'][0]['kind'] for r in report['relationships']},{'entity_call','applied_configuration'})
        self.assertEqual(report,self.scan())
        self.put('Events.cs','namespace Synthetic; interface Marker {} class Event : Marker {}')
        self.assertFalse(any(g['code']=='DOTNET_TYPE_AMBIGUOUS' for g in self.scan()['gaps']))

    def test_invoked_extensions_keep_call_chain_and_detect_cycle(self):
        self.context('b.RegisterRows();', 'static class Extensions { public static void RegisterRows(this ModelBuilder b) { b.More(); } public static void More(this ModelBuilder c) { c.Entity<Row>(r => { r.ToTable("rows"); }); } }')
        report=self.scan()
        self.assertEqual([t['name'] for t in report['tables']],['rows'])
        self.assertTrue(report['relationships'][0]['registrations'][0]['refs'])
        self.put('Data.cs',self.files['Data.cs'].replace('c.Entity<Row>(r => { r.ToTable("rows"); });','c.RegisterRows();'))
        report=self.scan()
        self.assertEqual(report['relationships'],[])
        self.assertTrue(any(g['code']=='DOTNET_EXTENSION_CYCLE' for g in report['gaps']))

    def test_ambiguous_extension_and_misleading_receiver_do_not_register(self):
        self.context('b.RegisterRows();', 'static class One { public static void RegisterRows(this ModelBuilder b) { b.Entity<Row>(); } } static class Two { public static void RegisterRows(this ModelBuilder b) { b.Entity<Row>(); } }')
        self.assertEqual(self.scan()['relationships'],[])
        self.assertTrue(any(g['code']=='DOTNET_EXTENSION_BINDING' for g in self.scan()['gaps']))
        self.context('other.Entity<Row>();')
        self.assertEqual(self.scan()['relationships'],[])
        self.context('b.Entity<Row>();','class DbContext {}')
        self.assertEqual(self.scan()['relationships'],[])
        self.context('b.Entity<Row>();')
        self.put('Runtime.cs','using Synthetic; Fake.AddNpgsqlDbContext<Store>("false_store"); static class Fake { public static void AddNpgsqlDbContext<T>(string name) {} }')
        self.assertTrue(any(g['code']=='DOTNET_REGISTRATION_BINDING' for g in self.scan()['gaps']))
        self.assertEqual(self.scan()['runtime_bindings'],[])

    def test_namespace_and_project_evidence_disambiguate_models(self):
        self.context('b.Entity<Contracts.Row>();')
        self.put('App.csproj','<Project><ItemGroup><ProjectReference Include="shared/Shared.csproj" /></ItemGroup></Project>')
        self.put('shared/Shared.csproj','<Project />')
        self.put('shared/Models.cs','namespace Contracts; class Row { public string Value { get; set; } }')
        self.put('unrelated/Other.csproj','<Project />')
        self.put('unrelated/Models.cs','namespace Contracts; class Row { public string Wrong { get; set; } }')
        self.assertEqual([f['name'] for f in self.scan()['tables'][0]['fields']],['Value'])
        self.put('App.csproj','<Project />')
        self.assertEqual(self.scan()['relationships'],[])

    def test_runtime_conditions_and_build_membership_are_distinct(self):
        self.context('b.Entity<Row>();')
        self.put('Runtime.cs','using Synthetic; if (flag) { builder.AddNpgsqlDbContext<Store>("left"); } else { builder.AddNpgsqlDbContext<Store>("right"); }')
        report=self.scan()
        self.assertEqual(len(report['runtime_bindings']),2)
        self.assertTrue(all(t['conditions'] for t in report['runtime_bindings']))
        self.assertTrue(any(g['code']=='DOTNET_RUNTIME_BRANCH' and g['category']=='ambiguous_binding' for g in report['gaps']))
        self.put('Conditional.cs','#if TEST\nclass BuildOnly {}\n#endif')
        self.assertTrue(any(g['category']=='source_membership_uncertainty' for g in self.scan()['gaps']))
        self.context('if (flag) { b.Entity<Row>(); }')
        self.assertEqual(self.scan()['relationships'],[])

    def test_external_framework_requires_supported_exact_version(self):
        self.put('App.csproj','<Project><ItemGroup><PackageReference Include="Microsoft.Extensions.Identity.Stores" Version="8.0.0" /></ItemGroup></Project>')
        self.put('Data.cs','using Microsoft.AspNetCore.Identity; using Microsoft.AspNetCore.Identity.EntityFrameworkCore; using Microsoft.EntityFrameworkCore; class Customer : IdentityUser { public string Custom { get; set; } } class Store : IdentityDbContext<Customer> {}')
        report=self.scan(); fields={f['name']:f for f in report['tables'][0]['fields']}
        self.assertIn('PasswordHash',fields)
        self.assertEqual(fields['Id']['clr_type'],'string')
        self.assertEqual(fields['Id']['framework_contract']['version'],'8.0.0')
        self.assertNotIn('categories',fields['PasswordHash'])
        self.put('App.csproj',self.files['App.csproj'].replace('8.0.0','99.0.0'))
        report=self.scan()
        self.assertEqual([f['name'] for f in report['tables'][0]['fields']],['Custom'])
        self.assertTrue(any(g['code']=='DOTNET_EXTERNAL_VERSION' for g in report['gaps']))
        diagnostic=next(g for g in report['gaps'] if g['code']=='DOTNET_EXTERNAL_VERSION')
        self.assertEqual(diagnostic['details']['observed_versions'],['99.0.0'])
        self.assertIn('8.0.0',diagnostic['details']['supported_versions'])

    def test_clr_storage_serialization_and_validation_stay_separate(self):
        self.context('b.Entity<Row>().Property(r => r.State).HasConversion<string>().HasColumnType("varchar(20)");', 'enum Status { One }')
        self.put('Data.cs',self.files['Data.cs'].replace('public int Id { get; set; }','[JsonIgnore][Required] public Status State { get; set; }'))
        field=self.scan()['tables'][0]['fields'][0]
        self.assertEqual(field['clr_type'],'Status')
        self.assertEqual(field['storage_type']['name'],'string')
        self.assertEqual(field['storage_type']['database_type'],'varchar(20)')
        self.assertEqual(field['serialization']['attributes'][0]['attribute'],'JsonIgnore')
        self.put('Data.cs',self.files['Data.cs'].replace('HasConversion<string>()','HasConversion(new CustomConverter())'))
        field=self.scan()['tables'][0]['fields'][0]
        self.assertEqual(field['storage_type']['kind'],'unknown')

    def test_relational_navigation_is_not_a_scalar_column(self):
        self.context('b.Entity<Row>(r => { r.HasOne(x => x.Parent).WithMany().HasForeignKey(x => x.ParentId); });', 'class Parent { public int Id { get; set; } }')
        self.put('Data.cs',self.files['Data.cs'].replace('class Row {','class Row { public Parent Parent { get; set; } public int ParentId { get; set; }'))
        report=self.scan()
        self.assertEqual({f['name'] for f in report['tables'][0]['fields']},{'Id','ParentId'})
        self.assertEqual(report['entity_relationships'][0]['foreign_key_members'],['ParentId'])
        self.assertEqual(report['entity_relationships'][0]['target_model'],'Synthetic.Parent')

    def test_inherited_backing_fields_keep_referenced_project_evidence(self):
        self.context('b.Entity<Row>().Property("_value").HasColumnName("stored_value");')
        self.put('App.csproj','<Project><ItemGroup><ProjectReference Include="shared/Shared.csproj" /></ItemGroup></Project>')
        self.put('shared/Shared.csproj','<Project />')
        self.put('shared/Base.cs','namespace Shared; public class Base { private string _value; }')
        self.put('Data.cs',self.files['Data.cs'].replace('class Row {','class Row : Shared.Base {'))
        field=next(f for f in self.scan()['tables'][0]['fields'] if f['name']=='stored_value')
        self.assertEqual(field['clr_type'],'string')
        self.assertEqual(field['evidence'][0]['path'],'shared/Base.cs')

    def test_diagnostics_group_occurrences_keep_stable_ids_and_freshness(self):
        self.context('b.Entity<Row>();')
        self.put('Data.cs',self.files['Data.cs'].replace('public int Id { get; set; }','public dynamic First { get; set; }\n public dynamic Second { get; set; }'))
        first=self.scan(); groups=[g for g in first['gaps'] if 'unknown additional' in g['reason']]
        self.assertEqual(len(groups),1)
        self.assertEqual(len(groups[0]['occurrences']),2)
        self.put('Data.cs','// moved\n'+self.files['Data.cs'])
        current=next(g for g in self.scan()['gaps'] if g['code']==groups[0]['code'])
        self.assertEqual(current['diagnostic_id'],groups[0]['diagnostic_id'])
        self.assertNotEqual(current['evidence'][0]['sha256'],groups[0]['evidence'][0]['sha256'])
        self.assertEqual(sum(first['diagnostic_summary']['by_category'].values()),len(first['gaps']))
        self.assertTrue(all(g['resolution_requirement'] and g['scope'] for g in first['gaps']))

    def test_primary_constructor_and_generic_source_inheritance(self):
        self.context('b.Entity<Row>();', 'class Base<T> { public T Key { get; set; } }')
        self.put('Data.cs',self.files['Data.cs'].replace('class Store : DbContext','class Store(DbContextOptions<Store> options) : DbContext(options)').replace('class Row {','class Row : Base<Guid> {'))
        report=self.scan()
        self.assertEqual(len(report['relationships']),1)
        self.assertEqual(next(f['clr_type'] for f in report['tables'][0]['fields'] if f['name']=='Key'),'Guid')

    def test_conditional_project_reference_is_not_an_evidenced_binding(self):
        self.context('b.Entity<Shared.Record>();')
        self.put('App.csproj','<Project><ItemGroup Condition="$(Flag)"><ProjectReference Include="shared/Shared.csproj" /></ItemGroup></Project>')
        self.put('shared/Shared.csproj','<Project />')
        self.put('shared/Model.cs','namespace Shared; class Record { public string Value { get; set; } }')
        report=self.scan()
        self.assertEqual(report['relationships'],[])
        self.assertTrue(any(g['category']=='source_membership_uncertainty' for g in report['gaps']))

    def test_model_builder_shadow_and_malformed_callback_stay_gaps(self):
        self.context('b.Entity<Row>();', 'class ModelBuilder {}')
        self.assertEqual(self.scan()['relationships'],[])
        self.assertTrue(any(g['code']=='DOTNET_MODEL_BUILDER_BINDING' for g in self.scan()['gaps']))
        self.context('b.Entity<Row>(')
        self.assertTrue(any(g['code']=='DOTNET_MODEL_SYNTAX' for g in self.scan()['gaps']))

    def test_central_framework_version_has_evidence_and_conditions_are_not_evaluated(self):
        self.put('App.csproj','<Project><ItemGroup><PackageReference Include="Microsoft.Extensions.Identity.Stores" /></ItemGroup></Project>')
        self.put('Directory.Packages.props','<Project><ItemGroup><PackageVersion Include="Microsoft.Extensions.Identity.Stores" Version="10.0.0" /></ItemGroup></Project>')
        self.put('Data.cs','using Microsoft.AspNetCore.Identity; using Microsoft.AspNetCore.Identity.EntityFrameworkCore; class Customer : IdentityUser { public string Custom { get; set; } } class Store : IdentityDbContext<Customer> {}')
        fields=self.scan()['tables'][0]['fields']
        key=next(f for f in fields if f['name']=='Id')
        self.assertIn('Directory.Packages.props:1',key['framework_contract']['package_refs'])
        self.put('Directory.Packages.props',self.files['Directory.Packages.props'].replace('<ItemGroup>','<ItemGroup Condition="$(UsePreview)">'))
        self.assertEqual([f['name'] for f in self.scan()['tables'][0]['fields']],['Custom'])

    def test_diagnostic_ids_and_representation_survive_collector_and_review(self):
        self.context('b.Entity<Row>();')
        self.put('Data.cs',self.files['Data.cs'].replace('public int Id','public dynamic Content { get; set; } public int Id'))
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True)
        result=subprocess.run(['node',str(SCRIPTS/'collect.mjs'),f'--repo={self.repo}','--output=json'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        derived=json.loads((self.repo/'.noru/.cache/privacy-datamap.derived.json').read_text())
        import field_review, review
        document={'observation_digest':'fixture'}
        contract=field_review.build_contract(document,self.repo,compiler_report={'tables':[],'gaps':[]},derived=derived)
        self.assertEqual({g['diagnostic_id'] for g in derived['dotnet']['gaps']},{g['gap_id'] for g in contract['gaps'] if g['kind']=='dotnet_extraction'})
        self.assertTrue(all(g['status']=='unresolved' for g in contract['gaps']))
        self.assertTrue(all(f['status']=='needs_investigation' for f in contract['fields']))
        self.assertTrue(any(fact['kind']=='persistence_representation' for fact in contract['facts'].values()))
        outputs=review.build_outputs(document,{'actions':[],'collection_review_required':[],'observation_digest':'fixture'},derived,{'dataset':[],'system':[]},[])
        self.assertIn('grouped items',review.render_outputs(outputs)[0])
        from jsonschema_mini import validate
        schema=json.loads((fixtures.ROOT/'contract/privacy-datamap-review.schema.json').read_text())
        self.assertEqual(validate(outputs,schema),[])


    def test_source_context_inheritance_and_explicit_base_call(self):
        self.context('base.OnModelCreating(b); b.Entity<Row>();', 'class BaseStore : DbContext { protected override void OnModelCreating(ModelBuilder b) { b.Entity<Extra>(); } } class Extra { public string Value { get; set; } }')
        self.put('Data.cs',self.files['Data.cs'].replace('class Store : DbContext','class Store : BaseStore'))
        report=self.scan()
        registrations=[r for r in report['relationships'] if r['model']=='Synthetic.Extra']
        self.assertEqual(len(registrations),2)
        self.assertTrue(all(r['registrations'] for r in registrations))

    def test_extension_traversal_limit_is_an_explicit_diagnostic(self):
        methods=' '.join('public static void Step'+str(i)+'(this ModelBuilder b) { b.Step'+str(i+1)+'(); }' for i in range(18))
        self.context('b.Step0();','static class Extensions { '+methods+' public static void Step18(this ModelBuilder b) { b.Entity<Row>(); } }')
        report=self.scan()
        self.assertEqual(report['relationships'],[])
        self.assertTrue(any(g['code']=='DOTNET_MODEL_TRAVERSAL_LIMIT' for g in report['gaps']))

    def test_backing_field_mismatch_and_owned_separate_table_remain_unknown(self):
        self.context('b.Entity<Row>().Property(r => r.Name).HasField("_value");')
        self.put('Data.cs',self.files['Data.cs'].replace('public int Id { get; set; }','private int _value; public string Name { get; set; }'))
        report=self.scan()
        self.assertEqual(report['tables'][0]['fields'][0]['storage_type']['kind'],'unknown')
        self.assertTrue(any(g['code']=='DOTNET_BACKING_BINDING' for g in report['gaps']))
        self.context('b.Entity<Row>().OwnsOne(r => r.Address);','[Table("addresses")] class Address { public string Street { get; set; } }')
        self.put('Data.cs',self.files['Data.cs'].replace('class Row {','class Row { public Address Address { get; set; }'))
        report=self.scan()
        self.assertFalse(any(f['name']=='Address_Street' for t in report['tables'] for f in t['fields']))
        self.assertTrue(any(g['code']=='DOTNET_OWNED_TABLE' for g in report['gaps']))

if __name__=='__main__': unittest.main()
