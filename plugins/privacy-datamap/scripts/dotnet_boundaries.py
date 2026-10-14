"""Offline boundary adapters. Registration evidence never accepts data meaning."""
import copy
import hashlib
import json
import pathlib
import re
from dotnet_syntax import close, split, values
from dotnet_adapters import runtime_conditions

CONTRACTS = json.loads((pathlib.Path(__file__).resolve().parent.parent / 'references/dotnet-boundary-contracts.json').read_text())


def calls(tokens, name):
    for i, token in enumerate(tokens):
        if token.kind == 'code' and token.value == name and i and tokens[i-1].value == '.' and values(tokens[i+1:i+2]) == '(':
            try:
                end = close(tokens, i+1, '(', ')')
                yield i, end, split(tokens[i+2:end])
            except ValueError:
                continue


def literal(tokens):
    return tokens[0].value if len(tokens) == 1 and tokens[0].kind == 'string' else None


def setting(tokens, name):
    found = []
    for i, token in enumerate(tokens):
        if token.kind == 'code' and token.value == name and values(tokens[i+1:i+2]) == '=':
            end = i+2
            while end < len(tokens) and tokens[end].value not in {';', '}'}: end += 1
            found.append(tokens[i+2:end])
    return found[0] if len(found) == 1 else []


def scheme(tokens):
    return literal(tokens) or {'CookieAuthenticationDefaults . AuthenticationScheme':'Cookies',
                               'OpenIdConnectDefaults . AuthenticationScheme':'OpenIdConnect'}.get(values(tokens))


def key(*parts):
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:20]


def emit(reader, path, line, boundary, collection, fields, refs, contract=None):
    evidence = [reader.span({'path':p}, int(n)) for p,n in (ref.rsplit(':',1) for ref in sorted(set(refs)))]
    def field(name, spec):
        row = {'name':name, 'shape':spec['kind'], 'type':{k:v for k,v in spec.items() if k!='members'},
               'evidence':evidence, 'members':[field(n,s) for n,s in spec.get('members', spec.get('element', {}).get('members', {})).items()]}
        if contract: row['framework_contract']={k:v for k,v in contract.items() if k in {'adapter','version','license','sources'}}
        if row['members']: row['type']['members'] = [{'name':f['name']} for f in row['members']]
        return row
    parsed = [field(n,s) for n,s in fields.items()]
    reader.tables.append({'name':collection,'dataset':boundary,'schema_path':path,'fields':parsed})
    reader.observations.append({'path':path,'boundary':boundary,'relationship_bound':True,'binding_refs':refs,
        'kind':'dotnet','role':'canonical','collections':[{'name':collection,'refs':refs,
        'fields':[{'name':f['name'],'shape':f['shape'],'refs':refs} for f in parsed]}]})


def local_method(reader, project, name):
    for source in reader.sources.values():
        if source['project'] != project: continue
        tokens=source['tokens']
        for i,t in enumerate(tokens):
            if t.kind=='code' and t.value==name and (not i or tokens[i-1].value!='.') and values(tokens[i+1:i+2])=='(':
                try:
                    end=close(tokens,i+1,'(',')')
                    if values(tokens[end+1:end+2]) in {'{','=>'}: return True
                except ValueError: continue
    return False


def project_closure(reader, project):
    seen={project}; queue=[project]
    while queue:
        for ref in reader.projects.get(queue.pop(),{}).get('project_references',[]):
            if ref not in seen: seen.add(ref); queue.append(ref)
    return seen


