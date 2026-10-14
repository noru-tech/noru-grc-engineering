"""Offline framework contracts and declarative registration adapters."""
import json
import pathlib
import re
from dotnet_syntax import values, close

REGISTRATIONS = {
    'AddDbContext': {'adapter_id':'ef-context-options@1','connection':'options'},
    'AddDbContextPool': {'adapter_id':'ef-context-options@1','connection':'options'},
    'AddNpgsqlDbContext': {'adapter_id':'aspire-npgsql-context@1','connection':'literal','provider':'postgresql'},
}
CONTRACTS = json.loads((pathlib.Path(__file__).resolve().parent.parent/'references/dotnet-framework-contracts.json').read_text())['contracts']
VALIDATION_ATTRIBUTES = {'Required','MaxLength','MinLength','StringLength','Range','RegularExpression','EmailAddress','Phone','Url','Compare','CreditCard','Display','DataType'}
SERIALIZATION_ATTRIBUTES = {'JsonIgnore','JsonPropertyName','JsonProperty','DataMember','IgnoreDataMember'}


def registration_call(tokens, index):
    token = tokens[index]
    if token.kind != 'code' or token.value not in REGISTRATIONS or values(tokens[index+1:index+2]) != '<': return None
    try:
        end = close(tokens,index+1,'<','>')
        if values(tokens[end+1:end+2]) != '(': return None
        last = close(tokens,end+1,'(',')')
    except ValueError: return None
    name = ''.join(t.value for t in tokens[index+2:end])
    return name, tokens[end+2:last], REGISTRATIONS[token.value]


def runtime_conditions(tokens, index):
    stack = []; start = 0
    for i,t in enumerate(tokens[:index]):
        if t.kind != 'code': continue
        if t.value == '{':
            head = tokens[start:i]
            conditional = any(x.value in {'if','else','switch','case','for','foreach','while','catch'} for x in head)
            stack.append({'kind':'runtime_condition','line':t.line,'expression':values(head)} if conditional else None)
            start = i+1
        elif t.value == '}':
            if stack: stack.pop()
            start = i+1
        elif t.value == ';': start = i+1
    # Single-statement conditionals have no braces.
    tail = tokens[start:index]
    active = [s for s in stack if s]
    if any(t.value in {'if','else','switch','for','foreach','while'} for t in tail):
        active.append({'kind':'runtime_condition','line':tail[0].line,'expression':values(tail)})
    return active


def external_properties(reader, name, model):
    match = re.fullmatch(r'(?:Microsoft\.AspNetCore\.Identity\.)?IdentityUser(?:<([\w.]+)>)?',name)
    if not match: return None
    if '.' not in name.split('<')[0] and 'Microsoft.AspNetCore.Identity' not in reader.namespace_imports(model): return None
    key_type = match[1] or 'string'
    if key_type not in {'string','Guid','System.Guid','int','long'}: return None
    packages = reader.projects.get(model['project'],{}).get('packages',[])
    evidence = [p for p in packages if p['name'] in {'Microsoft.Extensions.Identity.Stores','Microsoft.AspNetCore.Identity.EntityFrameworkCore'} and not p['conditional']]
    versions = {p['version'] for p in evidence}
    contract = next((c for c in CONTRACTS if versions == {c['version']}),None)
    if not contract:
        reader.gap(model['path'],model['line'],'External Identity model version is unavailable or unsupported; provide current framework source/schema evidence',code='DOTNET_EXTERNAL_VERSION',details={'requested_type':name,'observed_versions':sorted(v or 'unresolved' for v in versions),'supported_versions':[c['version'] for c in CONTRACTS]})
        return None
    adapter = {k:contract[k] for k in ('adapter_id','version','source_url','source_sha256','license')}
    adapter['package_refs'] = sorted({ref for p in evidence for ref in (p['ref'],p.get('version_ref',p['ref']))})
    return [{'name':f['name'],'type':f['clr_type'].replace('TKey',key_type),'line':model['line'],
             'attributes':[],'owner':model,'framework_contract':adapter} for f in contract['fields']]


def identity_default_models(reader, model):
    """Emit pinned default schemas as candidates; configuration still requires investigation."""
    packages = reader.projects.get(model['project'], {}).get('packages', [])
    evidence = [p for p in packages if p['name'] == 'Microsoft.AspNetCore.Identity.EntityFrameworkCore' and not p['conditional']]
    versions = {p['version'] for p in evidence}
    contract = next((c for c in CONTRACTS if versions == {c['version']} and c.get('default_tables')), None)
    if contract is None: return []
    refs = sorted({ref for p in evidence for ref in (p['ref'], p.get('version_ref', p['ref']))})
    result = []
    for table in contract['default_tables']:
        adapter = {k: contract[k] for k in ('adapter_id', 'version', 'license', 'schema_sources')}
        adapter.update({k: table[k] for k in ('source_url', 'source_sha256')})
        adapter['package_refs'] = refs
        synthetic = dict(model, name=table['type'], qualified='Microsoft.AspNetCore.Identity.'+table['type'],
                         body=[], header=[], modifiers=[], attributes=[])
        synthetic['contract_properties'] = [dict(name=f['name'], type=f['clr_type'].replace('TKey', 'string'),
            line=model['line'], attributes=[], owner=synthetic, framework_contract=adapter) for f in table['fields']]
        result.append((table['table'], synthetic))
    return result
