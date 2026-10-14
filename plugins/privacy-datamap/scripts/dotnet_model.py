"""Evidence-bounded model-builder traversal, independent of application naming."""
import re
from dotnet_syntax import Token, values, close, split, literal_arg, namespace_usings


def identity(model):
    return model['project'], model['qualified']


class ModelDiscovery:
    def reachable_projects(self, project):
        reached, queue = {project}, [project]
        while queue:
            for ref in self.projects.get(queue.pop(), {}).get('project_references', []):
                if ref not in reached: reached.add(ref); queue.append(ref)
        return reached

    def namespace_imports(self, model):
        namespaces = {model.get('namespace', '')}
        if not model.get('namespace'):
            namespaces.update(m['namespace'] for m in self.models if m['path']==model['path'] and m['line']<=model.get('line',0)<=max([m['line']]+[t.line for t in m['body']]))
        for path, source in self.sources.items():
            if source['project'] != model['project']: continue
            for name, global_using, scope, _ in namespace_usings(source['tokens']):
                if global_using or (path == model['path'] and any(not scope or n == scope or n.startswith(scope+'.') for n in namespaces)):
                    namespaces.add(name)
        return namespaces

    def source_type_candidates(self, name, owner):
        name = name.replace('global::','').rstrip('?').split('<',1)[0]
        allowed = self.reachable_projects(owner['project'])
        matches = [m for m in self.models if m['project'] in allowed and m['qualified'] in {name,owner['namespace']+'.'+name}]
        if not matches: matches = [m for m in self.models if m['project'] in allowed and m['name']==name and m['namespace'] in self.namespace_imports(owner)]
        return matches

    def context_kind(self, model, seen=()):
        key = identity(model)
        if key in seen or len(seen) >= 16: return None
        header = model['header']
        colon = next((i for i,t in enumerate(header) if t.value == ':'),None)
        if colon is None: return None
        base = split(header[colon+1:])[0]
        invocation = next((i for i,t in enumerate(base) if t.value=='('),len(base))
        name = ''.join(t.value for t in base[:invocation]).replace('global::','')
        simple = name.split('<')[0].split('.')[-1]
        local = self.resolve(name,model,required=False)
        if local:
            return None if local['kind']=='interface' else self.context_kind(local,(*seen,key))
        if self.source_type_candidates(name,model):
            if simple in {'DbContext','IdentityDbContext'}:
                self.gap(model['path'],model['line'],'Context base resolves to ambiguous source symbols',code='DOTNET_TYPE_AMBIGUOUS')
            return None
        imports = self.namespace_imports(model)
        if name in {'Microsoft.EntityFrameworkCore.DbContext','System.Data.Entity.DbContext'}: return 'ef'
        if name == 'DbContext' and imports & {'Microsoft.EntityFrameworkCore','System.Data.Entity'}: return 'ef'
        if simple == 'IdentityDbContext' and ('Microsoft.AspNetCore.Identity.EntityFrameworkCore' in imports or name.startswith('Microsoft.AspNetCore.Identity.EntityFrameworkCore.')): return 'identity'
        return None

    def source_base(self, model):
        header = model['header']; colon = next((i for i,t in enumerate(header) if t.value==':'),None)
        if colon is None: return None
        part = split(header[colon+1:])[0]
        invocation = next((i for i,t in enumerate(part) if t.value=='('),len(part))
        base = self.resolve(''.join(t.value for t in part[:invocation]),model,required=False)
        return base if base and base['kind']!='interface' else None

    def context_sources(self, context):
        sources=[]; current=context; seen=set()
        while current and identity(current) not in seen and len(seen)<16:
            seen.add(identity(current)); sources.append(current); current=self.source_base(current)
        if current: self.gap(context['path'],context['line'],'Context inheritance cycle or depth limit requires investigation',code='DOTNET_MODEL_TRAVERSAL_LIMIT')
        return sources

    def source_methods(self, model):
        tokens = model['body']; i = 0; result = []
        while i < len(tokens):
            start = i
            while i < len(tokens) and tokens[i].value not in {'{',';','=>'}: i += 1
            if i == len(tokens): break
            head = tokens[start:i]
            if tokens[i].value != '{':
                while i < len(tokens) and tokens[i].value != ';': i += 1
                i += 1; continue
            end = close(tokens, i)
            opening = next((j for j,t in enumerate(head) if t.value == '('), None)
            if opening and head[opening-1].kind == 'code':
                try: paren_end = close(head, opening, '(', ')')
                except ValueError: i = end + 1; continue
                result.append({'name':head[opening-1].value, 'parameters':split(head[opening+1:paren_end]),
                               'body':tokens[i+1:end], 'line':head[opening-1].line, 'owner':model,
                               'static':any(t.value == 'static' for t in head)})
            i = end + 1
        return result

    def discover_entities(self, context):
        try: return self._discover_entities(context)
        except (ValueError,IndexError,RecursionError):
            self.gap(context['path'],context['line'],'Malformed model-builder registration requires source investigation',code='DOTNET_MODEL_SYNTAX')
            return self.entity_registry.get(identity(context),{'entities':{},'schema':None,'refs':[]})

    def _discover_entities(self, context):
        key = identity(context)
        if key in self.entity_registry: return self.entity_registry[key]
        result = {'entities':{}, 'schema':None, 'refs':[]}; self.entity_registry[key] = result
        excluded = set(); budget = [128]

        def register(name, owner, line, provenance, kind, table=None):
            target = self.resolve(name, owner)
            if not target: return None
            target_key = identity(target)
            entry = result['entities'].setdefault(target_key, {'target':target, 'table':table or target['name'], 'blocks':[], 'registrations':[], 'configs':set()})
            if table: entry['table'] = table
            refs = sorted(set(provenance + [f"{owner['path']}:{line}"]))
            registration = {'kind':kind, 'refs':refs}
            if registration not in entry['registrations']: entry['registrations'].append(registration)
            return entry

        for source in self.context_sources(context):
            for prop in self.properties(source):
                match = re.fullmatch(r'(?:global::)?(?:(?:Microsoft\.EntityFrameworkCore|System\.Data\.Entity)\.)?DbSet<(.+)>',prop['type'])
                if match: register(match[1],source,prop['line'],[f"{context['path']}:{context['line']}"],'dbset',prop['name'])

        def block(entry, owner, receiver, body, provenance):
            entry['blocks'].append({'owner':owner,'receiver':receiver,'body':body,'refs':provenance})

        def walk(body, owner, receiver, provenance, stack):
            budget[0] -= 1
            if budget[0] < 0 or len(stack) >= 16:
                self.gap(owner['path'],owner['line'],'Model-builder traversal limit reached; supply bounded registration evidence', code='DOTNET_MODEL_TRAVERSAL_LIMIT'); return
            for statement in split(body, ';'):
                if not statement: continue
                if values(statement) == 'return '+receiver: continue
                chain = self.fluent_chain(statement)
                if not chain or chain[0] != receiver:
                    if values(statement[:3]) == 'base . OnModelCreating':
                        parent = self.source_base(owner)
                        inherited = [m for m in self.source_methods(parent) if m['name']=='OnModelCreating'] if parent else []
                        if len(inherited)==1 and len(inherited[0]['parameters'])==1 and values(statement[4:-1])==receiver:
                            method = inherited[0]; call_key=(parent['path'],parent['qualified'],method['name'],method['line'])
                            if call_key in stack: self.gap(owner['path'],statement[0].line,'Model-builder inheritance cycle requires investigation',code='DOTNET_EXTENSION_CYCLE')
                            else: walk(method['body'],parent,method['parameters'][0][-1].value,provenance+[f"{owner['path']}:{statement[0].line}",f"{parent['path']}:{method['line']}"],(*stack,call_key))
                        else: self.gap(owner['path'],statement[0].line,'Inherited model-builder behavior requires source or framework adapter evidence', code='DOTNET_MODEL_BASE_CALL')
                    else:
                        self.gap(owner['path'],statement[0].line,'Conditional or unsupported model-builder statement requires investigation', code='DOTNET_MODEL_CONDITIONAL')
                    continue
                _, calls = chain; method, generic, args, line = calls[0]
                refs = provenance + [f"{owner['path']}:{line}"]
                if method == 'HasDefaultSchema' and len(calls) == 1:
                    name = literal_arg(args)
                    if name and result['schema'] in {None,name}: result['schema'] = name; result['refs'] += refs
                    else: self.gap(owner['path'],line,'Dynamic or conflicting default schema requires investigation', code='DOTNET_SCHEMA_AMBIGUOUS')
                elif method == 'Ignore' and generic and not args and len(calls) == 1:
                    target = self.resolve(''.join(t.value for t in generic),owner)
                    if target: excluded.add(identity(target))
                elif method == 'Entity' and generic:
                    name = ''.join(t.value for t in generic)
                    entry = register(name,owner,line,refs,'entity_call')
                    if not entry: continue
                    if args:
                        if len(calls) == 1 and len(args) >= 4 and args[1].value == '=>' and args[2].value == '{' and close(args,2) == len(args)-1:
                            block(entry,owner,args[0].value,args[3:-1],refs)
                        else: self.gap(owner['path'],line,'Unsupported entity callback requires mapping investigation',code='DOTNET_ENTITY_CALLBACK')
                    elif len(calls) > 1:
                        # Reuse the entity-builder interpreter with the evidenced receiver.
                        index = next(i for i,t in enumerate(statement) if t.value == '(')
                        end = close(statement,index,'(',')')
                        block(entry,owner,'entity',[Token('entity',line),*statement[end+1:]],refs)
                elif method == 'ApplyConfiguration' and len(calls) == 1:
                    if values(args[:1]) != 'new' or values(args[-2:]) != '( )':
                        self.gap(owner['path'],line,'Dynamic configuration application requires investigation',code='DOTNET_CONFIGURATION_DYNAMIC'); continue
                    config = self.resolve(''.join(t.value for t in args[1:-2]),owner)
                    if not config: continue
                    match = re.search(r'\bIEntityTypeConfiguration\s*<\s*([\w.]+)\s*>',values(config['header']))
                    methods = [m for m in self.source_methods(config) if m['name'] == 'Configure']
                    if self.source_type_candidates('IEntityTypeConfiguration',config):
                        self.gap(config['path'],config['line'],'Configuration interface resolves to a source-defined symbol',code='DOTNET_CONFIGURATION_BINDING'); continue
                    if not match or len(methods) != 1 or len(methods[0]['parameters']) != 1:
                        self.gap(config['path'],config['line'],'Configuration signature is ambiguous or unsupported',code='DOTNET_CONFIGURATION_SIGNATURE'); continue
                    parameter = methods[0]['parameters'][0]
                    if not re.fullmatch(r'EntityTypeBuilder < [\w .]+ > \w+',values(parameter)):
                        self.gap(config['path'],config['line'],'Configuration builder parameter is unresolved',code='DOTNET_CONFIGURATION_SIGNATURE'); continue
                    entry = register(match[1],config,config['line'],refs,'applied_configuration')
                    if not entry: continue
                    entry['configs'].add(identity(config))
                    block(entry,config,parameter[-1].value,methods[0]['body'],refs+[f"{config['path']}:{methods[0]['line']}"])
                else:
                    candidates = []
                    allowed = self.reachable_projects(owner['project']); namespaces = self.namespace_imports(owner)
                    for candidate in self.models:
                        if candidate['project'] not in allowed or candidate['namespace'] not in namespaces: continue
                        for extension in self.source_methods(candidate):
                            if extension['name'] != method or not extension['static']: continue
                            params = extension['parameters']
                            if params and re.fullmatch(r'this (?:Microsoft \. EntityFrameworkCore \. )?ModelBuilder \w+',values(params[0])): candidates.append(extension)
                    if len(candidates) != 1 or args or generic or len(calls) != 1 or len(candidates[0]['parameters']) != 1:
                        self.gap(owner['path'],line,'Model-builder extension call is unsupported or ambiguous; resolve the invoked method',code='DOTNET_EXTENSION_BINDING'); continue
                    extension = candidates[0]; callee = extension['owner']
                    call_key = (callee['path'],callee['qualified'],extension['name'],extension['line'])
                    if call_key in stack:
                        self.gap(owner['path'],line,'Model-builder extension cycle requires bounded registration evidence',code='DOTNET_EXTENSION_CYCLE'); continue
                    walk(extension['body'],callee,extension['parameters'][0][-1].value,refs+[f"{callee['path']}:{extension['line']}"],(*stack,call_key))

        method_owner = context; methods=[]
        for source in self.context_sources(context):
            methods=[m for m in self.source_methods(source) if m['name']=='OnModelCreating']
            if methods: method_owner=source; break
        if len(methods) > 1:
            self.gap(context['path'],context['line'],'Multiple model-builder entry points require symbol resolution',code='DOTNET_MODEL_ENTRYPOINT')
        elif methods:
            method = methods[0]; params = method['parameters']
            shadow = self.source_type_candidates('ModelBuilder',context)
            if shadow:
                self.gap(context['path'],method['line'],'ModelBuilder resolves to a source-defined symbol, not the framework builder',code='DOTNET_MODEL_BUILDER_BINDING')
            elif len(params) == 1 and re.fullmatch(r'(?:Microsoft \. EntityFrameworkCore \. )?ModelBuilder \w+',values(params[0])):
                walk(method['body'],method_owner,params[0][-1].value,[f"{context['path']}:{context['line']}"],())
            else: self.gap(context['path'],method['line'],'Model-builder parameter is not established',code='DOTNET_MODEL_ENTRYPOINT')
        for target in excluded:
            entry = result['entities'].pop(target,None)
            if entry and any(r['kind']!='dbset' for r in entry['registrations']): self.gap(context['path'],context['line'],'Entity is both explicitly registered and excluded; resolve model-builder precedence',code='DOTNET_MEMBER_PRECEDENCE')
        return result