def default_propagation(reader, source, tokens, inject):
    """Resolve a deferred DI helper property after evidenced SDK initialization."""
    receiver=tokens[inject[0][0]-2].value
    expression=values(setting(tokens,receiver))
    match=re.fullmatch(r'(\w+) \. (\w+)',expression)
    if not match:return None,[],[]
    parameter,member=match.groups()
    # Resolve the constructor parameter, not arbitrary identifiers in another file.
    owners=[]
    for model in reader.models:
        if model['path']!=source['path']:continue
        for i,t in enumerate(tokens):
            if t.value!=model['name'] or values(tokens[i+1:i+2])!='(':continue
            try:
                end=close(tokens,i+1,'(',')')
                primary=i>0 and tokens[i-1].value in {'class','record'}
                if values(tokens[end+1:end+2])!='{' and not primary:continue
                for param in split(tokens[i+2:end]):
                    if len(param)==2 and param[-1].value==parameter:
                        owners.append((model['name'],param[0].value))
            except ValueError:continue
    if len(owners)!=1:return None,[],[]
    owner,helper=owners[0]
    candidates=[m for m in reader.models if m['name']==helper and m['project'] in project_closure(reader,source['project'])]
    if len(candidates)!=1:return None,[],[]
    model=candidates[0]
    pattern=f'TextMapPropagator {member} {{ get ; }} = Propagators . DefaultTextMapPropagator ;'
    if pattern not in values(model['body']):return None,[],[]
    refs=[f"{model['path']}:{model['line']}"]; contracts=[]; runtime_refs=[]
    entry_paths={e['ref'].rsplit(':',1)[0] for e in reader.runtime_entrypoints()}
    roots={s['project'] for path,s in reader.sources.items() if path in entry_paths}
    runtimes=[p for p in roots if source['project'] in project_closure(reader,p)]
    if not runtimes:return None,[],[]
    for runtime in sorted(runtimes):
        projects=project_closure(reader,runtime)
        sources=[dict(s,path=p) for p,s in reader.sources.items() if s['project'] in projects]
        packages=[p for project in projects for p in reader.projects[project]['packages'] if p['name'] in {'OpenTelemetry','OpenTelemetry.Api','OpenTelemetry.Extensions.Hosting'}]
        versions={p['version'] for p in packages}
        contract=next((c for c in CONTRACTS['propagation'] if versions=={c['version']}),None)
        if not contract or any(p['conditional'] for p in packages):return None,[],[]
        if not any(p['name']=='OpenTelemetry.Extensions.Hosting' for p in packages):return None,[],[]
        # An override anywhere in the reachable compilation needs its own binding analysis.
        if any(any(t.value=='SetDefaultTextMapPropagator' for t in s['tokens']) or setting(s['tokens'],'DefaultTextMapPropagator') for s in sources):return None,[],[]
        if any(local_method(reader,p,'AddOpenTelemetry') for p in projects):return None,[],[]
        registrations=[]
        for candidate in sources:
            ct=candidate['tokens']
            for i,end,args in calls(ct,'AddOpenTelemetry'):
                invoked=invocation_evidence(reader,runtime,candidate['path'],i)
                if not invoked or runtime_conditions(ct,i):continue
                # A unique, invoked setup method initializes the SDK before registering
                # both deferred singleton constructors. Calls outside that method do not count.
                start=i
                while start>=0 and ct[start].value!='{':start-=1
                try:last=close(ct,start)
                except ValueError:continue
                tail=values(ct[end:last])
                helper_registration=f'AddSingleton < {helper} > ( )'
                owner_registration=re.search(r'AddSingleton < (?:\w+ , )?'+re.escape(owner)+r' > \( \)',tail)
                if helper_registration not in tail or not owner_registration:continue
                if any(t.value in {'if','switch','return'} for t in ct[end:last]):
                    # The final return of the setup method is allowed after both registrations.
                    cut=tail.find('return')
                    if cut>=0 and (tail.find(helper_registration)>cut or owner_registration.start()>cut):continue
                    if any(t.value in {'if','switch'} for t in ct[end:last]):continue
                registrations.extend(invoked+[f"{candidate['path']}:{ct[i].line}"])
        if not registrations:return None,[],[]
        contracts.append(contract); refs.extend(registrations)
        refs.extend(p.get('version_ref',p['ref']) for p in packages)
        # Include the project graph and all reachable source files: new overrides or
        # changed helper invocations must invalidate the binding on reconciliation.
        refs.extend(f'{p}:1' for p in projects)
        refs.extend(f"{s['path']}:1" for s in sources)
        runtime_refs.append(runtime)
    if len({c['version'] for c in contracts})!=1:return None,[],[]
    return contracts[0],sorted(set(refs)),runtime_refs


