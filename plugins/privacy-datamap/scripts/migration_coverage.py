"""Migration identity adapters; historical columns never establish current coverage."""
import hashlib
import json
import pathlib
import re
import sys
from dotnet_syntax import lex, namespace_usings


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def inspect(repo, request):
    repo = pathlib.Path(repo).resolve()
    dotnet = request.get('dotnet', {})
    projects = {p['path']:p for p in dotnet.get('projects', [])}
    edges = [e for e in dotnet.get('relationships', []) if e.get('context')]
    sources = {}; declarations = {}; records = []
    def project(path):
        choices = [p for p in projects if pathlib.PurePosixPath(path).is_relative_to(pathlib.PurePosixPath(p).parent)]
        return max(choices,key=len) if choices else None
    def reachable(start):
        seen = {start}; queue = [start]
        while queue:
            for p in projects.get(queue.pop(), {}).get('project_references', []):
                if p not in seen: seen.add(p); queue.append(p)
        return seen
    for path in request.get('migrations', []):
        full = repo / path
        row = {'path':path, 'project':project(path), 'contexts':[], 'declaration':None}
        if full.is_symlink() or not full.resolve().is_relative_to(repo) or not full.is_file() or full.stat().st_size > 1_000_000:
            sources[path] = row; continue
        raw = full.read_bytes(); row['sha256'] = hashlib.sha256(raw).hexdigest()
        if path.endswith('.cs'):
            try:
                tokens = lex(raw.decode('utf-8-sig')); code = ' '.join(t.value for t in tokens if t.kind=='code')
                namespace = re.search(r'\bnamespace\s+([\w. ]+)\s*[;{]',code)
                namespace = namespace[1].replace(' ','') if namespace else ''
                classes = re.findall(r'\bclass\s+(\w+)',code)
                if len(classes)!=1: raise ValueError('Migration declaration identity is ambiguous')
                row['declaration'] = (row['project'],namespace,classes[0])
                imports = {name for name,_,_,_ in namespace_usings(tokens)}
                qualified = r'Microsoft\s*\.\s*EntityFrameworkCore\s*\.\s*Infrastructure\s*\.\s*'
                pattern = r'\[\s*(' + qualified + r')?DbContext(?:Attribute)?\s*\(\s*typeof\s*\(\s*([\w. :]+)\s*\)\s*\)\s*\]'
                for match in re.finditer(pattern,code):
                    if not match[1] and 'Microsoft.EntityFrameworkCore.Infrastructure' not in imports: continue
                    symbols = [s for s in dotnet.get('type_symbols', []) if s['project'] in reachable(row['project'])]
                    if any(s['qualified']=='Microsoft.EntityFrameworkCore.Infrastructure.DbContextAttribute' or
                           (not match[1] and s['name'] in {'DbContext','DbContextAttribute'} and s['qualified'].rsplit('.',1)[0] in imports | {namespace}) for s in symbols):
                        continue
                    name = match[2].replace(' ','').replace('global::','')
                    candidates = {(e['context']['project'],e['context']['symbol'],e['context']['path']) for e in edges
                                  if e['context']['project'] in reachable(row['project']) and
                                  (e['context']['symbol']==name or e['context']['symbol']==namespace+'.'+name or
                                   any(e['context']['symbol']==imp+'.'+name for imp in imports))}
                    row['contexts'].extend(sorted(candidates))
            except (UnicodeError,ValueError,IndexError): pass
        sources[path] = row
        if row['declaration']:
            declarations.setdefault(row['declaration'],set()).update(row['contexts'])
    for path,row in sorted(sources.items()):
        contexts = set(row['contexts']) | declarations.get(row['declaration'],set())
        topology = request.get('topology', {}).get(path)
        matched = []
        if len(contexts)==1:
            context = next(iter(contexts))
            matched = [e for e in edges if (e['context']['project'],e['context']['symbol'],e['context']['path'])==context]
            stores = sorted({e['dataset'] for e in matched})
            identity = 'matched' if len(stores)==1 else 'ambiguous'
            scope = {'context':context[1], 'project':context[0], 'datastore':stores[0] if len(stores)==1 else None}
        elif topology:
            matched = [o for o in request.get('observations', []) if o.get('topology_ref')==topology['ref']]
            stores = sorted({request.get('datastore_keys', {}).get(o['boundary'], o['boundary']) for o in matched})
            identity = 'matched' if len(stores)==1 else 'ambiguous' if stores else 'unresolved'
            scope = {'topology_ref':topology['ref'], 'datastore':stores[0] if len(stores)==1 else None}
        else:
            stores=[]; identity='ambiguous' if contexts else 'unresolved'; scope={'project':row['project'],'path':path}
        relevant_paths = {ref.rsplit(':',1)[0] for edge in matched for ref in edge.get('refs', [])}
        relevant_paths.update(edge.get('schema_path',edge.get('path')) for edge in matched)
        relevant_paths.update(reachable(row['project']))
        gaps = [g for g in dotnet.get('gaps', []) + request.get('source_gaps', []) if g.get('ref','').rsplit(':',1)[0] in relevant_paths]
        # Identity never proves completeness: require an extracted nonempty definition for
        # every linked model and no known extraction limitation in its evidenced scope.
        tables = dotnet.get('tables', [])
        expected_paths = {name for name,claim in request.get('current_schema_claims', {}).items() if topology and claim.get('ref')==topology['ref']}
        missing_paths = sorted(expected_paths - {o.get('path') for o in matched})
        complete = identity=='matched' and bool(matched) and not gaps and not missing_paths
        if contexts:
            complete = complete and all(any(t['dataset']==edge['dataset'] and t['name']==edge['collection'] and t.get('fields') for t in tables) for edge in matched)
        else:
            complete = complete and all(o.get('collections') and all(c.get('fields') for c in o['collections']) for o in matched)
        record = {'adapter':'ef-context-attribute@1' if contexts else 'explicit-topology@1' if topology else 'unresolved@1',
                  'migration':path, 'scope':scope, 'identity_status':identity, 'schema_status':'covered' if complete else 'incomplete',
                  'datastores':stores, 'current_structures':matched, 'missing_current_sources':missing_paths, 'diagnostics':gaps,
                  'observation_files':{name:item.get('sha256') for name,item in sources.items() if name==path or (row['declaration'] and item['declaration']==row['declaration'])}, 'physical_identity':'unverified'}
        records.append(record)
    gaps=[]
    for record in records:
        if record['schema_status']=='covered': continue
        scope=record['scope']
        gaps.append({'format':'excluded_migrations','code':'MIGRATION_CURRENT_COVERAGE', 'ref':record['migration']+':1',
                     'diagnostic_id':'migration:'+digest([record['adapter'],scope,record['migration']]),
                     'reason':'Migration identity is '+record['identity_status']+'; current schema coverage is incomplete. Supply current definitions and evidence of their context/datastore binding.',
                     'scope':scope,'required_field_ids':sorted({t['dataset']+'/'+t['name']+'/'+f['name'] for t in dotnet.get('tables', []) if t['dataset'] in record['datastores'] for f in t.get('fields', [])}), 'coverage':record,'observation_files':record['observation_files'],
                     'resolution_requirement':'Establish context/datastore identity and separately prove current schema completeness; migration history cannot supply fields.'})
    return {'version':1,'records':records,'gaps':gaps}


if __name__=='__main__':
    print(json.dumps(inspect(sys.argv[1],json.load(sys.stdin)),sort_keys=True))
