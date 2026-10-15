#!/usr/bin/env python3
"""Offline .NET model, datastore identity, and coverage regressions."""
import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT/'plugins/privacy-datamap/scripts'
sys.path.insert(0,str(SCRIPTS))
import dotnet


class DotnetTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.repo=pathlib.Path(tmp.name)
        self.files={}

    def put(self,path,text):
        self.files[path]=text
        full=self.repo/path; full.parent.mkdir(parents=True,exist_ok=True); full.write_text(text)

    def scan(self):
        return dotnet.discover(self.repo,list(self.files))

    def fixture(self):
        self.put('src/App.csproj','<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="Microsoft.EntityFrameworkCore.SqlServer" Version="10.0.0" /></ItemGroup></Project>')
        self.put('src/Entities.cs','''using Microsoft.EntityFrameworkCore;
using System.ComponentModel.DataAnnotations.Schema;
namespace Example;
[Table("records", Schema = "left")]
public class Item {
 public int Id { get; set; }
 [Column("contact")] public string? Email { get; set; }
 [NotMapped] public string Temporary { get; set; }
}
public class FirstContext : DbContext { public DbSet<Item> Items { get; set; } }
public class SecondContext : DbContext { public DbSet<Item> Items { get; set; } }
''')
        self.put('src/Program.cs','''using Microsoft.EntityFrameworkCore;
services.AddDbContext<FirstContext>(o => o.UseSqlServer(configuration.GetConnectionString("Primary")));
services.AddDbContext<SecondContext>(o => o.UseSqlServer(configuration.GetConnectionString("Secondary")));
throw new Exception("target must not run");
''')

    def test_context_fields_and_config_reference_identities(self):
        self.fixture(); report=self.scan()
        self.assertEqual(len(report['observations']),2)
        self.assertEqual(report['gaps'],[])
        self.assertEqual(len({r['boundary'] for r in report['observations']}),2)
        table=report['observations'][0]['collections'][0]
        self.assertEqual(table['name'],'left.records')
        self.assertEqual([f['name'] for f in table['fields']],['Id','contact'])
        self.assertTrue(report['tables'][0]['fields'][1]['optional'])
        self.assertIn('src/Program.cs:2',report['observations'][0]['binding_refs'])
        self.assertEqual(report,self.scan())
        self.put('src/Program.cs',self.files['src/Program.cs'].replace('"Secondary"','"Primary"'))
        self.assertEqual(len({r['boundary'] for r in self.scan()['observations']}),1)

    def test_global_ef_imports_bind_contexts_with_evidence(self):
        self.fixture()
        self.put('src/Entities.cs', self.files['src/Entities.cs'].replace('using Microsoft.EntityFrameworkCore;', ''))
        self.assertEqual(self.scan()['relationships'], [])
        for namespace in ('Microsoft.EntityFrameworkCore', 'global::Microsoft.EntityFrameworkCore', 'System.Data.Entity'):
            self.put('src/ZGlobalUsings.cs', '// compilation imports\nglobal using ' + namespace + ';')
            report = self.scan()
            self.assertEqual(len(report['relationships']), 2)
            self.assertEqual(report['gaps'], [])
            for edge in report['relationships']:
                self.assertIn('src/ZGlobalUsings.cs:2', edge['refs'])
            for observation in report['observations']:
                self.assertIn('src/ZGlobalUsings.cs:2', observation['binding_refs'])
                self.assertEqual([f['name'] for f in observation['collections'][0]['fields']], ['Id', 'contact'])
        self.put('src/ZGlobalUsings.cs', '')
        self.assertEqual(self.scan()['relationships'], [])

    def test_global_import_does_not_bind_foreign_qualified_context(self):
        self.fixture()
        self.put('src/Entities.cs', self.files['src/Entities.cs'].replace('using Microsoft.EntityFrameworkCore;', '').replace(': DbContext', ': Other.DbContext'))
        self.put('src/GlobalUsings.cs', 'global using Microsoft.EntityFrameworkCore;')
        self.assertEqual(self.scan()['relationships'], [])

    def test_global_imports_do_not_cross_project_boundaries(self):
        self.fixture()
        self.put('src/Entities.cs', self.files['src/Entities.cs'].replace('using Microsoft.EntityFrameworkCore;', ''))
        self.put('shared/Shared.csproj', '<Project />')
        self.put('shared/GlobalUsings.cs', 'global using Microsoft.EntityFrameworkCore;')
        self.put('src/App.csproj', '<Project><ItemGroup><ProjectReference Include="../shared/Shared.csproj" /></ItemGroup></Project>')
        self.assertEqual(self.scan()['relationships'], [])
        self.put('src/Other.csproj', '<Project />')
        self.put('src/GlobalUsings.cs', 'global using Microsoft.EntityFrameworkCore;')
        self.assertEqual(self.scan()['relationships'], [])

    def test_global_imports_reject_non_imports_and_conditional_sources(self):
        self.fixture()
        self.put('src/Entities.cs', self.files['src/Entities.cs'].replace('using Microsoft.EntityFrameworkCore;', ''))
        for source in (
            'using Microsoft.EntityFrameworkCore;',
            '// global using Microsoft.EntityFrameworkCore;',
            'string text = "global using Microsoft.EntityFrameworkCore;";',
            'global using EF = Microsoft.EntityFrameworkCore;',
            'global using static Microsoft.EntityFrameworkCore;',
            '#if FEATURE\nglobal using Microsoft.EntityFrameworkCore;\n#endif',
            'namespace Other { global using Microsoft.EntityFrameworkCore; }',
        ):
            with self.subTest(source=source):
                self.put('src/GlobalUsings.cs', source)
                self.assertEqual(self.scan()['relationships'], [])

    def test_interpolated_and_raw_messages_preserve_enum_declarations(self):
        self.fixture()
        self.put('src/Types.cs', 'namespace Example; public enum WebhookType : byte { Created, Updated }')
        original = self.files['src/Entities.cs'].replace('public int Id', 'public WebhookType Kind { get; set; } public int Id')
        messages = [
            '$"Transaction {transaction.Id} is not current"',
            '$"Nested {Call("inner", new { X = 1 })} {{escaped}}"',
            '$@"line one\n{Call(@"nested ""quote""")} line two"',
            '@$"{Call($"{value}")}"',
            '"""raw text with "quotes" and {braces}"""',
            '$$"""raw {{Call("nested")}} {literal}"""',
        ]
        for message in messages:
            with self.subTest(message=message):
                self.put('src/Entities.cs', original + '\nclass Diagnostic { void Fail() { throw new Exception('+message+'); } }')
                report = self.scan()
                self.assertEqual(len(report['relationships']), 2)
                kind = report['tables'][0]['fields'][0]
                self.assertEqual(kind['type']['kind'], 'scalar')
                self.assertEqual(kind['shape'], 'WebhookType')
                self.assertNotIn('Transaction', json.dumps(report))
                self.assertEqual(report['gaps'], [])
        self.put('src/Entities.cs', original + '\nstring broken = $"{Call("inner")};')
        self.assertEqual(self.scan()['relationships'], [])
        self.assertTrue(any('Unterminated' in g['reason'] for g in self.scan()['gaps']))

    def test_dynamic_mapping_and_ambiguous_enum_remain_gaps(self):
        self.fixture()
        self.put('src/Entities.cs', self.files['src/Entities.cs'].replace('"records"', '$"{table}"'))
        self.assertEqual(self.scan()['relationships'], [])
        self.assertTrue(any('literal' in g['reason'] for g in self.scan()['gaps']))
        self.fixture()
        self.put('src/Entities.cs', self.files['src/Entities.cs'].replace('public int Id', 'public WebhookType Kind { get; set; } public int Id'))
        self.put('src/Types.cs', 'namespace One { enum WebhookType { A } } namespace Two { enum WebhookType { B } }')
        self.assertTrue(any('ambiguous' in g['reason'] for g in self.scan()['gaps']))
        self.put('src/Types.cs', '#if FEATURE\nenum WebhookType { A }\n#endif')
        self.assertTrue(any('preprocessor' in g['reason'] for g in self.scan()['gaps']))

    def test_postgres_registration_across_project_reference_and_aspire(self):
        self.fixture()
        self.put('src/Program.cs', '')
        self.put('api/Api.csproj', '<Project><ItemGroup><ProjectReference Include="../src/App.csproj" /></ItemGroup></Project>')
        self.put('api/Startup.cs', 'builder.AddNpgsqlDbContext<FirstContext>("main"); services.AddDbContext<SecondContext>(o => o.UseNpgsql(configuration.GetConnectionString("main")));')
        report = self.scan()
        self.assertEqual(len({r['dataset'] for r in report['relationships']}), 1)
        self.assertTrue(all('api/Startup.cs:1' in r['refs'] for r in report['relationships']))
        self.put('host/Host.csproj', '<Project />')
        self.put('host/App.cs', 'var server = builder.AddPostgres("server"); var db = server.AddDatabase("main"); var api = builder.AddDotnetProject("api", "../api").WithReference(db);')
        hosted = self.scan()
        self.assertEqual(len({r['dataset'] for r in hosted['relationships']}), 1)
        self.assertNotEqual(hosted['relationships'][0]['dataset'], report['relationships'][0]['dataset'])
        self.assertTrue(all('host/App.cs:1' in r['refs'] for r in hosted['relationships']))
        self.put('api/Program.cs', 'var builder = WebApplication.CreateBuilder(args); builder.Build().Run();')
        script = "import {collectFacts} from './plugins/privacy-datamap/scripts/collect.mjs'; console.log(JSON.stringify(collectFacts(process.argv[1])));"
        collected = subprocess.run(['node', '--input-type=module', '-e', script, str(self.repo)], cwd=SCRIPTS.parents[2], capture_output=True, text=True)
        self.assertEqual(collected.returncode, 0, collected.stderr)
        facts = json.loads(collected.stdout)
        runtime = next(s for s in facts['systems'] if s['name'] == 'api')
        self.assertTrue(runtime['dataset_references'])
        self.assertTrue(set(runtime['dataset_references']) <= {d['fides_key'] for d in facts['datasets']})
        self.put('api/Startup.cs', self.files['api/Startup.cs'].replace('GetConnectionString("main")', 'GetConnectionString("other")'))
        self.assertEqual(len({r['dataset'] for r in self.scan()['relationships']}), 2)

    def test_dynamic_or_conflicting_connections_and_reassigned_host_remain_unresolved(self):
        self.fixture()
        self.put('src/Program.cs', 'builder.AddNpgsqlDbContext<FirstContext>($"{name}"); builder.AddNpgsqlDbContext<SecondContext>("a"); builder.AddNpgsqlDbContext<SecondContext>("b");')
        report = self.scan()
        self.assertTrue(any('dynamic or unsupported' in g['reason'] for g in report['gaps']))
        self.assertTrue(any('unresolved or ambiguous' in g['reason'] for g in report['gaps']))
        self.put('src/Program.cs', 'builder.AddNpgsqlDbContext<FirstContext>("main");')
        self.put('host/Host.csproj', '<Project />')
        self.put('host/App.cs', 'var server = builder.AddPostgres("server"); var db = server.AddDatabase("main"); db = Other(); var app = builder.AddDotnetProject("app", "../src").WithReference(db);')
        self.assertFalse(any('host/App.cs:1' in r['refs'] for r in self.scan()['relationships']))

    def test_inherited_fields_keep_origin_and_missing_framework_stays_gap(self):
        self.fixture()
        self.put('src/Base.cs', 'namespace Example; public abstract class Base { int _id; public virtual int Key { get { return _id; } protected set { _id = value; } } public string Shared { get; set; } }')
        self.put('src/Entities.cs', self.files['src/Entities.cs'].replace('class Item {', 'class Item : Base {'))
        report = self.scan()
        fields = {f['name']: f for f in report['tables'][0]['fields']}
        self.assertIn('Key', fields)
        self.assertEqual(fields['Shared']['evidence'][0]['path'], 'src/Base.cs')
        self.put('src/Base.cs', 'namespace Example; public abstract class Base : MissingBase { public string Shared { get; set; } }')
        self.assertTrue(any('not synthesized' in g['reason'] for g in self.scan()['gaps']))
        self.put('src/Base.cs', 'namespace Example; public class Base : Item { public string Shared { get; set; } }')
        self.assertTrue(any('Cyclic' in g['reason'] for g in self.scan()['gaps']))

    def test_identity_extracts_only_source_declared_custom_fields(self):
        self.put('App.csproj', '<Project />')
        self.put('Data.cs', 'using Microsoft.EntityFrameworkCore; using Microsoft.AspNetCore.Identity.EntityFrameworkCore; class Customer : IdentityUser { public string Street { get; set; } } class Store : IdentityDbContext<Customer> {}')
        self.put('Program.cs', 'builder.AddNpgsqlDbContext<Store>("customers");')
        report = self.scan()
        self.assertEqual([f['name'] for f in report['tables'][0]['fields']], ['Street'])
        self.assertEqual(report['relationships'][0]['kind'], 'identity_user_model')
        self.assertTrue(any('framework table names' in g['reason'] for g in report['gaps']))
        self.assertNotIn('AspNetUsers', json.dumps(report))
        self.assertNotIn('PasswordHash', json.dumps(report))
        self.put('More.cs', 'class Customer { public string Other { get; set; } }')
        self.assertEqual(self.scan()['relationships'], [])

    def fluent_fixture(self):
        self.fixture()
        self.put('src/Entities.cs', self.files['src/Entities.cs'].replace('public int Id', 'private string _token; public Address Address { get; set; } public Status Status { get; set; } public int Id').replace('public DbSet<Item> Items { get; set; }', 'public DbSet<Item> Items { get; set; } protected override void OnModelCreating(ModelBuilder b) { b.HasDefaultSchema("sales"); b.ApplyConfiguration(new ItemConfiguration()); }'))
        self.put('src/Types.cs', 'namespace Example; public enum Status { Pending, Done } public class Address { public string Street { get; set; } public string City { get; set; } }')
        self.put('src/Mapping.cs', '''namespace Example;
class ItemConfiguration : IEntityTypeConfiguration<Item> {
 public void Configure(EntityTypeBuilder<Item> b) {
 b.ToTable("items", "sales");
 b.Ignore(x => x.Id);
 b.Property(x => x.Email).HasColumnName("email_address");
 b.Property("_token").HasColumnName("token");
 b.Property<int>("ParentId");
 b.OwnsOne(x => x.Address);
 b.Property(x => x.Status).HasConversion<string>();
 }
}''')

    def test_applied_fluent_mapping_resolves_storage_shape_and_evidence(self):
        self.fluent_fixture()
        report = self.scan()
        self.assertEqual(report['gaps'], [])
        for table in report['tables']:
            self.assertEqual(table['name'], 'sales.items')
            fields = {f['name']: f for f in table['fields']}
            self.assertEqual(set(fields), {'Address_Street','Address_City','Status','email_address','token','ParentId'})
            self.assertEqual(fields['Status']['type']['name'], 'string')
            self.assertTrue(any(e['path'] == 'src/Mapping.cs' for e in fields['Status']['evidence']))
            self.assertEqual(fields['token']['type']['name'], 'string')
            self.assertEqual(fields['ParentId']['type']['name'], 'int')
        self.assertTrue(all(any(ref.startswith('src/Mapping.cs:') for ref in r['refs']) for r in report['relationships']))
        self.put('src/Unused.cs', 'namespace Example; class Unused : IEntityTypeConfiguration<Item> { public void Configure(EntityTypeBuilder<Item> b) { b.ToTable("wrong"); } }')
        self.assertTrue(all(t['name'] == 'sales.items' for t in self.scan()['tables']))

    def test_fluent_dynamic_conditional_and_custom_conversion_stay_gaps(self):
        self.fluent_fixture()
        mapping = self.files['src/Mapping.cs']
        self.put('src/Mapping.cs', mapping.replace('b.ToTable("items", "sales");','if (flag) { b.ToTable("conditional"); }').replace('.HasConversion<string>()','.HasConversion(new CustomConverter())'))
        report = self.scan()
        self.assertFalse(any(t['name'] == 'conditional' for t in report['tables']))
        self.assertTrue(any('Conditional' in g['reason'] for g in report['gaps']))
        self.assertTrue(any('Custom conversion' in g['reason'] for g in report['gaps']))
        self.put('src/Mapping.cs', mapping.replace('b.OwnsOne(x => x.Address);', 'b.OwnsOne(x => x.Address, a => { a.ToTable("addresses"); });'))
        report = self.scan()
        self.assertFalse(any(f['name']=='Address_Street' for t in report['tables'] for f in t['fields']))
        self.assertTrue(any('Unsupported fluent' in g['reason'] for g in report['gaps']))
        self.put('src/Mapping.cs', mapping.replace('b.Property(x => x.Email)', 'b.Property(x => x.Email'))
        self.assertTrue(any('Malformed or unsupported fluent' in g['reason'] for g in self.scan()['gaps']))

    def test_fluent_multiple_configurations_do_not_choose_precedence(self):
        self.fluent_fixture()
        self.put('src/Entities.cs', self.files['src/Entities.cs'].replace('b.ApplyConfiguration(new ItemConfiguration());', 'b.ApplyConfiguration(new ItemConfiguration()); b.ApplyConfiguration(new OtherConfiguration());'))
        self.put('src/Other.cs', 'namespace Example; class OtherConfiguration : IEntityTypeConfiguration<Item> { public void Configure(EntityTypeBuilder<Item> b) { b.ToTable("other"); } }')
        report = self.scan()
        self.assertTrue(any('Multiple configurations' in g['reason'] for g in report['gaps']))
        self.assertFalse(any(t['name'] in {'sales.items','sales.other'} for t in report['tables']))

    def test_fluent_fields_reach_canonical_review_without_becoming_accepted(self):
        self.fluent_fixture()
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True)
        result=subprocess.run(['node',str(SCRIPTS/'collect.mjs'),f'--repo={self.repo}','--output=json'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        derived=json.loads((self.repo/'.noru/.cache/privacy-datamap.derived.json').read_text())
        import field_review
        contract=field_review.build_contract({'observation_digest':'fixture'},self.repo,compiler_report={'tables':[],'gaps':[]},derived=derived)
        rows={row['name']:row for row in contract['fields']}
        self.assertEqual(set(rows), {'Address_Street','Address_City','Status','email_address','token','ParentId'})
        self.assertEqual(rows['Status']['type']['name'], 'string')
        self.assertTrue(all(r['status']=='needs_investigation' for r in contract['fields']))
        previous = field_review.source_inventory(self.repo)
        self.put('src/Mapping.cs', self.files['src/Mapping.cs'].replace('email_address','email'))
        self.assertNotEqual(previous, field_review.source_inventory(self.repo))

    def test_mongo_nested_members_and_receiver_scopes(self):
        self.put('App.csproj','<Project />')
        self.put('Models.cs','''using MongoDB.Driver;
using MongoDB.Bson.Serialization.Attributes;
public record Address(string City, string? Street);
public class Document {
 public string Id { get; set; }
 [BsonElement("address")] public Address Location { get; set; }
 public List<string> Tags { get; set; }
 [BsonIgnore] public string Transient { get; set; }
 public Dictionary<string, object> Extra { get; set; }
}
public class Repository {
 public void First() { var db = client.GetDatabase("left"); var rows = db.GetCollection<Document>("documents"); }
 public void Second() { var db = client.GetDatabase("right"); var rows = db.GetCollection<Document>("documents"); }
}
''')
        report=self.scan()
        self.assertEqual(len(report['observations']),2)
        self.assertEqual(len({r['boundary'] for r in report['observations']}),2)
        fields={f['name']:f for f in report['tables'][0]['fields']}
        self.assertEqual(set(fields),{'_id','address','Tags','Extra'})
        self.assertEqual([f['name'] for f in fields['address']['members']],['City','Street'])
        self.assertEqual(fields['Tags']['type']['kind'],'array')
        self.assertTrue(fields['Extra']['type']['open'])
        self.assertTrue(any('content contract' in g['reason'] for g in report['gaps']))

    def test_dynamo_mapping_not_ordinary_dtos(self):
        self.put('App.csproj','<Project />')
        self.put('Records.cs','''using Amazon.DynamoDBv2.DataModel;
[DynamoDBTable("records")]
public class Record {
 [DynamoDBHashKey("pk")] public string Id { get; set; }
 [DynamoDBProperty("display_name")] public string Name { get; set; }
 [DynamoDBIgnore] public string Scratch { get; set; }
}
public class Response { public string Secret { get; set; } }
''')
        report=self.scan()
        self.assertEqual(len(report['observations']),1)
        self.assertEqual([f['name'] for f in report['tables'][0]['fields']],['pk','display_name'])
        self.assertEqual(report['relationships'][0]['physical_identity'],'unverified')

    def test_literal_dapper_projection(self):
        self.put('App.csproj','<Project />')
        self.put('Queries.cs','''using Dapper;
public record Row(int Id, string Email);
public class Queries { public void Load() { var rows = connection.Query<Row>("SELECT [Id], [Email] FROM [dbo].[records]"); } }
''')
        report=self.scan()
        self.assertEqual(report['relationships'][0]['kind'],'literal_query_result')
        self.assertEqual(report['observations'][0]['collections'][0]['name'],'dbo.records')
        self.assertTrue(any('complete database schema' in g['reason'] for g in report['gaps']))
        self.put('Queries.cs',self.files['Queries.cs'].replace('public record Row(int Id, string Email);',
            'public class Row { public int Id { get; set; } [Column("different")] public string Email { get; set; } }'))
        mapped=self.scan()
        self.assertEqual([f['name'] for f in mapped['tables'][0]['fields']],['Id','Email'])
        self.assertTrue(any('Unsupported member attribute' in g['reason'] for g in mapped['gaps']))
        self.put('Queries.cs',self.files['Queries.cs'].replace('"SELECT [Id], [Email] FROM [dbo].[records]"','sql'))
        self.assertEqual(self.scan()['observations'],[])
        self.assertTrue(self.scan()['gaps'])

    def test_comments_strings_generated_and_dynamic_never_become_models(self):
        self.put('App.csproj','<Project />')
        self.put('Text.cs','''// public class Fake : DbContext { public DbSet<User> Users { get; set; } }
string text = "DbContext DbSet MongoDB GetCollection";
''')
        self.put('obj/Generated.cs','public class Fake : DbContext {}')
        report=self.scan()
        self.assertEqual(report['observations'],[]); self.assertEqual(report['technologies'],[])
        self.put('Dynamic.cs','#if FEATURE\npublic class Hidden {}\n#endif')
        self.assertTrue(any(g['ref']=='Dynamic.cs:1' for g in self.scan()['gaps']))

    def test_project_references_and_ambiguous_models(self):
        self.fixture()
        self.put('shared/Shared.csproj','<Project />')
        self.put('shared/Item.cs','namespace Example; public class Item { public string Other { get; set; } }')
        self.assertEqual(len(self.scan()['observations']),2)
        self.put('src/Duplicate.cs','namespace Example; public class Item { public string Other { get; set; } }')
        report=self.scan(); self.assertEqual(report['observations'],[])
        self.assertTrue(any('ambiguous' in g['reason'] for g in report['gaps']))

    def test_msbuild_not_evaluated_and_outside_paths_not_read(self):
        self.put('App.csproj','''<Project><Import Project="../outside.props" /><ItemGroup Condition="x"><PackageReference Include="MongoDB.Driver" /></ItemGroup><Target Name="Build"><Exec Command="touch MUST_NOT_EXIST" /></Target></Project>''')
        report=self.scan()
        self.assertTrue(report['gaps']); self.assertFalse((self.repo/'MUST_NOT_EXIST').exists())
        self.assertEqual(report['technologies'][0]['evidence_kind'],'dependency')
        (self.repo/'Link.cs').symlink_to('/etc/passwd'); self.files['Link.cs']=''
        self.assertNotIn('root:',json.dumps(self.scan()))
        self.put('App.csproj','<!DOCTYPE Project [<!ENTITY x SYSTEM "file:///etc/passwd">]><Project>&x;</Project>')
        self.assertTrue(any('XML' in g['reason'] for g in self.scan()['gaps']))

    def test_secrets_absent_and_fluent_limitations_visible(self):
        self.fixture()
        self.put('src/Program.cs','''using Microsoft.EntityFrameworkCore;
services.AddDbContext<FirstContext>(o => o.UseSqlServer("Server=host;Password=DO_NOT_EXPORT_THIS"));
''')
        self.put('src/Override.cs','class Mapping : IEntityTypeConfiguration<Item> { void Configure() { builder.ToTable("changed"); } }')
        report=self.scan(); self.assertNotIn('DO_NOT_EXPORT_THIS',json.dumps(report))
        self.assertTrue(any('override' in g['reason'] for g in report['gaps']))
        self.assertTrue(any('connection target' in g['reason'] for g in report['gaps']))

    def test_collector_excludes_generated_test_and_migration_sources(self):
        self.fixture()
        self.put('Workspace.slnx','<Solution><Project Path="src/App.csproj" /><Project Path="tests/Test.csproj" /></Solution>')
        for name in ['tests/Test.csproj','tests/Data.cs','src/bin/Generated.cs','src/obj/Generated.cs','src/Migrations/Initial.cs']:
            self.put(name,'<Project />' if name.endswith('.csproj') else 'using Amazon.DynamoDBv2.DataModel; [DynamoDBTable("excluded")] public class Hidden { public string Value { get; set; } }')
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True)
        program='import {collectFacts} from '+json.dumps((SCRIPTS/'collect.mjs').as_uri())+'; console.log(JSON.stringify(collectFacts('+json.dumps(str(self.repo))+')));'
        result=subprocess.run(['node','--input-type=module','-e',program],capture_output=True,text=True,check=True)
        derived=json.loads(result.stdout)
        self.assertEqual(derived['counts']['datasets'],2)
        self.assertNotIn('excluded',{c['name'] for d in derived['datasets'] for c in d['collections']})
        self.assertFalse(any('Solution references' in g['reason'] for g in derived['dotnet']['gaps']))

    def test_solutions_references_and_boundaries(self):
        self.put('Workspace.slnx','<Solution><Project Path="app/App.csproj" /><Project Path="missing/Missing.csproj" /></Solution>')
        self.put('app/App.csproj','<Project><ItemGroup><ProjectReference Include="../shared/Shared.csproj" /></ItemGroup></Project>')
        self.put('shared/Shared.csproj','<Project />')
        self.put('shared/Item.cs','namespace Shared; public class Item { public string Value { get; set; } }')
        self.put('app/Context.cs','using Microsoft.EntityFrameworkCore; class Store : DbContext { public DbSet<Shared.Item> Items { get; set; } }')
        report=self.scan()
        self.assertEqual(report['solutions'][0]['project_references'],['app/App.csproj'])
        self.assertEqual(report['observations'][0]['path'],'shared/Item.cs')
        self.assertTrue(any('Solution references' in g['reason'] for g in report['gaps']))
        self.put('app/Other.csproj','<Project />')
        report=self.scan()
        self.assertEqual(report['observations'],[])
        self.assertTrue(any('Multiple projects' in g['reason'] for g in report['gaps']))

    def test_mongo_reassignments_and_dynamo_same_name_do_not_merge(self):
        self.put('App.csproj','<Project />')
        self.put('Data.cs','''using MongoDB.Driver;
using Amazon.DynamoDBv2.DataModel;
[DynamoDBTable("records")] public class Left { public string LeftValue { get; set; } }
[DynamoDBTable("records")] public class Right { public string RightValue { get; set; } }
class Repo { void Read() { var db = client.GetDatabase("records"); db = Other(); var c = db.GetCollection<Left>("items"); } }
''')
        report=self.scan()
        self.assertEqual(len(report['observations']),2)
        self.assertEqual(len({r['boundary'] for r in report['observations']}),2)
        self.assertTrue(any('reassigned' in g['reason'] for g in report['gaps']))

    def test_nested_fields_reach_review_and_source_changes_invalidate(self):
        self.put('App.csproj','<Project />')
        self.put('Data.cs','''using Amazon.DynamoDBv2.DataModel;
public record Detail(string Name, string? Other);
[DynamoDBTable("items")] public class Item { public Detail Data { get; set; } public List<string> Tags { get; set; } }
''')
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True)
        result=subprocess.run(['node',str(SCRIPTS/'collect.mjs'),f'--repo={self.repo}','--output=json'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        derived=json.loads((self.repo/'.noru/.cache/privacy-datamap.derived.json').read_text())
        import field_review
        document={'observation_digest':'fixture'}
        contract=field_review.build_contract(document,self.repo,compiler_report={'tables':[],'gaps':[]},derived=derived)
        rows={row['name']:row for row in contract['fields']}
        self.assertEqual([r['name'] for r in rows['Data']['nested_members']],['Name','Other'])
        self.assertTrue(rows['Tags']['scalar_array'])
        import review
        result={'actions': [], 'collection_review_required': [], 'observation_digest': 'fixture'}
        output=review.build_outputs(document,result,derived,{'dataset':[],'system':[]},[])
        rendered=review.render_outputs(output)[0]
        self.assertIn('.NET discovery and modeled relationships',rendered)
        self.assertIn('physical identity unverified',rendered)
        sys.path.insert(0,str(ROOT/'scripts'))
        from jsonschema_mini import validate
        schema=json.loads((ROOT/'contract/privacy-datamap-review.schema.json').read_text())
        self.assertEqual(validate(output,schema),[])

    def test_explicit_relationship_binding_overrides_modeled_identity(self):
        self.fixture()
        import dependencies
        import reconcile
        graph={'nodes':[
            {'id':'runtime','kind':'runtime','key':'repository'},
            {'id':'client','kind':'client'},
            {'id':'connection','kind':'connection'},
            {'id':'store','kind':'datastore','key':'reviewed_store'},
            {'id':'schema','kind':'schema','paths':['src/Entities.cs']}], 'edges':[]}
        specs=[]
        for index,(source,target) in enumerate([('runtime','client'),('client','connection'),('connection','store'),('client','schema')]):
            identity='edge_'+str(index)
            spec={'id':identity,'path':'src/Program.cs','method':'text','targets':['relationship:'+identity]}
            spec['fingerprint']=dependencies.observe(self.repo,spec)['fingerprint']; specs.append(spec)
            graph['edges'].append({'id':identity,'source':source,'target':target,'dependencies':[identity],'rationale':'Synthetic reviewed connection mapping.','confidence':'high'})
        self.put('.noru/privacy-datamap.yml',reconcile.to_yaml({'relationship_graph':graph,'evidence_dependencies':specs}))
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True)
        # collectFacts avoids treating this minimal graph fixture as an accepted full manifest.
        program='import {collectFacts} from '+json.dumps((SCRIPTS/'collect.mjs').as_uri())+'; console.log(JSON.stringify(collectFacts('+json.dumps(str(self.repo))+')));'
        result=subprocess.run(['node','--input-type=module','-e',program],capture_output=True,text=True,check=True)
        derived=json.loads(result.stdout)
        self.assertEqual([d['fides_key'] for d in derived['datasets']],['reviewed_store'])
        self.assertEqual({t['dataset'] for t in derived['dotnet']['tables']},{'reviewed_store'})
        self.assertEqual({b['datastore'] for b in derived['dotnet']['runtime_bindings']},{'reviewed_store'})
        import field_review
        contract=field_review.build_contract({'observation_digest':'fixture'},self.repo,compiler_report={'tables':[],'gaps':[]},derived=derived)
        self.assertEqual(len(contract['fields']),2)
        self.assertFalse(any(g['kind']=='identity_ambiguity' for g in contract['gaps']))

    def test_limits_remain_gaps_and_initializer_values_stay_private(self):
        self.put('App.csproj','<Project />')
        self.put('Data.cs','''using Amazon.DynamoDBv2.DataModel;
using Alias = Example.Other;
[DynamoDBTable("items")] public partial class Item : Base {
 public string Name { get; init; } = "PRIVATE_INITIALIZER_VALUE";
 public string Computed { get { return "PRIVATE_VALUE"; } }
 public Item Child { get; set; }
}
''')
        report=self.scan()
        reasons=' '.join(g['reason'] for g in report['gaps'])
        for expected in ['aliases','inherited','auto-property','Recursive']:
            self.assertIn(expected,reasons)
        self.assertNotIn('PRIVATE',json.dumps(report))
        self.put('Data.cs','using Amazon.DynamoDBv2.DataModel; [DynamoDBTable("items")] public class Item { public string Name { get; set; } } string s = $"{value}";')
        self.assertEqual(len(self.scan()['observations']),1)
        for body in ['[Broken public string Name { get; set; }', 'public string this[int i] { get; set; }']:
            self.put('Data.cs','using Amazon.DynamoDBv2.DataModel; [DynamoDBTable("items")] public class Item { '+body+' }')
            malformed=self.scan()
            self.assertEqual(malformed['observations'],[])
            self.assertTrue(malformed['gaps'])

    def test_sln_and_mapping_change_identity(self):
        self.fixture()
        self.put('Workspace.sln','Project("{00000000-0000-0000-0000-000000000000}") = "App", "src\\App.csproj", "{00000000-0000-0000-0000-000000000001}"')
        before=self.scan()
        self.assertEqual(before['solutions'][0]['project_references'],['src/App.csproj'])
        self.put('src/Program.cs','class Startup { void Configure() { '+self.files['src/Program.cs']+' } }')
        self.assertEqual({o['boundary'] for o in before['observations']},{o['boundary'] for o in self.scan()['observations']})
        self.put('src/Program.cs',self.files['src/Program.cs'].replace('"Primary"','"Changed"'))
        self.assertNotEqual({o['boundary'] for o in before['observations']},{o['boundary'] for o in self.scan()['observations']})

    def test_sql_server_qualified_schema_and_column_types(self):
        source = 'CREATE TABLE [left].[records] (\n [Id] INT,\n [Value] NVARCHAR(100) NULL\n);\nCREATE TABLE [right].[records] (\n [Id] INT\n);'
        program = "import {parseSqlDdl} from " + json.dumps((SCRIPTS/'collect.mjs').as_uri()) + "; console.log(JSON.stringify(parseSqlDdl(" + json.dumps(source) + ")));"
        result=subprocess.run(['node','--input-type=module','-e',program],capture_output=True,text=True,check=True)
        tables=json.loads(result.stdout)
        self.assertEqual([t['name'] for t in tables],['left.records','right.records'])
        self.assertEqual(tables[0]['fields'][1]['shape'],'nvarchar(100)null')

    def test_dotnet_ai_dependency_discovery(self):
        self.put('App.csproj','<Project><PackageReference Include="Azure.AI.OpenAI" /></Project>')
        self.put('Directory.Packages.props','<Project><PackageVersion Include="Microsoft.SemanticKernel" Version="1.0.0" /></Project>')
        self.put('obj/Generated.cs','using Anthropic;')
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True)
        result=subprocess.run(['node',str(ROOT/'plugins/ai-inventory/scripts/collect.mjs'),f'--repo={self.repo}','--output=json'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        derived=json.loads((self.repo/'.noru/.cache/ai-inventory.derived.json').read_text())
        self.assertIn('azure-openai',{p['key'] for p in derived['providers']})
        self.assertIn('semantic-kernel',{p['key'] for p in derived['frameworks']})
        self.assertNotIn('anthropic',{p['key'] for p in derived['providers']})

    def test_collector_and_canonical_review_preserve_evidence(self):
        self.fixture()
        self.put('src/Entities.cs', self.files['src/Entities.cs'].replace('using Microsoft.EntityFrameworkCore;', ''))
        self.put('src/GlobalUsings.cs', 'global using Microsoft.EntityFrameworkCore;')
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True)
        result=subprocess.run(['node',str(SCRIPTS/'collect.mjs'),f'--repo={self.repo}','--output=json'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        derived=json.loads((self.repo/'.noru/.cache/privacy-datamap.derived.json').read_text())
        self.assertEqual(derived['counts']['datasets'],2)
        import field_review
        document={'observation_digest':'fixture'}
        contract=field_review.build_contract(document,self.repo,compiler_report={'tables':[],'gaps':[]},derived=derived)
        self.assertEqual(len(contract['fields']),4)
        self.assertEqual(contract['fields'][1]['type']['name'],'string?')
        self.assertIn('src/Entities.cs',contract['binding']['source_inventory'])
        self.assertTrue(all(row['status']=='needs_investigation' for row in contract['fields']))
        self.assertIn('src/GlobalUsings.cs', contract['binding']['source_inventory'])
        previous=copy.deepcopy(contract['binding'])
        self.put('src/GlobalUsings.cs', '')
        self.assertNotEqual(previous['source_inventory'], field_review.source_inventory(self.repo))
        self.put('src/GlobalUsings.cs', 'global using Microsoft.EntityFrameworkCore;')
        self.put('src/Entities.cs',self.files['src/Entities.cs'].replace('string? Email','string Email'))
        current=field_review.source_inventory(self.repo)
        self.assertNotEqual(previous['source_inventory'],current)


if __name__=='__main__': unittest.main()