def invocation_evidence(reader, project, path, index):
    """Follow unique source method names from evidenced entrypoints, with a finite bound."""
    sources={p:s for p,s in reader.sources.items() if s['project'] in project_closure(reader,project)}
    methods=[]
    control={'if','for','foreach','while','switch','catch','using','lock','fixed'}
    for p,source in sources.items():
        tokens=source['tokens']
        for i,t in enumerate(tokens):
            if t.kind!='code' or t.value in control or not re.fullmatch(r'\w+',t.value) or values(tokens[i+1:i+2])!='(' or (i and tokens[i-1].value in {'.','new'}): continue
            try:
                end=close(tokens,i+1,'(',')')
                if values(tokens[end+1:end+2])!='{':continue
                last=close(tokens,end+1)
                methods.append({'path':p,'name':t.value,'declaration':i,'start':end+2,'end':last,'ref':f'{p}:{t.line}'})
            except ValueError:continue
    target=[m for m in methods if m['path']==path and m['start']<=index<m['end']]
    target=max(target,key=lambda m:m['start']) if target else None
    roots={e['ref'].rsplit(':',1)[0] for e in reader.runtime_entrypoints() if reader.sources.get(e['ref'].rsplit(':',1)[0],{}).get('project')==project}
    visited=set();queue=[]
    for p in roots & set(sources):
        tokens=sources[p]['tokens']
        top=[t for i,t in enumerate(tokens) if not any(m['path']==p and m['declaration']<=i<=m['end'] for m in methods)]
        queue.append((p,top,[]))
    if target is None:return [f'{path}:{sources[path]["tokens"][index].line}'] if path in roots else []
    while queue and len(visited)<128:
        p,tokens,refs=queue.pop(0)
        for m in methods:
            if (m['path'],m['start']) in visited:continue
            # Method names are followed only when unambiguous within this compilation.
            if sum(n['name']==m['name'] for n in methods)!=1:continue
            invocations=[i for i,t in enumerate(tokens) if t.value==m['name'] and values(tokens[i+1:i+2])=='(']
            if not any(not runtime_conditions(tokens,i) for i in invocations):continue
            chain=refs+[m['ref']]
            if m==target:return chain
            visited.add((m['path'],m['start']))
            queue.append((m['path'],sources[m['path']]['tokens'][m['start']:m['end']],chain))
    return []


def header_setter(tokens, args):
    if len(args)!=3 or len(args[-1])!=1: return False
    name=args[-1][0].value
    for i,t in enumerate(tokens):
        if t.value!=name or values(tokens[i+1:i+2])!='(': continue
        try:
            end=close(tokens,i+1,'(',')');params=split(tokens[i+2:end])
            if len(params)!=3 or values(tokens[end+1:end+2])!='{': continue
            last=close(tokens,end+1);body=values(tokens[end+2:last])
            carrier,key_,value=[p[-1].value for p in params]
            assignment=f'{carrier} . Headers [ {key_} ] = {value} ;'
            # Only an untransformed assignment establishes the injected header keys.
            initialization=f'{carrier} . Headers ? ? = new Dictionary < string , object > ( ) ;'
            if body in {assignment, initialization+' '+assignment}: return True
        except (ValueError,IndexError): continue
    return False


def header_getter(tokens, args):
    if len(args)!=3 or len(args[-1])!=1:return False
    name=args[-1][0].value
    for i,t in enumerate(tokens):
        if t.value!=name or values(tokens[i+1:i+2])!='(':continue
        try:
            end=close(tokens,i+1,'(',')'); params=split(tokens[i+2:end])
            if len(params)!=2 or values(tokens[end+1:end+2])!='{':continue
            last=close(tokens,end+1); body=values(tokens[end+2:last])
            carrier,key_=[p[-1].value for p in params]
            pattern=(r'if \( '+re.escape(carrier)+r' \. Headers \. TryGetValue \( '+re.escape(key_)+
                     r' , out var (\w+) \) \) \{ var (\w+) = \1 as byte \[ \] ; '
                     r'return \[ Encoding \. UTF8 \. GetString \( \2 \) \] ; \} return \[ \] ;')
            if re.fullmatch(pattern,body):return True
        except (ValueError,IndexError):continue
    return False


