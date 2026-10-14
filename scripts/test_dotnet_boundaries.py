#!/usr/bin/env python3
"""Offline authentication, propagation and provisioned runtime regression fixtures."""
import copy
import json
import subprocess
import sys
import unittest
import test_dotnet as fixtures
sys.path.insert(0,str(fixtures.SCRIPTS))
import gap_resolution
import review

class BoundaryTests(unittest.TestCase):
    setUp=fixtures.DotnetTests.setUp
    put=fixtures.DotnetTests.put
    scan=fixtures.DotnetTests.scan

    def auth(self, options='', version='10.0.12'):
        self.put('App.csproj','<Project><ItemGroup><PackageReference Include="Microsoft.AspNetCore.Authentication.OpenIdConnect" Version="'+version+'" /></ItemGroup></Project>')
        self.put('Program.cs','using Microsoft.AspNetCore.Authentication.Cookies; using Microsoft.AspNetCore.Authentication.OpenIdConnect; var builder = WebApplication.CreateBuilder(args); builder.Services.AddAuthentication().AddCookie("session", o => { '+options+' }).AddOpenIdConnect("login", o => { o.SignInScheme="session"; o.SaveTokens=true; o.Scope.Add("profile"); }); builder.Build().Run();')

    def test_ticket_structure_and_scheme_binding(self):
        self.auth('o.Cookie.Name="custom";')
        report=self.scan(); ticket=report['authentication_tickets'][0]
        self.assertEqual(ticket['storage'],'browser_cookie')
        self.assertEqual(ticket['cookie_name'],'custom')
        self.assertTrue(ticket['oidc'][0]['save_tokens'])
        fields={f['name']:f for f in report['tables'][0]['fields']}
        self.assertTrue(fields['properties']['type']['open'])
        self.assertTrue(fields['identities']['members'])
        self.assertNotIn('categories',fields['properties'])
        self.assertEqual(report,self.scan())
        self.assertEqual(ticket['content_review']['state'],'required')
        self.assertFalse(any(g['code']=='DOTNET_AUTH_CONTENT' for g in report['gaps']))

    def test_custom_storage_does_not_invent_cookie_payload(self):
        for option in ['o.SessionStore = custom;', 'o.TicketDataFormat = custom;', 'o.Events = custom;']:
            self.auth(option)
            report=self.scan()
            self.assertFalse(report['tables'])
            self.assertTrue(any(g['code']=='DOTNET_AUTH_CUSTOMIZATION' for g in report['gaps']))

    def test_misleading_method_names_do_not_create_storage(self):
        self.auth()
        self.put('Custom.cs','static class Custom { public static void AddCookie(string scheme) {} }')
        self.assertEqual(self.scan()['tables'],[])
        self.assertTrue(any(g['code']=='DOTNET_AUTH_BINDING' for g in self.scan()['gaps']))

    def test_unused_authentication_helper_requires_activation_evidence(self):
        self.auth()
        self.put('Unused.cs','static class Unused { static void Configure(IServiceCollection services) { services.AddAuthentication().AddCookie("unused").AddOpenIdConnect(o => { o.SignInScheme="unused"; }); } }')
        report=self.scan()
        self.assertEqual(len(report['tables']),1)
        self.assertTrue(any('activation' in g['reason'] for g in report['gaps']))

    def test_unknown_version_and_dynamic_scheme_block(self):
        self.auth(version='99.0.0')
        self.assertFalse(self.scan()['tables'])
        self.auth()
        self.put('Program.cs',self.files['Program.cs'].replace('AddCookie("session"','AddCookie(configuration["scheme"]'))
        self.assertFalse(self.scan()['tables'])

    def test_multiple_cookie_schemes_are_separate(self):
        self.auth()
        self.put('Program.cs',self.files['Program.cs'].replace('.AddOpenIdConnect(','.AddCookie("other").AddOpenIdConnect('))
        report=self.scan()
        self.assertEqual(len(report['tables']),1)
        self.assertTrue(any(g['code']=='DOTNET_AUTH_BINDING' for g in report['gaps']))
        self.assertEqual([len(t['oidc']) for t in report['authentication_tickets']],[1,0])

    def test_unknown_propagator_preserves_open_header_boundary(self):
        self.put('App.csproj','<Project><ItemGroup><PackageReference Include="RabbitMQ.Client" Version="7.0.0" /></ItemGroup></Project>')
        self.put('Bus.cs','using RabbitMQ.Client; class Bus { void Send() { props.Headers[key] = value; propagator.Inject(context, props, setter); channel.BasicPublishAsync(props); } }')
        report=self.scan()
        self.assertTrue(report['message_envelopes'][0]['headers']['additional_entries'])
        self.assertEqual(report['tables'][0]['fields'][0]['type']['kind'],'record')
        self.assertTrue(any(g['code']=='DOTNET_PROPAGATOR_BINDING' for g in report['gaps']))

    def test_pinned_explicit_propagator_keeps_fixed_and_open_members(self):
        self.put('App.csproj','<Project><ItemGroup><PackageReference Include="RabbitMQ.Client" Version="7.0.0" /><PackageReference Include="OpenTelemetry.Api" Version="1.19.1" /></ItemGroup></Project>')
        self.put('Bus.cs','using RabbitMQ.Client; using OpenTelemetry.Context.Propagation; class Bus { void Send() { var propagator = new CompositeTextMapPropagator([new TraceContextPropagator(), new BaggagePropagator()]); props.Headers[key] = value; propagator.Inject(context, props, setter); channel.BasicPublishAsync(props); } static void setter(IBasicProperties props, string key, string value) { props.Headers[key] = value; } }')
        report=self.scan(); header=report['tables'][0]['fields'][0]
        self.assertEqual({f['name'] for f in header['members']},{'traceparent','tracestate','baggage'})
        self.assertTrue(header['type']['open'])
        self.assertFalse(any(g['code']=='DOTNET_PROPAGATOR_BINDING' for g in report['gaps']))
        self.put('Bus.cs',self.files['Bus.cs'].replace('new TraceContextPropagator()','new CustomPropagator()'))
        self.assertTrue(any(g['code']=='DOTNET_PROPAGATOR_BINDING' for g in self.scan()['gaps']))

    def proxy(self, helper=False):
        self.put('host/Host.csproj','<Project><ItemGroup><PackageReference Include="Aspire.Hosting.Yarp" Version="13.6.0" /></ItemGroup></Project>')
        self.put('api/Api.csproj','<Project />')
        self.put('api/Program.cs','var builder = WebApplication.CreateBuilder(args); builder.Build().Run();')
        setup='WithConfiguration(c => { c.AddRoute("/service/{**path}", api.GetEndpoint("http")).WithTransformPathRemovePrefix("/service"); })'
        if helper:
            self.put('host/Routes.cs','static class Routes { public static IResourceBuilder<YarpResource> ConfigureRoutes(this IResourceBuilder<YarpResource> proxy, IResourceBuilder<DotnetProjectResource> dest) { return proxy.'+setup.replace('api.GetEndpoint','dest.GetEndpoint')+'; } }')
            setup='ConfigureRoutes(api)'
        self.put('host/Program.cs','var builder = DistributedApplication.CreateBuilder(args); var api = builder.AddDotnetProject("service", "../api"); builder.AddYarp("gateway").WithExternalHttpEndpoints().'+setup+'; builder.Build().Run();')

    def default_propagator(self):
        self.put('app/App.csproj','<Project><ItemGroup><ProjectReference Include="../bus/Bus.csproj" /><PackageReference Include="OpenTelemetry.Extensions.Hosting" Version="1.19.1" /></ItemGroup></Project>')
        self.put('app/Program.cs','var builder = WebApplication.CreateBuilder(args); builder.WireMessaging(); builder.Build().Run();')
        self.put('bus/Bus.csproj','<Project><ItemGroup><PackageReference Include="RabbitMQ.Client" Version="7.0.0" /></ItemGroup></Project>')
        self.put('bus/Telemetry.cs','class TraceHelper { public TextMapPropagator Carrier { get; } = Propagators.DefaultTextMapPropagator; }')
        self.put('bus/Setup.cs','static class Setup { public static void WireMessaging(this IHostApplicationBuilder builder) { builder.Services.AddOpenTelemetry(); builder.Services.AddSingleton<TraceHelper>(); builder.Services.AddSingleton<IBus, Publisher>(); } }')
        self.put('bus/Publisher.cs','class Publisher { TextMapPropagator propagation; public Publisher(TraceHelper helper) { propagation = helper.Carrier; } void Send() { propagation.Inject(context, props, setter); channel.BasicPublishAsync(props); } static void setter(IBasicProperties props, string key, string value) { props.Headers ??= new Dictionary<string, object>(); props.Headers[key] = value; } }')

    def test_injected_default_propagator_traces_sdk_initialization(self):
        self.default_propagator()
        report=self.scan()
        self.assertFalse(any(g['code']=='DOTNET_PROPAGATOR_BINDING' for g in report['gaps']))
        header=report['tables'][0]['fields'][0]
        self.assertEqual({f['name'] for f in header['members']},{'traceparent','tracestate','baggage'})
        self.assertTrue(header['type']['open'])
        self.assertEqual(report['message_envelopes'][0]['runtime_projects'],['app/App.csproj'])
        self.assertEqual(report,self.scan())
        script="import {collectFacts} from './plugins/privacy-datamap/scripts/collect.mjs'; console.log(JSON.stringify(collectFacts(process.argv[1])));"
        result=subprocess.run(['node','--input-type=module','-e',script,str(self.repo)],cwd=fixtures.ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        dataset=next(d for d in json.loads(result.stdout)['datasets'] if d['fides_key']==report['message_envelopes'][0]['dataset'])
        self.assertEqual(dataset['system_references'],['app'])
        self.put('bus/Publisher.cs',self.files['bus/Publisher.cs'].replace('class Publisher { TextMapPropagator propagation; public Publisher(TraceHelper helper) { propagation = helper.Carrier; }','class Publisher(TraceHelper helper) : IBus { TextMapPropagator propagation = helper.Carrier;'))
        self.assertFalse(any(g['code']=='DOTNET_PROPAGATOR_BINDING' for g in self.scan()['gaps']))
        self.put('bus/Override.cs','static class Override { static void Configure() { Sdk.SetDefaultTextMapPropagator(custom); } }')
        self.assertTrue(any(g['code']=='DOTNET_PROPAGATOR_BINDING' for g in self.scan()['gaps']))

    def test_default_propagator_requires_unambiguous_activation_and_version(self):
        for path,old,new in [('app/Program.cs','builder.WireMessaging();',''),
                             ('app/Program.cs','builder.WireMessaging();','if (enabled) { builder.WireMessaging(); }'),
                             ('app/App.csproj','1.19.1','99.0.0'),
                             ('bus/Setup.cs','builder.Services.AddOpenTelemetry();',''),
                             ('bus/Setup.cs','builder.Services.AddOpenTelemetry();','if (enabled) { builder.Services.AddOpenTelemetry(); }'),
                             ('bus/Setup.cs','builder.Services.AddOpenTelemetry();','Sdk.SetDefaultTextMapPropagator(custom); builder.Services.AddOpenTelemetry();'),
                             ('bus/Publisher.cs','props.Headers[key] = value;','props.Headers[key] = Transform(value);')]:
            with self.subTest(path=path,new=new):
                self.default_propagator(); self.put(path,self.files[path].replace(old,new))
                self.assertTrue(any(g['code']=='DOTNET_PROPAGATOR_BINDING' for g in self.scan()['gaps']))
        self.default_propagator()
        self.put('bus/Telemetry.cs','class TraceHelper { public TextMapPropagator Carrier { get; } = custom; } class Unused { public TextMapPropagator Carrier { get; } = Propagators.DefaultTextMapPropagator; }')
        self.assertTrue(any(g['code']=='DOTNET_PROPAGATOR_BINDING' for g in self.scan()['gaps']))

    def test_consumer_decoding_is_evidenced_and_custom_transform_blocks(self):
        self.default_propagator()
        consumer=' void Receive() { propagation.Extract(context, props, getter); } static IEnumerable<string> getter(IReadOnlyBasicProperties props, string key) { if (props.Headers.TryGetValue(key, out var value)) { var bytes = value as byte[]; return [Encoding.UTF8.GetString(bytes)]; } return []; } '
        self.put('bus/Publisher.cs',self.files['bus/Publisher.cs'][:-1]+consumer+'}')
        self.assertFalse(any(g['code'].startswith('DOTNET_PROPAGATOR') for g in self.scan()['gaps']))
        self.put('bus/Publisher.cs',self.files['bus/Publisher.cs'].replace('Encoding.UTF8.GetString(bytes)','Decrypt(bytes)'))
        self.assertTrue(any(g['code']=='DOTNET_PROPAGATOR_CONSUMER' for g in self.scan()['gaps']))

    def test_direct_and_helper_routes_separate_proxy_from_provisioner(self):
        for helper in [False,True]:
            self.proxy(helper)
            report=self.scan(); proxy=report['provisioned_runtimes'][0]
            self.assertEqual(proxy['provisioner_root'],'host')
            self.assertEqual(proxy['routes'][0]['destination']['root'],'api')
            self.assertEqual(proxy['routes'][0]['transforms'][0]['value'],'/service')
            self.assertEqual(report['tables'],[])
            self.assertFalse(any(g['code'].startswith('DOTNET_PROXY') for g in report['gaps']))
            self.assertEqual(report,self.scan())

    def test_dynamic_proxy_destination_stays_blocking(self):
        self.proxy()
        self.put('host/Program.cs',self.files['host/Program.cs'].replace('api.GetEndpoint("http")','ResolveDestination()'))
        report=self.scan()
        self.assertIsNone(report['provisioned_runtimes'][0]['routes'][0]['destination'])
        self.assertTrue(any(g['code']=='DOTNET_PROXY_ROUTE_BINDING' for g in report['gaps']))

    def test_boundaries_follow_renamed_projects_helpers_and_schemes(self):
        self.auth()
        self.proxy(True)
        original=self.scan()
        replacements={'host':'deployment', 'Host.csproj':'Launcher.csproj', 'api':'worker',
                      'Api.csproj':'Worker.csproj', 'ConfigureRoutes':'WireEndpoints',
                      'gateway':'public-edge', 'session':'member-session',
                      'login':'identity-provider'}
        sources=list(self.files.items())
        self.files={}
        for path, source in sources:
            (self.repo/path).unlink()
            for old, new in replacements.items():
                path=path.replace(old,new)
                source=source.replace(old,new)
            self.put(path,source)
        renamed=self.scan()
        self.assertEqual(len(renamed['tables']),len(original['tables']))
        self.assertEqual(renamed['gaps'],original['gaps'])
        ticket=renamed['authentication_tickets'][0]
        self.assertEqual(ticket['scheme'],'member-session')
        def shape(fields):
            return [(f['name'],f['type']['kind'],shape(f.get('members',[]))) for f in fields]
        self.assertEqual(shape(renamed['tables'][0]['fields']),shape(original['tables'][0]['fields']))
        proxy=renamed['provisioned_runtimes'][0]
        self.assertEqual(proxy['name'],'public-edge')
        self.assertEqual(proxy['provisioner_root'],'deployment')
        self.assertEqual(proxy['routes'][0]['destination']['root'],'worker')
        self.assertEqual(proxy['routes'][0]['transforms'][0]['value'],'/service')

    def test_proxy_collection_uses_source_identity_and_survives_rescan(self):
        self.proxy(True)
        script="import {collectFacts} from './plugins/privacy-datamap/scripts/collect.mjs'; console.log(JSON.stringify(collectFacts(process.argv[1])));"
        result=subprocess.run(['node','--input-type=module','-e',script,str(self.repo)],cwd=fixtures.ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)
        proxy=next(s for s in report['systems'] if s.get('meta',{}).get('noru',{}).get('origin')=='provisioned')
        self.assertEqual(proxy['dataset_references'],[])
        self.assertEqual(proxy['egress'],[{'type':'system','fides_key':'api'}])
        self.assertEqual(proxy['meta']['noru']['provisioner'],'host')
        before=self.scan()['provisioned_runtimes'][0]
        self.put('host/Program.cs',self.files['host/Program.cs'].replace('"gateway"','"renamed-edge"'))
        after=self.scan()['provisioned_runtimes'][0]
        self.assertNotEqual(before['identity'],after['identity'])
        self.assertEqual(after['name'],'renamed-edge')
        self.assertEqual(before['routes'],after['routes'])

    def test_route_and_serializer_edits_change_evidence(self):
        self.auth();before=self.scan()['tables'][0]['fields'][0]['evidence']
        self.put('Program.cs',self.files['Program.cs'].replace('o.SaveTokens=true','o.SaveTokens=false'))
        self.assertNotEqual(before,self.scan()['tables'][0]['fields'][0]['evidence'])
        self.proxy(True);before=self.scan()['provisioned_runtimes'][0]['routes']
        self.put('host/Routes.cs',self.files['host/Routes.cs'].replace('"/service"','"/public"'))
        self.assertNotEqual(before,self.scan()['provisioned_runtimes'][0]['routes'])

    def test_supplemental_provisioned_runtime_is_not_external(self):
        self.put('host/App.csproj','<Project />')
        self.put('host/Program.cs','var builder = WebApplication.CreateBuilder(args); ProvisionEdge(); builder.Build().Run();')
        self.put('.noru/privacy-datamap.yml',"system:\n  - fides_key: edge\n    name: Edge\n    dataset_references: []\n    meta:\n      noru:\n        origin: provisioned\n        provisioner: host\n        refs: [host/Program.cs:1]\n")
        script="import {collectFacts} from './plugins/privacy-datamap/scripts/collect.mjs'; console.log(JSON.stringify(collectFacts(process.argv[1])));"
        result=subprocess.run(['node','--input-type=module','-e',script,str(self.repo)],cwd=fixtures.ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        edge=next(s for s in json.loads(result.stdout)['systems'] if s['fides_key']=='edge')
        self.assertEqual(edge['meta']['noru']['origin'],'provisioned')
        self.assertEqual(edge['dataset_references'],[])
        self.put('.noru/privacy-datamap.yml',self.files['.noru/privacy-datamap.yml'].replace('provisioner: host','provisioner: missing'))
        result=subprocess.run(['node','--input-type=module','-e',script,str(self.repo)],cwd=fixtures.ROOT,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('evidenced provisioner',result.stderr)

    def test_enrichment_gap_is_agent_owned_even_without_collector_gaps(self):
        doc={'store_investigation':{'findings':[{'store':'ticket','status':'gap','refs':['Program.cs:1'],
             'unresolved_question':'Resolve the serializer contract','resolution_needed':'Inspect source'}]}}
        derived={'coverage':{}}
        partition=gap_resolution.calculate(doc,derived)
        self.assertEqual(len(partition['active_blockers']),1)
        progress=review.scan_progress(doc,{},derived,[],[])
        self.assertEqual(progress['agent_investigation'],'incomplete')
        self.assertEqual(progress['extraction_gaps'],1)
        self.assertEqual(progress['missing_facts'],[])
        self.assertEqual(progress['publication'],'blocked')
        self.assertFalse(any(a.startswith('User:') for a in progress['next_actions']))
        self.assertEqual(doc['store_investigation']['findings'][0]['status'],'gap')

if __name__=='__main__': unittest.main()
