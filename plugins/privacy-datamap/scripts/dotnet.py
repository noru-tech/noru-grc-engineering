"""Bounded offline .NET declarations and modeled storage bindings; never evaluates MSBuild."""
import hashlib
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET
from dotnet_syntax import Token, lex, values, close, split, attrs, attr, literal_arg
from dotnet_model import ModelDiscovery
from dotnet_adapters import REGISTRATIONS, registration_call, runtime_conditions, external_properties, identity_default_models, VALIDATION_ATTRIBUTES, SERIALIZATION_ATTRIBUTES
from dotnet_diagnostics import finalize as finalize_diagnostics, summary as diagnostic_summary

MAX_BYTES = 1_000_000
SKIP = {"bin", "obj", ".git", ".noru", "node_modules", "vendor", "packages"}
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LEX = re.compile(r'@?[A-Za-z_][A-Za-z0-9_]*|[0-9]+|=>|::|\?\.|[^\s]')
RESOURCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
SCALARS = set('string String bool Boolean byte Byte sbyte short Int16 ushort int Int32 uint long Int64 ulong float Single double Double decimal Decimal char Char Guid DateTime DateTimeOffset DateOnly TimeOnly TimeSpan ObjectId'.split())
PACKAGES = {"microsoft.entityframeworkcore": "entity_framework", "entityframework": "entity_framework",
            "microsoft.data.sqlclient": "sql_server", "system.data.sqlclient": "sql_server", "dapper": "dapper",
            "mongodb.driver": "mongodb", "awssdk.dynamodbv2": "dynamodb", "rabbitmq.client": "rabbitmq",
            "masstransit.rabbitmq": "rabbitmq", "serilog": "logging", "nlog": "logging",
            "elastic": "elastic", "grafana": "grafana", "openai": "openai", "azure.ai.openai": "azure_openai",
            "microsoft.semantickernel": "semantic_kernel", "npgsql": "postgresql",
            "aspire.npgsql": "postgresql", "aspire.hosting.postgresql": "postgresql"}