def authentication(reader):
    output = []
    for path, source in reader.sources.items():
        tokens = source['tokens']; project = source['project']
        registrations = list(calls(tokens, 'AddCookie'))
        if not registrations: continue
        if local_method(reader,project,'AddCookie') or local_method(reader,project,'AddOpenIdConnect'):
            reader.gap(path,tokens[registrations[0][0]].line,'Source-defined authentication methods shadow framework registration; resolve the binding.',code='DOTNET_AUTH_BINDING')
            continue
        packages = reader.projects.get(project, {}).get('packages', [])
        versions = {p['version'] for p in packages if p['name']=='Microsoft.AspNetCore.Authentication.OpenIdConnect' and not p['conditional']}
        contract = next((c for c in CONTRACTS['authentication'] if versions == {c['version']}), None)
        refs = [p.get('version_ref',p['ref']) for p in packages if p['name']=='Microsoft.AspNetCore.Authentication.OpenIdConnect']
        for i,end,args in registrations:
            line = tokens[i].line
            name = scheme(args[0]) if args and not any(t.value == '=>' for t in args[0]) else 'Cookies'
            record = {'adapter':'aspnet-authentication@1','project':project,'ref':f'{path}:{line}',
                      'scheme':name,'storage':'unresolved','oidc':[],'classification':'requires_investigation'}
            output.append(record)
            duplicate=sum(1 for _,_,a in registrations if (scheme(a[0]) if a and not any(t.value=='=>' for t in a[0]) else 'Cookies')==name)>1
            if duplicate or runtime_conditions(tokens,i):
                reader.gap(path,line,'Duplicate or conditional authentication registration needs branch-specific scheme binding.',code='DOTNET_AUTH_BINDING')
                continue
            if not name or not contract:
                reader.gap(path,line,'Authentication scheme or exact framework serialization contract is unavailable.',code='DOTNET_AUTH_CONTRACT')
                continue
            invoked=invocation_evidence(reader,project,path,i)
            if not invoked:
                reader.gap(path,line,'Authentication registration activation is not established from a runtime entrypoint; trace the invoked helper or conditional registration.',code='DOTNET_AUTH_BINDING')
                continue
            config = tokens[i+2:end]
            all_project_tokens = [t for s in reader.sources.values() if s['project']==project for t in s['tokens']]
            custom = sorted({t.value for index,t in enumerate(all_project_tokens) if t.kind=='code' and (t.value in {'IClaimsTransformation','IConfigureOptions','IPostConfigureOptions'} or values(all_project_tokens[index+1:index+2]) in {'=', '('} or (t.value=='Events' and values(all_project_tokens[index+1:index+2])=='.'))} & {
                'SessionStore','TicketDataFormat','TicketSerializer','CookieManager','IClaimsTransformation','Events','EventsType',
                'PostConfigure','ConfigureOptions','IConfigureOptions','IPostConfigureOptions'})
            if setting(config,'Name') and literal(setting(config,'Name')) is None:
                reader.gap(path,line,'Cookie name is dynamic; establish the configured storage boundary.',code='DOTNET_AUTH_BINDING')
                continue
            record.update(contract=contract, cookie_name=literal(setting(config,'Name')) or '.AspNetCore.'+name,
                          storage='server_ticket_store' if 'SessionStore' in custom else 'custom_or_unresolved' if custom else 'browser_cookie',
                          customizations=custom)
            if custom:
                reader.gap(path,line,'Authentication customization requires tracing storage, serializers, events and transformed claims before modeling ticket contents.',code='DOTNET_AUTH_CUSTOMIZATION',details={'customizations':custom})
                continue
            for j,last,oidc_args in calls(tokens,'AddOpenIdConnect'):
                body = tokens[j+2:last]
                target = scheme(setting(body,'SignInScheme')) or scheme(setting(tokens,'DefaultSignInScheme')) or scheme(setting(tokens,'DefaultScheme'))
                if target != name: continue
                record['oidc'].append({'ref':f'{path}:{tokens[j].line}',
                    'scheme':scheme(oidc_args[0]) if oidc_args and literal(oidc_args[0]) else 'OpenIdConnect',
                    'save_tokens':values(setting(body,'SaveTokens')) == 'true',
                    'userinfo':values(setting(body,'GetClaimsFromUserInfoEndpoint')) == 'true',
                    'requested_scopes':[literal(a[0]) for ai,_,a in calls(body,'Add') if ai>=2 and body[ai-2].value=='Scope' and a and literal(a[0])],
                    'claim_actions':[t.value for t in body if t.value in {'MapUniqueJsonKey','MapJsonKey','DeleteClaim','MapAll'}]})
            signins=[f'{path}:{tokens[j].line}' for j,_,a in calls(tokens,'SignInAsync') if a and scheme(a[0])==name]
            if not record['oidc'] and not signins:
                reader.gap(path,line,'Cookie registration has no evidenced sign-in producer bound to this scheme; do not infer ticket contents from registration alone.',code='DOTNET_AUTH_BINDING')
                continue
            boundary = 'dotnet_ticket_'+key(project,name)
            record.update(dataset=boundary, refs=sorted(set([record['ref']]+refs+invoked+signins+[r['ref'] for r in record['oidc']])))
            fields=copy.deepcopy(contract['fields'])
            if any(r['save_tokens'] for r in record['oidc']):
                fields['properties']['members']={name:{'kind':'string','optional':True} for name in contract['saved_token_keys']}
            emit(reader,path,line,boundary,'authentication_ticket',fields,record['refs'],contract)
            reader.relationships.append({'kind':'authentication_ticket','dataset':boundary,'collection':'authentication_ticket',
                'model':'AuthenticationTicket','schema_path':path,'runtime_project':project,'storage':record['storage'],'refs':record['refs'],
                'physical_identity':'unverified'})
            # An issuer's claims are not automatically the client's persisted claims.
            record['content_review']={'state':'required','reason':'Trace issuer scopes, claim actions and token-property writers; serializer structure does not establish privacy meaning.'}
    return output


def messaging(reader):
    output=[]
    for path,source in reader.sources.items():
        tokens=source['tokens']; names={t.value for t in tokens if t.kind=='code'}
        packages=reader.projects.get(source['project'],{}).get('packages',[])
        if not any(p['name'] in {'RabbitMQ.Client','Aspire.RabbitMQ.Client'} for p in packages): continue
        if not names & {'BasicPublish','BasicPublishAsync'}: continue
        if 'Headers' not in names: continue
        inject=list(calls(tokens,'Inject')); extract=list(calls(tokens,'Extract'))
        if not inject and not extract: continue
        line=tokens[inject[0][0] if inject else extract[0][0]].line
        boundary='dotnet_envelope_'+key(source['project'],path)
        refs=[f'{path}:{line}']
        versions={p['version'] for p in packages if p['name'] in {'OpenTelemetry.Api','OpenTelemetry'} and not p['conditional']}
        contract=next((c for c in CONTRACTS.get('propagation',[]) if versions=={c['version']}),None)
        propagators=[]
        if contract and inject and header_setter(tokens,inject[0][2]):
            receiver=tokens[inject[0][0]-2].value
            expression=setting(tokens,receiver)
            declared=re.findall(r'new (TraceContextPropagator|BaggagePropagator) \( \)',values(expression))
            residual=re.sub(r'new (TraceContextPropagator|BaggagePropagator) \( \)', '', values(expression))
            if declared and not re.search(r'\bnew (?!CompositeTextMapPropagator)',residual):
                propagators=declared
        runtime_projects=[]
        if not propagators and inject and header_setter(tokens,inject[0][2]):
            resolved,binding_refs,runtime_projects=default_propagation(reader,dict(source,path=path),tokens,inject)
            if resolved:
                contract=resolved; propagators=contract['sdk_default_propagators']; refs.extend(binding_refs)
        fields={'headers':{'kind':'record','open':True,'key_type':'string','value_type':'object'}}
        if propagators:
            fields['headers']['members']={name:({'kind':'record','open':True,'key_type':'string','value_type':'string','name':'W3C encoded baggage'} if name=='baggage' else {'kind':'string','optional':True})
                for propagator in propagators for name in contract['propagators'][propagator]}
            refs += [p.get('version_ref',p['ref']) for p in packages if p['name'] in {'OpenTelemetry.Api','OpenTelemetry'}]
        properties={}
        for bi,t in enumerate(tokens):
            if t.value!='BasicProperties' or not bi or tokens[bi-1].value!='new':continue
            try:
                be=close(tokens,bi+1,'(',')')
                if values(tokens[be+1:be+2])!='{':continue
                last=close(tokens,be+1)
                for assignment in split(tokens[be+2:last]):
                    if len(assignment)>=3 and assignment[0].kind=='code' and assignment[1].value=='=':
                        properties[assignment[0].value]={'kind':'string' if assignment[2].kind=='string' else 'unknown'}
                refs.append(f'{path}:{t.line}')
            except ValueError:continue
        if properties:fields['broker_properties']={'kind':'object','members':properties}
        emit(reader,path,line,boundary,'message_envelope',fields,refs,contract if propagators else None)
        row={'adapter':'rabbitmq-propagation@1','dataset':boundary,'project':source['project'],'refs':refs,
             'body':'separate_application_payload','broker_properties':sorted(names & {'DeliveryMode','Persistent','ContentType','MessageId','CorrelationId'}),
             'headers':{'additional_entries':True,'value_type':'object','propagator':propagators or 'unresolved'},
             'runtime_projects':runtime_projects, 'inject':bool(inject),'extract':bool(extract),'baggage': 'Current' in names and 'Baggage' in names}
        output.append(row)
        if extract and (not inject or any(tokens[j-2].value!=tokens[inject[0][0]-2].value or not header_getter(tokens,a) for j,_,a in extract)):
            reader.gap(path,line,'RabbitMQ header consumer uses an unresolved propagator or decoding transformation; investigate incoming header representation.',code='DOTNET_PROPAGATOR_CONSUMER',details=row)
        if not propagators: reader.gap(path,line,'RabbitMQ propagation envelope is open; resolve the configured propagator and setter/getter serialization contract before assigning fixed header members.',code='DOTNET_PROPAGATOR_BINDING',details=row)
    return output