class Reader(ModelDiscovery):
    def __init__(self, repo, files, excluded=(), reviewed_bindings=None):
        self.repo = pathlib.Path(repo).resolve()
        self.files = sorted(set(files))
        self.excluded = set(excluded)
        self.reviewed_bindings = reviewed_bindings or {}
        self.projects, self.sources, self.models = {}, {}, []
        self.gaps, self.technologies, self.observations, self.relationships = [], [], [], []
        self.tables = []
        self.solutions = []
        self.inspected_configurations = set()
        self.entity_registry = {}
        self.runtime_bindings = []
        self.central_packages = {}
        self.entity_relationships = []

    def gap(self, path, line, reason, code=None, column=None, details=None):
        row = {'format': 'dotnet', 'ref': f'{path}:{line}', 'reason': reason}
        if code: row['code'] = code
        if details: row['details'] = details
        position = getattr(self,'_active_position',None)
        if column is not None: row['_column'] = column
        elif position and position['path']==path and position['line']==line: row['_column'] = position['column']
        scope = getattr(self,'_active_scope',None)
        if scope and scope['path']==path: row['_scope'] = {'kind':'type','path':path,'symbol':scope['qualified'],'project':scope['project']}
        if row not in self.gaps: self.gaps.append(row)

    def read(self, path):
        full = self.repo / path
        if full.is_symlink() or not full.resolve().is_relative_to(self.repo) or set(pathlib.PurePosixPath(path).parts) & SKIP:
            return None
        try:
            if full.stat().st_size > MAX_BYTES: raise ValueError('Source exceeds the one-megabyte inspection limit')
            return full.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError, ValueError):
            self.gap(path, 1, 'Source is unreadable or exceeds the inspection limit'); return None

    def project(self, path):
        parents = [p for p in self.projects if pathlib.PurePosixPath(path).is_relative_to(pathlib.PurePosixPath(p).parent)]
        if not parents:
            self.gap(path, 1, 'No project boundary established; confirm source membership and logical connection scope')
            return '.'
        depth = max(len(pathlib.PurePosixPath(p).parts) for p in parents)
        closest = [p for p in parents if len(pathlib.PurePosixPath(p).parts) == depth]
        if len(closest) != 1:
            self.gap(path, 1, 'Multiple projects share this source directory; source membership requires investigation')
            return None
        return closest[0]

    def solution_targets(self, path, targets):
        result = []
        for target in targets:
            resolved = (self.repo / pathlib.PurePosixPath(path).parent / target.replace('\\', '/')).resolve()
            if resolved.is_relative_to(self.repo) and resolved.relative_to(self.repo).as_posix() in self.excluded:
                continue
            if resolved.is_relative_to(self.repo) and resolved.relative_to(self.repo).as_posix() in self.files:
                result.append(resolved.relative_to(self.repo).as_posix())
            else:
                self.gap(path, 1, 'Solution references a missing or external project; repository coverage is incomplete')
        return sorted(set(result))

    def inventory(self):
        for path in self.files:
            if not (path.endswith(('.csproj', '.props', '.targets', '.sln', '.slnx'))): continue
            text = self.read(path)
            if text is None: continue
            if path.endswith('.sln'):
                targets = re.findall(r'Project\([^\n]*?=\s*"[^"\n]*",\s*"([^"\n]+\.csproj)"', text, re.I)
                self.solutions.append({'path': path, 'project_references': self.solution_targets(path, targets)})
                continue
            try:
                if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper(): raise ValueError()
                root = ET.fromstring(text)
            except (ET.ParseError, ValueError):
                self.gap(path, 1, 'Project XML cannot be inspected safely'); continue
            if path.endswith('.slnx'):
                targets = [node.get('Path', '') for node in root.iter() if node.tag.split('}')[-1] == 'Project']
                self.solutions.append({'path': path, 'project_references': self.solution_targets(path, targets)})
                continue
            project = {'path': path, 'refs': [f'{path}:1'], 'project_references': [], 'packages': []}
            tag_offsets = {}
            for node in root.iter():
                tag = node.tag.split('}')[-1]
                positions = list(re.finditer(r'<(?:[\w]+:)?'+re.escape(tag)+r'(?=[\s>])',text))
                offset = tag_offsets.get(tag,0); tag_offsets[tag] = offset+1
                node_line = text.count('\n',0,positions[offset].start())+1 if offset<len(positions) else 1
                if 'Condition' in node.attrib or tag in {'Import', 'Choose', 'Target'} or '$(' in ''.join(node.attrib.values()):
                    self.gap(path, node_line, 'Conditional/imported MSBuild settings are not evaluated; confirm source membership and package configuration')
                if tag == 'Compile':
                    self.gap(path, node_line, 'Explicit Compile items require source-membership investigation')
                if tag == 'EnableDefaultCompileItems' and (node.text or '').strip().lower() == 'false':
                    self.gap(path, node_line, 'Default C# source inclusion is disabled; confirm included sources')
                if tag == 'ProjectReference':
                    target = node.get('Include', '').replace('\\', '/')
                    resolved = (self.repo / pathlib.PurePosixPath(path).parent / target).resolve()
                    conditional = any('Condition' in ancestor.attrib for ancestor in root.iter() if node in list(ancestor.iter()))
                    if conditional:
                        self.gap(path,node_line,'Conditional project reference does not establish source membership',code='DOTNET_BUILD_MEMBERSHIP')
                    elif target and resolved.is_relative_to(self.repo) and resolved.relative_to(self.repo).as_posix() in self.files:
                        project['project_references'].append(resolved.relative_to(self.repo).as_posix())
                    else: self.gap(path, node_line, 'Project reference is external, dynamic, or missing from the scanned inventory')
                if tag in {'PackageReference', 'PackageVersion'}:
                    package = node.get('Include', node.get('Update', ''))
                    project['packages'].append({'name':package,'version':node.get('Version'),'conditional':any('Condition' in ancestor.attrib for ancestor in root.iter() if node in list(ancestor.iter())), 'ref':f'{path}:{node_line}'})
                    for prefix, technology in PACKAGES.items():
                        if package.lower() == prefix or package.lower().startswith(prefix + '.'):
                            self.technologies.append({'technology': technology, 'evidence_kind': 'dependency', 'ref': f'{path}:{node_line}'})
                            break
                    if package.lower().startswith(('microsoft.entityframeworkcore', 'entityframework', 'dapper', 'mongodb', 'awssdk.dynamodb', 'microsoft.data.sqlclient', 'system.data.sqlclient')):
                        self.gap(path, node_line, 'Persistence dependency declared; investigate runtime usage and model coverage before accepting an empty or partial map')
            if path.endswith('.csproj'): self.projects[path] = project
            elif pathlib.PurePosixPath(path).name=='Directory.Packages.props': self.central_packages[path] = project['packages']
        for project in self.projects.values():
            candidates = [p for p in self.central_packages if pathlib.PurePosixPath(project['path']).is_relative_to(pathlib.PurePosixPath(p).parent)]
            nearest = max(candidates,key=lambda p:len(pathlib.PurePosixPath(p).parts)) if candidates else None
            for package in project['packages']:
                if package['version'] is not None or nearest is None: continue
                definitions = [p for p in self.central_packages[nearest] if p['name'].lower()==package['name'].lower()]
                if len(definitions)==1 and not definitions[0]['conditional']:
                    package['version'] = definitions[0]['version']; package['version_ref'] = definitions[0]['ref']

    def parse(self, path, text):
        tokens = lex(text)
        self.sources[path] = {'tokens': tokens, 'text': text, 'project': self.project(path)}
        if self.sources[path]['project'] is None: return
        if any(t.value == 'using' and i+2 < len(tokens) and tokens[i+2].value == '=' for i,t in enumerate(tokens)):
            # Aliases are not compiler-resolved by this reader.
            self.gap(path, 1, 'Using aliases may affect type or attribute identity; investigate bindings')
        def declarations(start, end, namespace=''):
            i, pending, modifiers = start, [], []
            while i < end:
                t = tokens[i]
                if t.kind != 'code': i += 1; continue
                if t.value == '[': pending, i = attrs(tokens, i); continue
                if t.value in {'public','private','internal','protected','sealed','abstract','partial','static'}:
                    modifiers.append(t.value); i += 1; continue
                if t.value == 'namespace':
                    j = i+1
                    while j < end and tokens[j].value not in {';', '{'}: j += 1
                    name = ''.join(x.value for x in tokens[i+1:j]); name = '.'.join(filter(None,[namespace,name]))
                    if j == end: raise ValueError('Unterminated namespace')
                    if tokens[j].value == ';': namespace = name; i = j+1
                    else:
                        stop = close(tokens,j); declarations(j+1,stop,name); i=stop+1
                    pending, modifiers = [], []; continue
                if t.value in {'class','record','struct','enum','interface'}:
                    j = i+1
                    if t.value == 'record' and j < end and tokens[j].value in {'class','struct'}: j += 1
                    name = tokens[j].value; j += 1
                    if not IDENT.fullmatch(name): raise ValueError('Unsupported type declaration')
                    header_start = j
                    while j < end and tokens[j].value not in {'{',';'}:
                        if tokens[j].value == '(':
                            j = close(tokens,j,'(',')')+1
                        else: j += 1
                    if j == end: raise ValueError('Unterminated type declaration')
                    stop = close(tokens,j) if tokens[j].value == '{' else j
                    model = {'name': name, 'qualified': '.'.join(filter(None,[namespace,name])), 'namespace': namespace,
                             'path':path,'line':t.line,'project':self.sources[path]['project'], 'attributes':pending,
                             'header':tokens[header_start:j], 'body':tokens[j+1:stop], 'modifiers':modifiers, 'kind':t.value}
                    self.models.append(model); i=stop+1; pending,modifiers=[],[]; continue
                if t.value == '{': i=close(tokens,i)+1
                else: i += 1
                pending, modifiers = [], []
        declarations(0,len(tokens))

    def resolve(self, name, owner, required=True):
        name = name.replace('global::','').rstrip('?')
        type_args = None
        if '<' in name and name.endswith('>'):
            root_name, argument_text = name.split('<',1)
            try: type_args = [''.join(t.value for t in part) for part in split(lex(argument_text[:-1]))]
            except ValueError: type_args = None
            if type_args: name = root_name
        matches = self.source_type_candidates(name,owner)
        if len(matches) != 1:
            if required: self.gap(owner['path'],owner['line'],'Referenced model is missing or ambiguous; resolve its namespace/project mapping',code='DOTNET_TYPE_MISSING' if not matches else 'DOTNET_TYPE_AMBIGUOUS',details={'requested_type':name,'candidate_definitions':[f"{m['path']}:{m['line']}" for m in matches]})
            return None
        target = matches[0]
        if type_args is not None:
            header = target['header']
            if not header or header[0].value!='<': return None
            end = close(header,0,'<','>')
            parameters = [values(part) for part in split(header[1:end])]
            if len(parameters)!=len(type_args) or not all(IDENT.fullmatch(p) for p in parameters):
                if required: self.gap(owner['path'],owner['line'],'Generic type arguments cannot be bound to source parameters',code='DOTNET_GENERIC_BINDING')
                return None
            substitutions = dict(zip(parameters,type_args))
            target = dict(target, qualified=target['qualified']+'<'+','.join(type_args)+'>',
                          header=[Token(substitutions.get(t.value,t.value),t.line,t.kind) for t in header[end+1:]], type_arguments=substitutions)
        return target

    def span(self, model, line):
        text = self.sources.get(model['path'],{}).get('text')
        if text is None: text = self.read(model['path']) or ''
        lines=text.splitlines()
        return {'path':model['path'],'start_line':line,'end_line':line,'start_column':1,
                'end_column':len(lines[line-1])+1,'sha256':hashlib.sha256((self.repo/model['path']).read_bytes()).hexdigest(),
                'extractor':'dotnet-structural@1'}

    def properties(self, model):
        if "contract_properties" in model: return model["contract_properties"]
        try:
            properties = self._properties(model)
            for prop in properties:
                for parameter, argument in model.get('type_arguments',{}).items():
                    prop['type'] = re.sub(r'\b'+re.escape(parameter)+r'\b',argument,prop['type'])
            return properties
        except (ValueError, IndexError, RecursionError):
            self.gap(model['path'], model['line'], 'Unsupported or malformed member declaration requires source investigation')
            return []

    def _properties(self, model):
        body, props, i = model['body'], [], 0
        header = model['header']
        if header and header[0].value == '(' and model['kind'] == 'record':
            for param in split(header[1:close(header,0,'(',')')]):
                attributes,j=attrs(param,0)
                words=param[j:]
                if len(words)>=2 and all(t.value != '=' for t in words):
                    props.append({'name':words[-1].value,'type':''.join(t.value for t in words[:-1]),'line':words[-1].line,'attributes':attributes,'column':words[-1].column})
                else: self.gap(model['path'],model['line'],'Record parameter has unsupported mapping or default')
        while i < len(body):
            attributes,i=attrs(body,i)
            start=i
            while i < len(body) and body[i].value not in {'{',';','=>'}: i+=1
            head=body[start:i]
            if i == len(body): break
            end=close(body,i) if body[i].value=='{' else i
            modifiers={'public','private','protected','internal','virtual','override','required','new','readonly','static','abstract'}
            words=[t for t in head if t.value not in modifiers]
            if 'public' in [t.value for t in head] and 'static' not in [t.value for t in head] and len(words)>=2 and not any(t.value in {'(', '=', 'class', 'record', 'struct', 'event'} for t in words):
                access=values(body[i+1:end]) if body[i].value=='{' else ''
                backing = re.fullmatch(r'get \{ return (\w+) ; \} (?:(?:private|protected|internal) )?set \{ (\w+) = value ; \}', access)
                backed = backing and backing[1] == backing[2] and re.search(r'\b'+re.escape(' '.join(t.value for t in words[:-1]))+r' '+re.escape(backing[1])+r' ;', values(body))
                if IDENT.fullmatch(words[-1].value) and (re.fullmatch(r'get ; (?:(?:private|protected|internal) )?(?:set|init) ;',access) or backed):
                    props.append({'name':words[-1].value,'type':''.join(t.value for t in words[:-1]),'line':words[-1].line,'attributes':attributes,'column':words[-1].column})
                else: self.gap(model['path'],head[0].line,'Public member is not a supported auto-property; investigate persisted fields, computed accessors or read-only mapping')
            i=end+1
            # Auto-property initializers do not change the declared shape; never emit their values.
            if i<len(body) and body[i].value=='=':
                while i<len(body) and body[i].value!=';':
                    if body[i].value=='{': i=close(body,i)
                    i+=1
                i+=1
        return props

    def method_body(self, model, name):
        tokens = model['body']; matches = []
        for i,t in enumerate(tokens):
            if t.kind != 'code' or t.value != name or values(tokens[i+1:i+2]) != '(': continue
            end = close(tokens, i+1, '(', ')')
            if values(tokens[end+1:end+2]) == '{': matches.append(tokens[end+2:close(tokens,end+1)])
        if len(matches) == 1: return matches[0]
        return []

    def fluent_chain(self, statement):
        if not statement or statement[0].kind != 'code': return None
        receiver = statement[0].value; calls = []; i = 1
        while i < len(statement):
            if values(statement[i:i+1]) != '.' or i+2 >= len(statement): return None
            method = statement[i+1]; i += 2; generic = []
            if statement[i].value == '<':
                end = close(statement, i, '<', '>'); generic = statement[i+1:end]; i = end+1
            if i >= len(statement) or statement[i].value != '(': return None
            end = close(statement, i, '(', ')')
            calls.append((method.value, generic, statement[i+1:end], method.line)); i = end+1
        return (receiver, calls) if calls else None

    def member_selector(self, args):
        literal = literal_arg(args)
        if literal: return literal
        if len(args) == 5 and values(args[1:3]) == '=> '+args[0].value and args[3].value == '.' and IDENT.fullmatch(args[4].value):
            return args[4].value
        return None

    def fluent_mapping(self, context, model):
        try:
            return self._fluent_mapping(context, model)
        except (ValueError, IndexError, RecursionError):
            self.gap(context['path'],context['line'],'Malformed or unsupported fluent configuration requires investigation')
            return {'properties':{},'ignore':set(),'navigation':set(),'owned':set(),'refs':[],'table':None,'schema':None}

    def _fluent_mapping(self, context, model):
        mapping = {'properties': {}, 'ignore': set(), 'navigation': set(), 'owned': set(), 'refs': [], 'table': None, 'schema': None}
        discovered = self.discover_entities(context)
        entry = discovered['entities'].get((model['project'],model['qualified']))
        mapping['schema'] = discovered['schema']; mapping['refs'] = list(discovered['refs'])
        if not entry: return mapping
        mapping['registrations'] = entry['registrations']
        for registration in entry['registrations']: mapping['refs'] += registration['refs']
        if len(entry['configs']) > 1:
            self.gap(context['path'],context['line'],'Multiple configurations target one entity; mapping precedence requires investigation', code='DOTNET_CONFIGURATION_PRECEDENCE')
            return mapping
        for block in entry['blocks']:
            config = block['owner']; body = block['body']; parameter = block['receiver']
            self.inspected_configurations.add((config['path'],config['qualified']))
            mapping['refs'] += block['refs']
            for statement in split(body, ';'):
                if not statement: continue
                chain = self.fluent_chain(statement)
                if not chain or chain[0] != parameter:
                    self.gap(config['path'],statement[0].line,'Conditional, reassigned or unsupported fluent builder requires investigation'); continue
                _, calls = chain; method, generic, args, line = calls[0]
                ref = f"{config['path']}:{line}"; mapping['refs'].append(ref)
                if method == 'ToTable' and len(calls) == 1:
                    parts = split(args); names = [literal_arg(p) for p in parts]
                    if 1 <= len(names) <= 2 and all(names) and mapping['table'] is None:
                        mapping['table'] = names[0]
                        if len(names) == 2: mapping['schema'] = names[1]
                    else: self.gap(config['path'],line,'Dynamic or conflicting table mapping requires investigation')
                    continue
                member = self.member_selector(args)
                if method == 'Ignore' and member and len(calls) == 1: mapping['ignore'].add(member); continue
                if method == 'OwnsOne' and member and len(calls) == 1: mapping['owned'].add(member); continue
                if method in {'HasOne','HasMany'}:
                    if member: mapping['navigation'].add(member)
                    target_name = ''.join(t.value for t in generic)
                    if not target_name and member:
                        declaration = next((p for p in self.inherited_properties(model) if p['name']==member),None)
                        if declaration:
                            target_name = declaration['type'].rstrip('?')
                            collection = re.fullmatch(r'(?:ICollection|IReadOnlyCollection|List|IEnumerable)<(.+)>',target_name)
                            if collection: target_name = collection[1]
                    target = self.resolve(target_name,model,required=False) if target_name else None
                    foreign_keys = []
                    unsupported = False
                    inverse = None
                    for operation, dependent_type, arguments, _ in calls[1:]:
                        if operation == 'HasForeignKey':
                            if dependent_type and ''.join(t.value for t in dependent_type) not in {model['name'],model['qualified']}: unsupported=True
                            selector = self.member_selector(arguments)
                            if selector: foreign_keys.append(selector)
                            else: unsupported = True
                        elif operation in {'WithOne','WithMany'}:
                            inverse = 'one' if operation=='WithOne' else 'many'
                            if arguments and not self.member_selector(arguments): unsupported=True
                        elif operation not in {'OnDelete','IsRequired'}: unsupported = True
                    edge = {'source_model':model['qualified'],'target_model':target['qualified'] if target else None,
                            'navigation':member,'kind':'many' if method=='HasMany' else 'one','foreign_key_members':foreign_keys,
                            'cardinality':{'source_to_target':'many' if method=='HasMany' else 'one','target_to_source':inverse},'foreign_key_representation':'clr_members',
                            'refs':sorted(set(mapping['refs']+[ref])),'status':'unresolved' if unsupported or not target else 'modeled'}
                    mapping.setdefault('entity_relationships',[]).append(edge)
                    if unsupported or not target: self.gap(config['path'],line,'Relationship target or foreign-key mapping is unresolved',code='DOTNET_RELATIONSHIP_BINDING')
                    continue
                if method in {'HasKey','HasIndex'}:
                    self.gap(config['path'],line,'Key/index configuration requires review beyond scalar field mapping'); continue
                if method != 'Property' or not member:
                    self.gap(config['path'],line,'Unsupported fluent mapping or selector requires investigation'); continue
                if member in mapping['properties']:
                    self.gap(config['path'],line,'Repeated property configuration requires precedence investigation'); continue
                prop = {'refs': [ref], 'declared_type': ''.join(t.value for t in generic) or None}
                for operation, type_args, arguments, operation_line in calls[1:]:
                    if operation in {'HasColumnName','HasField'}:
                        name = literal_arg(arguments)
                        if name: prop[operation] = name
                        else: self.gap(config['path'],operation_line,'Dynamic column or backing-field mapping requires investigation')
                    elif operation == 'HasColumnType':
                        if len(arguments)==1 and arguments[0].kind=='string' and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\([0-9]+(?:,[0-9]+)?\))?(?:\[\])?',arguments[0].value): prop['database_type']=arguments[0].value
                        else: self.gap(config['path'],operation_line,'Database type declaration is dynamic or unsupported',code='DOTNET_DATABASE_TYPE')
                    elif operation == 'HasConversion':
                        converted = ''.join(t.value for t in type_args)
                        if converted == 'string' and not arguments: prop['conversion'] = converted
                        else:
                            prop['conversion_unresolved'] = True
                            self.gap(config['path'],operation_line,'Custom conversion requires explicit stored-type investigation')
                    elif operation not in {'HasMaxLength','IsRequired','UseHiLo','ValueGeneratedNever','ValueGeneratedOnAdd','HasPrecision'}:
                        self.gap(config['path'],operation_line,'Unsupported property configuration may change stored shape')
                mapping['properties'][member] = prop
        for member in mapping['ignore'] & (set(mapping['properties']) | mapping['owned']):
            self.gap(context['path'],context['line'],'Excluded member also has explicit persistence mapping; resolve precedence',code='DOTNET_MEMBER_PRECEDENCE')
        return mapping

    def declared_backing_fields(self, model, seen=()):
        key = (model['project'],model['qualified'])
        if key in seen or len(seen)>=16: return {}
        fields = {}; tokens = model['body']; i = 0
        while i < len(tokens):
            attributes, i = attrs(tokens, i); start = i
            while i < len(tokens) and tokens[i].value not in {';', '{', '=>'}: i += 1
            if i == len(tokens): break
            if tokens[i].value == '{': i = close(tokens, i)+1; continue
            head = tokens[start:i]; i += 1
            if any(t.value in {'(', '=>', ','} for t in head): continue
            head = head[:next((j for j,t in enumerate(head) if t.value == '='),len(head))]
            words = [t for t in head if t.value not in {'public','private','protected','internal','readonly','volatile'}]
            if len(words) < 2 or not IDENT.fullmatch(words[-1].value) or any(t.value == 'static' for t in words): continue
            fields[words[-1].value] = {'name':words[-1].value,'type':''.join(t.value for t in words[:-1]),'line':words[-1].line,'attributes':attributes,'owner':model,'column':words[-1].column}
        for field in fields.values():
            for parameter, argument in model.get('type_arguments',{}).items(): field['type']=re.sub(r'\b'+re.escape(parameter)+r'\b',argument,field['type'])
        colon = next((i for i,t in enumerate(model['header']) if t.value==':'),None)
        if colon is not None:
            for part in split(model['header'][colon+1:]):
                base = self.resolve(''.join(t.value for t in part),model,required=False)
                if base and base['kind'] != 'interface':
                    for name, field in self.declared_backing_fields(base,(*seen,key)).items():
                        if name in fields:
                            fields.pop(name,None)
                            self.gap(model['path'],model['line'],'Inherited backing-field name is ambiguous',code='DOTNET_BACKING_BINDING')
                        else: fields[name] = field
        return fields

    def inherited_properties(self, model, seen=()):
        identity = (model['project'], model['qualified'])
        if identity in seen or len(seen) >= 16:
            self.gap(model['path'], model['line'], 'Cyclic or deep inheritance requires investigation'); return []
        props = [dict(p, owner=model) for p in self.properties(model)]
        header = model['header']
        colon = next((i for i,t in enumerate(header) if t.value == ':'), None)
        if colon is None: return props
        for part in split(header[colon+1:]):
            name = ''.join(t.value for t in part)
            if name in {'object','System.Object'}: continue
            framework = external_properties(self,name,model) if not self.source_type_candidates(name,model) else None
            if framework is not None:
                for prop in framework:
                    if any(p['name'] == prop['name'] for p in props): self.gap(model['path'],model['line'],'Source member conflicts with framework contract; resolve storage mapping',code='DOTNET_EXTERNAL_SHADOW')
                    else: props.append(prop)
                continue
            base = self.resolve(name, model)
            if base is None:
                self.gap(model['path'],model['line'],'External or unresolved inherited model: base fields and framework storage definitions are not synthesized'); continue
            if base['kind'] == 'interface': continue
            for prop in self.inherited_properties(base, (*seen,identity)):
                existing = next((p for p in props if p['name'] == prop['name']), None)
                if existing:
                    self.gap(model['path'],model['line'],'Hidden or overridden inherited member requires mapping investigation')
                else: props.append(prop)
        return props

    def fields(self, model, dialect, seen=(), mapping=None):
        previous = getattr(self,'_active_scope',None)
        position = getattr(self,'_active_position',None)
        self._active_scope = model; self._active_position = None
        try: return self._fields(model,dialect,seen,mapping)
        finally: self._active_scope = previous; self._active_position = position

    def _fields(self, model, dialect, seen=(), mapping=None):
        if model['qualified'] in seen or len(seen)>=8:
            self.gap(model['path'],model['line'],'Recursive or deep nested model requires shape investigation'); return []
        if 'partial' in model['modifiers'] or (model['header'] and model['header'][0].value=='<'):
            self.gap(model['path'],model['line'],'Partial or generic model requires investigation beyond declared members')
        out=[]
        props = self.inherited_properties(model)
        if mapping:
            backing = self.declared_backing_fields(model)
            for name, config in mapping['properties'].items():
                if config.get('HasField'):
                    field = backing.get(config['HasField'])
                    declaration = next((p for p in props if p['name']==name),None)
                    if field is None or (declaration and field['type']!=declaration['type']):
                        config['backing_unresolved']=True
                        self.gap(model['path'],model['line'],'Backing field is missing or its type conflicts with the mapped property',code='DOTNET_BACKING_BINDING')
                    elif field: config['backing_evidence']=[self.span(field['owner'],field['line'])]
                if not any(p['name'] == name for p in props):
                    field = backing.get(config.get('HasField', name))
                    if field: props.append(dict(field, name=name))
                    elif config['declared_type']:
                        props.append({'name':name,'type':config['declared_type'],'line':model['line'],'attributes':[],'owner':model})
                    else: self.gap(model['path'],model['line'],'Configured property has no resolved declaration or explicit type')
        for prop in props:
            if mapping and prop['name'] in mapping['ignore'] | mapping['navigation']: continue
            config = mapping['properties'].get(prop['name'], {}) if mapping else {}

            origin = prop['owner']
            self._active_position = {'path':origin['path'],'line':prop['line'],'column':prop.get('column',1)}
            attributes=prop['attributes']
            ignored={'sql':'NotMapped','mongo':'BsonIgnore','dynamo':'DynamoDBIgnore','query':'__none__'}[dialect]
            if attr(attributes,ignored) is not None: continue
            name=prop['name']
            rename={'sql':'Column','mongo':'BsonElement','dynamo':'DynamoDBProperty','query':'__none__'}[dialect]
            if dialect=='mongo' and (name in {'Id','id','_id'} or attr(attributes,'BsonId') is not None): name='_id'
            keys=[rename] + (['DynamoDBHashKey','DynamoDBRangeKey'] if dialect=='dynamo' else [])
            for key in keys:
                args=attr(attributes,key)
                if args is not None and args:
                    mapped=literal_arg(args)
                    if mapped: name=mapped
                    else: self.gap(model['path'],prop['line'],'Nonliteral or configured property mapping requires investigation')
            known=VALIDATION_ATTRIBUTES | SERIALIZATION_ATTRIBUTES | {ignored,rename,'Key','Required','MaxLength','StringLength','BsonId','BsonIgnoreIfNull','BsonIgnoreIfDefault','DynamoDBHashKey','DynamoDBRangeKey','DynamoDBVersion'}
            if any(key.split('.')[-1] not in known for key,_ in attributes):
                self.gap(model['path'],prop['line'],'Unsupported member attribute may change stored shape; investigate serializer/converter semantics')
            if config.get('HasColumnName'): name = config['HasColumnName']
            shape=prop['type']; base=shape.rstrip('?'); optional=shape.endswith('?')
            if mapping and prop['name'] in mapping['owned']:
                child = self.resolve(base, origin)
                if child and attr(child['attributes'],'Table') is not None:
                    self.gap(child['path'],child['line'],'Owned type declares a separate table; table splitting needs an explicit adapter',code='DOTNET_OWNED_TABLE')
                    continue
                mapping.setdefault('entity_relationships',[]).append({'source_model':model['qualified'],'target_model':child['qualified'] if child else None,'navigation':prop['name'],'kind':'owned','refs':mapping['refs'],'status':'modeled' if child else 'unresolved'})
                nested = self.fields(child, dialect, (*seen,model['qualified'])) if child else []
                for field in nested:
                    field['name'] = prop['name'] + '_' + field['name']
                    field['evidence'].append(self.span(origin, prop['line']))
                    for ref in mapping['refs']:
                        path, line = ref.rsplit(':', 1)
                        field['evidence'].append(self.span({'path':path}, int(line)))
                    if any(f['name'] == field['name'] for f in out):
                        self.gap(model['path'],prop['line'],'Owned member conflicts with another stored column; resolve mapping')
                    else: out.append(field)
                continue

            members=[]; enum_evidence=[]
            match=re.fullmatch(r'(?:System\.Collections\.Generic\.)?(?:List|IList|ICollection|IEnumerable|HashSet|IReadOnlyList)<(.+)>|(.+)\[\]',base)
            item=next((x for x in match.groups() if x),None) if match else None
            target=item or base
            simple=target.removeprefix('System.')
            info={'kind':'scalar','name':shape,'optional':optional}
            if simple not in SCALARS:
                if simple in {'object','Object','dynamic','BsonDocument','JsonDocument','JsonElement'} or '<' in simple:
                    info={'kind':'unknown_object','name':shape,'open':True}
                    self.gap(model['path'],prop['line'],'Dynamic or dictionary content has unknown additional members; supply a content contract')
                else:
                    child=self.resolve(target,origin)
                    if child and child['kind'] == 'enum':
                        enum_evidence.append(self.span(child,child['line']))
                        enum_base = values(child['header']).removeprefix(':').strip() or 'int'
                        if enum_base not in {'byte','sbyte','short','ushort','int','uint','long','ulong'}:
                            self.gap(child['path'],child['line'],'Unsupported enum underlying type requires investigation')
                        info={'kind':'scalar','name':shape,'optional':optional}
                    elif dialect in {'sql','query'}:
                        self.gap(model['path'],prop['line'],'Navigation, owned or converted property needs an explicit relational mapping')
                        info={'kind':'unknown_object','name':shape,'open':True}
                    else:
                        members=self.fields(child,dialect,(*seen,model['qualified'])) if child else []
                        info={'kind':'object','name':shape,'open':not bool(members)}
            if config.get('conversion'):
                child = self.resolve(base, origin) if base.removeprefix('System.') not in SCALARS else None
                if child and child['kind'] == 'enum':
                    shape = config['conversion']; info = {'kind':'scalar','name':shape,'optional':optional}
                else: self.gap(model['path'],prop['line'],'String conversion source is not a resolved enum; stored representation requires investigation')
            if item: info={'kind':'array','name':shape,'items':[info], 'open':info.get('open',False)}
            if members: info['members']=[{'name':member['name']} for member in members]
            serialization = [{'attribute':key, 'name':literal_arg(args)} for key,args in attributes if key.split('.')[-1] in SERIALIZATION_ATTRIBUTES]
            field={'name':name,'line':prop['line'],'shape':shape,'type':info,'optional':optional,
                   'evidence':[self.span(origin,prop['line']), *enum_evidence, *config.get('backing_evidence',[])], 'members':members,
                   'clr_type':prop['type'],'storage_type':{'kind':'unknown','reason':'custom_conversion'} if config.get('conversion_unresolved') or config.get('backing_unresolved') else {**info,'database_type':config.get('database_type'),'verification':'modeled'},
                   'serialization':{'status':'attributes_only' if serialization else 'unestablished','attributes':serialization}}
            if prop.get('framework_contract'):
                field['framework_contract'] = prop['framework_contract']
                for ref in prop['framework_contract']['package_refs']:
                    path,line = ref.rsplit(':',1)
                    field['evidence'].append(self.span({'path':path},int(line)))
            if mapping:
                for ref in mapping['refs']:
                    path, line = ref.rsplit(':', 1)
                    field['evidence'].append(self.span({'path':path}, int(line)))
            if any(f['name']==name for f in out): self.gap(model['path'],prop['line'],'Multiple properties map to the same stored name')
            else: out.append(field)
        if not out: self.gap(model['path'],model['line'],'Mapped model has no extracted persisted fields')
        return out

    def bind(self, owner, model, table, store, dialect, line, kind, mapping=None):
        if not model or not RESOURCE.fullmatch(table):
            self.gap(owner['path'],line,'Table/collection name is dynamic or unsupported'); return
        identity=json.dumps([owner.get('binding_scope', owner['project']),dialect,store],sort_keys=True)
        key='dotnet_'+dialect+'_'+hashlib.sha256(identity.encode()).hexdigest()[:16]
        fields=self.fields(model,dialect,mapping=mapping)
        for edge in (mapping or {}).get('entity_relationships',[]):
            record = dict(edge, dataset=key, collection=table, schema_path=model['path'])
            if record not in self.entity_relationships: self.entity_relationships.append(record)
        refs=sorted({f"{owner['path']}:{line}",f"{model['path']}:{model['line']}", *(mapping['refs'] if mapping else [])})
        self.relationships.append({'dataset':key,'collection':table,'model':model['qualified'],'kind':kind,
                                   'context':{'symbol':owner['qualified'],'project':owner['project'],'path':owner['path']} if kind in {'context_entity_table','identity_user_model'} else None,
                                   'schema_path':model['path'],'identity_basis':'modeled_reference','physical_identity':'unverified','refs':refs,
                                   'registrations':mapping.get('registrations',[]) if mapping else []})
        if fields:
            self.observations.append({'path':model['path'],'boundary':key,'relationship_bound':True,'binding_refs':refs,
                                     'kind':'dotnet','role':'canonical','collections':[{'name':table,'refs':refs,
                                     'fields':[{'name':f['name'],'shape':f['shape'],'refs':[f"{e['path']}:{e['start_line']}" for e in f['evidence']]} for f in fields]}]})
            self.tables.append({'name':table,'dataset':key,'schema_path':model['path'],'fields':fields})

    def global_ef_imports(self):
        imports = {}
        for path, source in self.sources.items():
            project = source['project']
            if project not in self.projects: continue
            tokens = source['tokens']; i = 0
            # Global imports belong to the compilation, never to referenced projects.
            while i < len(tokens):
                token = tokens[i]
                if token.kind != 'code': i += 1; continue
                if token.value in {'namespace', 'class', 'record', 'struct', 'interface', 'enum'}: break
                if token.value == '{': i = close(tokens, i) + 1; continue
                if token.value == 'global' and values(tokens[i+1:i+2]) == 'using':
                    end = i + 2
                    while end < len(tokens) and tokens[end].value != ';': end += 1
                    name = ''.join(t.value for t in tokens[i+2:end])
                    if end < len(tokens) and all(t.kind == 'code' for t in tokens[i+2:end]) and name in {
                        'Microsoft.EntityFrameworkCore', 'System.Data.Entity',
                        'global::Microsoft.EntityFrameworkCore', 'global::System.Data.Entity',
                    }:
                        imports.setdefault(project, []).append(f'{path}:{token.line}')
                    i = end + 1
                else: i += 1
        return imports

    def connection_bindings(self, model):
        bindings = []; conditional = False
        for path, source in self.sources.items():
            if source['project'] not in self.projects: continue
            allowed, queue = {source['project']}, [source['project']]
            while queue:
                for ref in self.projects.get(queue.pop(), {}).get('project_references', []):
                    if ref not in allowed: allowed.add(ref); queue.append(ref)
            if model['project'] not in allowed: continue
            tokens = source['tokens']
            for i, token in enumerate(tokens):
                call = registration_call(tokens,i)
                if not call: continue
                typename, args, adapter = call
                if typename not in {model['name'],model['qualified']}: continue
                if i>=2 and tokens[i-1].value=='.':
                    owner={'path':path,'line':token.line,'project':source['project'],'namespace':''}
                    if self.source_type_candidates(tokens[i-2].value,owner):
                        self.gap(path,token.line,'Registration receiver is source-defined; trace its implementation before using a framework adapter',code='DOTNET_REGISTRATION_BINDING')
                        continue
                candidates = [m for m in self.models if (m['name'] == typename or m['qualified'] == typename) and m['project'] in allowed]
                if len(candidates) != 1:
                    self.gap(path, token.line, 'Context registration is ambiguous across referenced projects'); continue
                conditions = runtime_conditions(tokens,i)
                if conditions:
                    conditional = True
                    self.gap(path,token.line,'Runtime-conditional context registration requires branch-specific binding evidence',code='DOTNET_RUNTIME_BRANCH')
                key = None; provider = None
                if adapter['connection'] == 'literal':
                    key = literal_arg(split(args)[0]); provider = adapter['provider']
                else:
                    providers = {t.value for t in args if t.kind == 'code'} & {'UseSqlServer','UseNpgsql'}
                    if len(providers) == 1:
                        provider = 'postgresql' if 'UseNpgsql' in providers else 'sql_server'
                        keys = [literal_arg(args[j+2:j+3]) for j,t in enumerate(args)
                                if t.value == 'GetConnectionString' and values(args[j+1:j+2]) == '(' and values(args[j+3:j+4]) == ')']
                        if len(keys) == 1: key = keys[0]
                if key and provider:
                    refs = [f'{path}:{token.line}']; scope = source['project']
                    host = self.aspire_bindings.get((scope, key), [])
                    if len(host) == 1 and provider == 'postgresql':
                        scope, resource, host_refs = host[0]; refs += host_refs
                        identity = ['aspire_database', resource]
                    else:
                        if len(host) > 1: self.gap(path, token.line, 'Multiple Aspire resources match this connection; logical datastore is ambiguous')
                        identity = ['connection_key', provider, key]
                    bindings.append((scope, identity, refs))
                    trace = {'adapter_id':adapter['adapter_id'],'runtime_project':source['project'],'client':model['qualified'],'logical_connection':{'provider':provider,'key':key},'conditions':conditions,'refs':refs,'physical_identity':'unverified','binding_scope':scope,'status':'conditional' if conditions else 'candidate','datastore':None}
                    if trace not in self.runtime_bindings: self.runtime_bindings.append(trace)
                else:
                    self.runtime_bindings.append({'adapter_id':adapter['adapter_id'],'runtime_project':source['project'],'client':model['qualified'],'logical_connection':{'provider':provider,'key':None},'conditions':conditions,'refs':[f'{path}:{token.line}'],'physical_identity':'unverified','binding_scope':source['project'],'status':'unresolved','datastore':None})
                    self.gap(path, token.line, 'Context connection registration is dynamic or unsupported; supply a logical connection binding')
        unique = {(scope, json.dumps(identity)) for scope,identity,_ in bindings}
        if len(unique) == 1 and not conditional:
            scope, identity, _ = bindings[0]
            return scope, identity, sorted({ref for _,_,refs in bindings for ref in refs})
        self.gap(model['path'],model['line'],'DbContext connection target is unresolved or ambiguous; confirm whether contexts share a physical datastore')
        return model['project'], ['context', model['qualified'], model['path']], []

    def aspire_connections(self):
        result = {}
        for path, source in self.sources.items():
            tokens = source['tokens']; resources = {}; databases = {}; statements = []; start = 0
            # Only straight-line top-level declarations establish host wiring.
            i = 0
            while i < len(tokens):
                if tokens[i].kind == 'code' and tokens[i].value == '{':
                    i = close(tokens, i) + 1; start = i; continue
                if tokens[i].kind == 'code' and tokens[i].value == ';':
                    statements.append(tokens[start:i]); start = i + 1
                i += 1
            assignments = {}
            for statement in statements:
                for i,t in enumerate(statement):
                    if t.value == '=' and i: assignments[statement[i-1].value] = assignments.get(statement[i-1].value, 0) + 1
            for statement in statements:
                if len(statement) < 8 or values(statement[:1]) != 'var' or values(statement[2:3]) != '=': continue
                variable = statement[1].value
                if assignments.get(variable) != 1:
                    self.gap(path, statement[1].line, 'Aspire resource variable is reassigned; host wiring requires investigation')
                    continue
                receiver = statement[3].value; method = statement[5].value
                if values(statement[4:5]) != '.' or values(statement[6:7]) != '(': continue
                try: end = close(statement, 6, '(', ')')
                except ValueError: continue
                args = split(statement[7:end]); name = literal_arg(args[0])
                ref = f'{path}:{statement[5].line}'
                if method == 'AddPostgres' and name: resources[variable] = (name, ref)
                if method == 'AddDatabase' and receiver in resources and name and len(args) == 1:
                    server, server_ref = resources[receiver]
                    databases[variable] = (name, [server,name], [server_ref,ref])
                if method != 'AddDotnetProject' or len(args) < 2: continue
                target = args[1]
                if len(target) != 1 or target[0].kind != 'string': continue
                directory = (self.repo / pathlib.PurePosixPath(path).parent / target[0].value).resolve()
                if not directory.is_relative_to(self.repo): continue
                projects = [p for p in self.projects if (self.repo/p).parent == directory or self.repo/p == directory]
                if len(projects) != 1: continue
                for j,t in enumerate(statement[end+1:], end+1):
                    if t.value != 'WithReference' or values(statement[j+1:j+2]) != '(' or values(statement[j+3:j+4]) != ')': continue
                    resource = databases.get(statement[j+2].value)
                    if resource:
                        key, identity, refs = resource
                        result.setdefault((projects[0], key), []).append((path, identity, refs+[ref,f'{path}:{t.line}']))
        return result

    def runtime_entrypoints(self):
        entries = []
        factories = {'WebApplication': 'CreateBuilder', 'Host': 'CreateApplicationBuilder',
                     'DistributedApplication': 'CreateBuilder', 'MauiApp': 'CreateBuilder'}
        for path in self.files:
            if not path.endswith('.cs') or path in self.excluded: continue
            project = self.project(path)
            if project not in self.projects: continue
            text = self.read(path)
            if text is None: continue
            try: tokens = lex(text, allow_preprocessor=True)
            except ValueError: continue
            code = values([t for t in tokens if t.kind == 'code'])
            for i, token in enumerate(tokens):
                if token.kind != 'code' or token.value not in factories: continue
                factory = factories[token.value]
                if values(tokens[i+1:i+4]) != '. '+factory+' (': continue
                owner = {'path':path, 'line':token.line, 'project':project, 'namespace':''}
                if self.source_type_candidates(token.value, owner) or re.search(r'\b(?:class|struct) '+token.value+r'\b', code): continue
                terminal = r'\. UseMauiApp <' if token.value == 'MauiApp' else r'\. Run(?:Async)? \('
                if not re.search(terminal, code): continue
                entries.append({'root': '' if pathlib.PurePosixPath(project).parent == pathlib.PurePosixPath('.') else str(pathlib.PurePosixPath(project).parent),
                                'ref': f'{path}:{token.line}', 'kind':'dotnet_entrypoint'})
                break
        return entries

    def relationships_from_source(self):
        global_imports = self.global_ef_imports()
        self.aspire_bindings = self.aspire_connections()
        for model in self.models:
            src=self.sources[model['path']]; code=values(src['tokens']); header=values(model['header'])
            if model['project'] is None: continue
            import_refs = global_imports.get(model['project'], [])
            local_ef = 'EntityFrameworkCore' in code or 'System . Data . Entity' in code
            global_ef = bool(import_refs) and bool(re.search(r'(?<!:):\s*DbContext\b', header))
            identity_context = re.search(r':\s*IdentityDbContext\s*<\s*([\w.]+)\s*>', header)
            context = self.context_kind(model)
            if context:
                scope, store, connection_refs = self.connection_bindings(model)
                owner = dict(model, binding_scope=scope)
                for trace in self.runtime_bindings:
                    if trace['client']==model['qualified'] and trace['binding_scope']==scope:
                        trace['datastore'] = 'dotnet_sql_'+hashlib.sha256(json.dumps([scope,'sql',store],sort_keys=True).encode()).hexdigest()[:16]
                        trace['status'] = 'conditional' if trace['conditions'] else ('unresolved' if store[0]=='context' else 'modeled')
                if identity_context:
                    target = self.resolve(identity_context[1], model)
                    self.gap(model['path'],model['line'],'Identity framework table names and inherited framework fields require source or schema evidence; custom user fields are provisional')
                    if target:
                        before = len(self.observations)
                        defaults = identity_default_models(self, model)
                        self.bind(owner,target,'AspNetUsers' if defaults else target['name'],store,'sql',model['line'],'identity_user_model')
                        if len(self.observations) > before:
                            self.observations[-1]['binding_refs'] += import_refs + connection_refs
                            self.relationships[-1]['refs'] = sorted(set(self.relationships[-1]['refs'] + import_refs + connection_refs))
                        for table, framework_model in defaults:
                            self.bind(owner,framework_model,table,store,'sql',model['line'],'identity_user_model')
                            self.observations[-1]['binding_refs'] += import_refs + connection_refs
                            self.relationships[-1]['refs'] = sorted(set(self.relationships[-1]['refs'] + import_refs + connection_refs))
                discovered = self.discover_entities(model)
                for entry in discovered['entities'].values():
                    target = entry['target']
                    prop = {'name':entry['table'],'line':model['line']}
                    table=prop['name']; annotation=attr(target['attributes'],'Table')
                    if annotation is not None:
                        parts=split(annotation); table=literal_arg(parts[0])
                        if table is None:
                            self.gap(target['path'],target['line'],'Table name is not a supported literal; resolve mapping before collection')
                            continue
                        if len(parts)>1:
                            if len(parts)==2 and values(parts[1][:2])=='Schema =' and literal_arg(parts[1][2:]): table=literal_arg(parts[1][2:])+'.'+table
                            else: self.gap(target['path'],target['line'],'Unsupported table attribute arguments')
                    if attr(target['attributes'],'NotMapped') is not None:
                        if any(r['kind']!='dbset' for r in entry['registrations']): self.gap(target['path'],target['line'],'Explicit entity registration conflicts with NotMapped; resolve precedence',code='DOTNET_MEMBER_PRECEDENCE')
                        continue
                    mapping = self.fluent_mapping(model, target)
                    if mapping['table']: table = mapping['table']
                    if mapping['schema'] and '.' not in table: table = mapping['schema'] + '.' + table
                    before=len(self.observations)
                    self.bind(owner,target,table,store,'sql',prop['line'],'context_entity_table',mapping=mapping)
                    if len(self.observations)>before:
                        self.observations[-1]['binding_refs']+= import_refs + connection_refs
                        self.relationships[-1]['refs'] = sorted(set(self.relationships[-1]['refs'] + import_refs + connection_refs))
                if any(t.value == 'OnModelCreatingPartial' for t in model['body']):
                    self.gap(model['path'],model['line'],'Fluent model configuration may override conventions/attributes; investigate mappings before accepting')
            table=attr(model['attributes'],'DynamoDBTable')
            if table is not None:
                if 'DynamoDBv2' in code:
                    name=literal_arg(table)
                    if name: self.bind(model,model,name,['dynamo_model',model['qualified'],model['path'],name],'dynamo',model['line'],'table_model')
                    else: self.gap(model['path'],model['line'],'DynamoDB table attribute is not a supported literal')
                else: self.gap(model['path'],model['line'],'DynamoDB attribute namespace is not established')
        # Local database/collection chains are scoped to each method/block, not receiver spelling globally.
        for path,src in self.sources.items():
            if src['project'] is None: continue
            tokens=src['tokens']; stack=[0]; declarations={}; code=values(tokens)
            assignments = {}
            for index, token in enumerate(tokens):
                if token.kind != 'code': continue
                if token.value == '{': stack.append(index)
                elif token.value == '}' and len(stack) > 1: stack.pop()
                elif token.value == '=' and index and IDENT.fullmatch(tokens[index-1].value):
                    key = (tuple(stack), tokens[index-1].value)
                    assignments[key] = assignments.get(key, 0) + 1
            stack = [0]
            mongo='MongoDB' in code
            for i,t in enumerate(tokens):
                if t.kind!='code': continue
                if t.value=='{': stack.append(i)
                if t.value=='}' and len(stack)>1: stack.pop()
                scope=tuple(stack)
                if mongo and t.value=='GetDatabase' and i>=4 and tokens[i-1].value=='.' and i+3<len(tokens):
                    # var db = client.GetDatabase("name"); all three values must be local and literal.
                    start=i-3
                    if tokens[start].value=='=' and tokens[i+2].kind=='string' and tokens[i+3].value==')':
                        variable=tokens[start-1].value; database=tokens[i+2].value
                        if RESOURCE.fullmatch(database): declarations.setdefault((scope,variable),[]).append((tokens[i-2].value,database,t.line))
                if mongo and t.value=='GetCollection' and i>=2 and tokens[i+1:i+2] and tokens[i+1].value=='<':
                    end=next((j for j in range(i+2,len(tokens)) if tokens[j].value=='>'),None)
                    if end is None or end+3>=len(tokens): continue
                    typename=''.join(x.value for x in tokens[i+2:end]); receiver=tokens[i-2].value
                    if tokens[end+1].value!='(' or tokens[end+2].kind!='string' or tokens[end+3].value!=')':
                        self.gap(path,t.line,'MongoDB collection name is dynamic'); continue
                    bindings=declarations.get((scope,receiver),[])
                    owner={'path':path,'line':t.line,'project':src['project'],'namespace':''}
                    target=self.resolve(typename,owner)
                    if len(bindings)!=1 or assignments.get((scope,receiver),0)!=1:
                        self.gap(path,t.line,'MongoDB database receiver is unresolved or reassigned; investigate database identity'); continue
                    client,database,dbline=bindings[0]
                    self.bind(owner,target,tokens[end+2].value,[path,list(scope),client,database],'mongo',t.line,'database_collection_model')
            signals={'entity_framework':{'DbContext','DbSet','IEntityTypeConfiguration'},'sql_server':{'SqlConnection','SqlCommand'},
                     'dapper':{'Query','QueryAsync','Execute','ExecuteAsync'},'mongodb':{'MongoClient','IMongoCollection','GetCollection'},
                     'dynamodb':{'DynamoDBContext','AmazonDynamoDBClient','DynamoDBTable'},'rabbitmq':{'BasicPublish','BasicPublishAsync','BasicConsume','BasicConsumeAsync'}}
            words={t.value for t in tokens if t.kind=='code'}
            for tech,markers in signals.items():
                hits=markers & words
                if tech=='dapper' and 'Dapper' not in words: continue
                if hits:
                    hit=next(t for t in tokens if t.value in hits)
                    self.technologies.append({'technology':tech,'evidence_kind':'source_signal','ref':f'{path}:{hit.line}'})
                    if tech in {'sql_server','dapper'}:
                        self.sql_queries(path,src)
                    if tech in {'mongodb','dynamodb','entity_framework'} and not any(path==o['path'] or any(ref.startswith(path+':') for ref in o['binding_refs']) for o in self.observations):
                        self.gap(path,hit.line,'Persistence usage has no extracted binding; inspect its models, serializer and resource configuration')
            inspected = [m for m in self.models if m['path'] == path]
            fully_inspected = bool(inspected) and all((path,m['qualified']) in self.inspected_configurations for m in inspected)
            if words & {'BsonClassMap','ConventionRegistry','TableNamePrefix','TableNameOverride','RegisterTableDefinition'} or (not fully_inspected and words & {'IEntityTypeConfiguration','HasConversion','ToTable'}):
                self.gap(path,1,'Custom mapping, convention or table override requires investigation; declared model names may differ from runtime storage')

    def sql_queries(self,path,src):
        tokens=src['tokens']; found=False
        for i,t in enumerate(tokens):
            if t.kind!='code' or t.value not in {'Query','QueryAsync'} or i<2 or i+2>=len(tokens) or tokens[i+1].value!='<': continue
            end=next((j for j in range(i+2,len(tokens)) if tokens[j].value=='>'),None)
            if end is None or end+2>=len(tokens) or tokens[end+1].value!='(' or tokens[end+2].kind!='string': continue
            sql=tokens[end+2].value
            match=re.fullmatch(r'\s*SELECT\s+([\w\[\],\s]+)\s+FROM\s+([\w.\[\]]+)\s*;?\s*',sql,re.I)
            if not match: continue
            columns=[x.strip().strip('[]') for x in match[1].split(',')]; table=match[2].replace('[','').replace(']','')
            owner={'path':path,'line':t.line,'project':src['project'],'namespace':''}
            target=self.resolve(''.join(x.value for x in tokens[i+2:end]),owner)
            if not target or not all(IDENT.fullmatch(c) for c in columns): continue
            props=self.properties(target)
            if {p['name'] for p in props}!={*columns}:
                self.gap(path,t.line,'SQL projection and typed result differ; investigate column/member mapping'); continue
            self.bind(owner,target,table,[path,'sql_receiver',tokens[i-2].value,t.line],'query',t.line,'literal_query_result')
            found=True
        self.gap(path,1,'SQL client target and query coverage require investigation; literal result mappings do not establish the complete database schema' if found else 'SQL usage has no supported literal typed query mapping; supply current DDL or investigate queries')

    def run(self):
        self.inventory()
        for path in self.files:
            if not path.endswith('.cs'): continue
            text=self.read(path)
            if text is None: continue
            before=len(self.models)
            try: self.parse(path,text)
            except (ValueError,IndexError,RecursionError) as exc:
                del self.models[before:]; self.sources.pop(path,None)
                self.gap(path,getattr(exc,'line',1),'C# extraction limitation: ' + str(exc) + '; inspect declarations before claiming coverage',code='DOTNET_BUILD_MEMBERSHIP' if 'preprocessor' in str(exc) else 'DOTNET_SOURCE_SYNTAX')
        shadowed = {'DbContext','DbSet','TableAttribute','ColumnAttribute','NotMappedAttribute','ModelBuilder','IEntityTypeConfiguration','EntityTypeBuilder','DynamoDBTableAttribute','DynamoDBPropertyAttribute','BsonElementAttribute','BsonIgnoreAttribute'}
        for model in self.models:
            if model['name'] in shadowed:
                self.gap(model['path'],model['line'],'Local type shadows a recognized framework symbol; investigate symbol identity')
        self.relationships_from_source()
        for model in self.models:
            if attr(model['attributes'],'Table') is not None and not any(o['path']==model['path'] for o in self.observations):
                self.gap(model['path'],model['line'],'Table-attributed model has no resolved datastore binding')
        self.tables = list({json.dumps(table, sort_keys=True): table for table in self.tables}.values())
        accounted = {('sql', 'entity_framework'), ('sql', 'sql_server'), ('query', 'dapper'), ('query', 'sql_server'), ('mongo', 'mongodb'), ('dynamo', 'dynamodb')}
        mapped = set()
        for edge in self.relationships:
            dialect = edge['dataset'].split('_')[1]
            for path in {ref.rsplit(':',1)[0] for ref in edge['refs']}:
                project = self.project(path)
                for kind, tech in accounted:
                    if dialect == kind: mapped.add((project, tech))
        self.gaps = [gap for gap in self.gaps if not (gap['reason'].startswith('Persistence dependency declared')
                     and any((gap['ref'].rsplit(':',1)[0], tech['technology']) in mapped
                             for tech in self.technologies if tech['ref'] == gap['ref']))]
        for path, bindings in self.reviewed_bindings.items():
            if path.endswith('.cs') and len(bindings)>1:
                self.gap(path,1,'Multiple reviewed bindings for one C# model need per-relationship investigation.',code='DOTNET_REVIEWED_BINDING')
        from dotnet_boundaries import discover as discover_boundaries
        boundaries = discover_boundaries(self)
        runtime_entrypoints = self.runtime_entrypoints()
        self.gaps = finalize_diagnostics(self)
        return {**boundaries, 'runtime_entrypoints':runtime_entrypoints, 'type_symbols':[{'name':m['name'],'qualified':m['qualified'],'project':m['project'],'path':m['path']} for m in self.models], 'diagnostic_summary':diagnostic_summary(self.gaps),'runtime_bindings':self.runtime_bindings,'entity_relationships':self.entity_relationships,'solutions':self.solutions,'projects':list(self.projects.values()),'technologies':sorted(self.technologies,key=lambda r:(r['ref'],r['technology'])),
                'relationships':self.relationships,'observations':self.observations,'tables':self.tables,
                'gaps':sorted(self.gaps,key=lambda r:(r['ref'],r['reason']))}


def discover(repo,files,excluded=(),reviewed_bindings=None):
    return Reader(repo,files,excluded,reviewed_bindings).run()


if __name__=='__main__':
    request = json.load(sys.stdin)
    print(json.dumps(discover(sys.argv[1], request['files'], request.get('excluded', []), request.get('reviewed_bindings')), sort_keys=True))