def discover(reader):
    return {'authentication_tickets':authentication(reader),'message_envelopes':messaging(reader), 'provisioned_runtimes':proxies(reader)}


def proxies(reader):
    output = []
    for path, source in reader.sources.items():
        project = source['project']; tokens = source['tokens']
        if not any(p['name']=='Aspire.Hosting.Yarp' and not p['conditional'] for p in reader.projects.get(project, {}).get('packages', [])):
            continue
        if local_method(reader,project,'AddYarp'):
            reader.gap(path,1,'Source-defined AddYarp shadows framework provisioning; resolve its runtime semantics.',code='DOTNET_PROXY_BINDING')
            continue
        resources = {}
        for i,end,args in calls(tokens, 'AddDotnetProject'):
            if i < 4 or tokens[i-3].value != '=' or len(args)<2: continue
            destination = literal(args[1])
            if destination:
                full = (reader.repo / pathlib.PurePosixPath(path).parent / destination).resolve()
                projects = [p for p in reader.projects if (reader.repo/p).parent == full or reader.repo/p == full]
                if len(projects)==1:
                    resources[tokens[i-4].value] = {'root':str(pathlib.PurePosixPath(projects[0]).parent),
                        'resource':literal(args[0]), 'ref':f'{path}:{tokens[i].line}'}
        for i,end,args in calls(tokens,'AddYarp'):
            line=tokens[i].line; name=literal(args[0]) if args else None
            if not name:
                reader.gap(path,line,'Proxy resource name is dynamic; establish the provisioned runtime identity.',code='DOTNET_PROXY_BINDING')
                continue
            last=end+1
            while last<len(tokens) and tokens[last].value!=';': last+=1
            chain=tokens[end+1:last]
            runtime={'identity':'dotnet_proxy_'+key(project,name), 'name':name,'provisioner_root':str(pathlib.PurePosixPath(project).parent),
                     'adapter':'aspire-yarp@1','refs':[f'{path}:{line}'],'routes':[], 'storage':'no_persistence_evidence',
                     'external_http_endpoints':any(t.value=='WithExternalHttpEndpoints' for t in chain)}
            output.append(runtime)
            bodies=[(path,chain,resources)]
            known={'WithConfiguration','WithExternalHttpEndpoints','WithReference','WaitFor'}
            depth=0
            for ci,t in enumerate(chain):
                if t.kind=='code' and t.value=='(': depth+=1
                if t.kind=='code' and t.value==')': depth-=1
                if depth or ci==0 or t.kind!='code' or chain[ci-1].value!='.' or values(chain[ci+1:ci+2])!='(' or t.value in known: continue
                # Only a unique explicitly called Yarp-resource extension is followed.
                matches=[]
                for helper_path,helper in reader.sources.items():
                    if helper['project']!=project: continue
                    ht=helper['tokens']
                    for hi,h in enumerate(ht):
                        if h.value!=t.value or values(ht[hi+1:hi+2])!='(': continue
                        try:
                            he=close(ht,hi+1,'(',')'); params=split(ht[hi+2:he])
                            if not params or 'this IResourceBuilder < YarpResource >' not in values(params[0]) or values(ht[he+1:he+2])!='{': continue
                            hb=close(ht,he+1)
                            matches.append((helper_path,ht[he+2:hb],params))
                        except ValueError: continue
                if len(matches)!=1:
                    reader.gap(path,t.line,'Proxy helper has no unique supported source definition; trace its routes.',code='DOTNET_PROXY_HELPER')
                    continue
                helper_path,body,params=matches[0]
                ce=close(chain,ci+1,'(',')'); arguments=split(chain[ci+2:ce])
                env={p[-1].value:resources[a[0].value] for p,a in zip(params[1:],arguments) if len(a)==1 and a[0].value in resources}
                bodies.append((helper_path,body,env));runtime['refs'].append(f'{helper_path}:{body[0].line}')
            for body_path,body,env in bodies:
                env=dict(env)
                for ci,ce,ca in calls(body,'AddCluster'):
                    if ci>=4 and body[ci-3].value=='=' and ca and len(ca[0])==1 and ca[0][0].value in env:
                        env[body[ci-4].value]=env[ca[0][0].value]
                for ri,re_,ra in calls(body,'AddRoute'):
                    route={'ref':f'{body_path}:{body[ri].line}','match':literal(ra[0]) if ra else None,'destination':None,'transforms':[]}
                    if len(ra)>=2 and ra[1]:
                        dest=ra[1]; expression=values(dest)
                        if len(dest)==1 or re.fullmatch(r'\w+ \. GetEndpoint \( <literal> \)',expression):
                            route['destination']=env.get(dest[0].value)
                    stop=re_+1
                    while stop<len(body) and body[stop].value!=';':stop+=1
                    for ti,te,ta in calls(body[re_+1:stop],'WithTransformPathRemovePrefix'):
                        route['transforms'].append({'kind':'remove_path_prefix','value':literal(ta[0]) if ta else None})
                    unknown_transforms=sorted({t.value for t in body[re_+1:stop] if t.kind=='code' and t.value.startswith('WithTransform') and t.value!='WithTransformPathRemovePrefix'})
                    if unknown_transforms:
                        route['unsupported_transforms']=unknown_transforms
                        reader.gap(body_path,body[ri].line,'Proxy route uses unsupported transformations; inspect how forwarding contents change.',code='DOTNET_PROXY_TRANSFORM',details=route)
                    runtime['routes'].append(route)
                    if not route['match'] or not route['destination'] or any(t['value'] is None for t in route['transforms']):
                        reader.gap(body_path,body[ri].line,'Proxy route match, destination or transform is unresolved; preserve the forwarding boundary and investigate configuration.',code='DOTNET_PROXY_ROUTE_BINDING',details=route)
            if not runtime['routes']:
                reader.gap(path,line,'Provisioned proxy has no statically resolved routes; inspect dynamic or external configuration.',code='DOTNET_PROXY_ROUTE_BINDING')
    return output
