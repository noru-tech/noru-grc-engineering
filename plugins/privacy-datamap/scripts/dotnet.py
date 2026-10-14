"""Bounded offline .NET declarations and modeled storage bindings; never evaluates MSBuild."""
import hashlib
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass

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


@dataclass
class Token:
    value: str
    line: int
    kind: str = "code"


def string_end(text, start, depth=0):
    """Skip literals and interpolation holes without treating their contents as declarations."""
    if depth > 64: raise ValueError('String nesting exceeds inspection limit')
    match = re.match(r'(\$+)?(@)?("{3,}|"|\')|(@\$)(")', text[start:])
    if not match: return None
    prefix = match.group(); interpolated = '$' in prefix; verbatim = '@' in prefix
    quote = "'" if prefix.endswith("'") else '"'
    width = len(prefix) - len(prefix.rstrip('"')) if quote == '"' else 1
    raw = width >= 3; dollars = prefix.count('$'); pos = start + len(prefix)
    while pos < len(text):
        if raw and text.startswith('"' * width, pos): return pos + width
        if not raw and text[pos] == quote:
            if verbatim and text.startswith('""', pos): pos += 2; continue
            return pos + 1
        if not raw and not verbatim and text[pos] == '\\': pos += 2; continue
        if interpolated and text[pos] == '{':
            if not raw and text.startswith('{{', pos): pos += 2; continue
            count = len(text[pos:]) - len(text[pos:].lstrip('{'))
            opening = dollars if raw else 1
            if count < opening: pos += count; continue
            pos += count; nesting = 1
            while pos < len(text):
                if text.startswith('//', pos):
                    end = text.find('\n', pos); pos = len(text) if end < 0 else end; continue
                if text.startswith('/*', pos):
                    end = text.find('*/', pos + 2)
                    if end < 0: raise ValueError('Unterminated interpolation comment')
                    pos = end + 2; continue
                nested = string_end(text, pos, depth + 1)
                if nested is not None: pos = nested; continue
                if text[pos] == '{': nesting += 1
                if text[pos] == '}':
                    nesting -= 1
                    if nesting == 0:
                        if not text.startswith('}' * opening, pos): raise ValueError('Unbalanced interpolation')
                        pos += opening; break
                pos += 1
            else: raise ValueError('Unterminated interpolation')
            continue
        pos += 1
    raise ValueError('Unterminated string')


def lex(text):
    tokens, pos, line = [], 0, 1
    while pos < len(text):
        ch = text[pos]
        if ch.isspace():
            line += ch == '\n'; pos += 1; continue
        if text.startswith('//', pos):
            end = text.find('\n', pos); pos = len(text) if end < 0 else end; continue
        if text.startswith('/*', pos):
            end = text.find('*/', pos + 2)
            if end < 0: raise ValueError('Unterminated comment')
            line += text[pos:end + 2].count('\n'); pos = end + 2; continue
        if ch == '#' and not re.match(r'#(?:nullable\b|pragma\s+warning\b|region\b|endregion\b)', text[pos:]):
            raise ValueError('Conditional or preprocessor source requires investigation')
        if ch == '#':
            end = text.find('\n', pos); pos = len(text) if end < 0 else end; continue
        if ch == '$' or text.startswith('@$', pos) or text.startswith('"""', pos):
            end = string_end(text, pos)
            if end is None: raise ValueError('Unsupported string prefix')
            tokens.append(Token('', line, 'computed_string'))
            line += text[pos:end].count('\n'); pos = end; continue
        start = line
        verbatim = text.startswith('@"', pos)
        if ch in '\"\'' or verbatim:
            quote = '"' if verbatim else ch
            pos += 2 if verbatim else 1
            value = ''
            while pos < len(text):
                if text[pos] == quote:
                    if verbatim and text.startswith('""', pos): value += '"'; pos += 2; continue
                    pos += 1; break
                if not verbatim and text[pos] == '\\':
                    if pos + 1 >= len(text): raise ValueError('Unterminated string')
                    escape = text[pos + 1]
                    value += {'n': '\n', 'r': '\r', 't': '\t', '"': '"', "'": "'", '\\': '\\'}.get(escape, '\\' + escape)
                    pos += 2; continue
                line += text[pos] == '\n'; value += text[pos]; pos += 1
            else: raise ValueError('Unterminated string')
            tokens.append(Token(value, start, 'string' if quote == '"' else 'char')); continue
        match = LEX.match(text, pos)
        value = match.group(); tokens.append(Token(value.lstrip('@'), line)); pos += len(value)
    return tokens


def values(tokens):
    return ' '.join(t.value if t.kind == 'code' else '<literal>' for t in tokens)


def close(tokens, start, left='{', right='}'):
    depth = 0
    for i in range(start, len(tokens)):
        if tokens[i].kind != 'code': continue
        if tokens[i].value == left: depth += 1
        if tokens[i].value == right:
            depth -= 1
            if depth == 0: return i
    raise ValueError('Unbalanced declaration')


def split(tokens, separator=','):
    parts, start, stack = [], 0, []
    for i, token in enumerate(tokens):
        if token.kind != 'code': continue
        if token.value in ('(', '[', '<', '{'): stack.append(token.value)
        elif token.value in (')', ']', '>', '}') and stack: stack.pop()
        elif token.value == separator and not stack: parts.append(tokens[start:i]); start = i + 1
    return parts + [tokens[start:]]


def attrs(tokens, start):
    result, i = [], start
    while i < len(tokens) and tokens[i].value == '[':
        end = close(tokens, i, '[', ']')
        for part in split(tokens[i+1:end]):
            if len(part) > 1 and part[1].value == ':': part = part[2:]
            paren = next((j for j, t in enumerate(part) if t.value == '('), len(part))
            name = ''.join(t.value for t in part[:paren])
            result.append((name.removesuffix('Attribute'), part[paren+1:-1] if paren < len(part) else []))
        i = end + 1
    return result, i


def attr(attributes, name):
    return next((args for key, args in attributes if key.split('.')[-1] == name), None)


def literal_arg(args):
    if args and len(args) == 1 and args[0].kind == 'string' and RESOURCE.fullmatch(args[0].value):
        return args[0].value
    return None


class Reader:
    def __init__(self, repo, files, excluded=()):
        self.repo = pathlib.Path(repo).resolve()
        self.files = sorted(set(files))
        self.excluded = set(excluded)
        self.projects, self.sources, self.models = {}, {}, []
        self.gaps, self.technologies, self.observations, self.relationships = [], [], [], []
        self.tables = []
        self.solutions = []

    def gap(self, path, line, reason):
        row = {'format': 'dotnet', 'ref': f'{path}:{line}', 'reason': reason}
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
            project = {'path': path, 'refs': [f'{path}:1'], 'project_references': []}
            for node in root.iter():
                tag = node.tag.split('}')[-1]
                if 'Condition' in node.attrib or tag in {'Import', 'Choose', 'Target'} or '$(' in ''.join(node.attrib.values()):
                    self.gap(path, 1, 'Conditional/imported MSBuild settings are not evaluated; confirm source membership and package configuration')
                if tag == 'Compile':
                    self.gap(path, 1, 'Explicit Compile items require source-membership investigation')
                if tag == 'EnableDefaultCompileItems' and (node.text or '').strip().lower() == 'false':
                    self.gap(path, 1, 'Default C# source inclusion is disabled; confirm included sources')
                if tag == 'ProjectReference':
                    target = node.get('Include', '').replace('\\', '/')
                    resolved = (self.repo / pathlib.PurePosixPath(path).parent / target).resolve()
                    if target and resolved.is_relative_to(self.repo) and resolved.relative_to(self.repo).as_posix() in self.files:
                        project['project_references'].append(resolved.relative_to(self.repo).as_posix())
                    else: self.gap(path, 1, 'Project reference is external, dynamic, or missing from the scanned inventory')
                if tag in {'PackageReference', 'PackageVersion'}:
                    package = node.get('Include', node.get('Update', ''))
                    for prefix, technology in PACKAGES.items():
                        if package.lower() == prefix or package.lower().startswith(prefix + '.'):
                            self.technologies.append({'technology': technology, 'evidence_kind': 'dependency', 'ref': f'{path}:1'})
                            break
                    if package.lower().startswith(('microsoft.entityframeworkcore', 'entityframework', 'dapper', 'mongodb', 'awssdk.dynamodb', 'microsoft.data.sqlclient', 'system.data.sqlclient')):
                        self.gap(path, 1, 'Persistence dependency declared; investigate runtime usage and model coverage before accepting an empty or partial map')
            if path.endswith('.csproj'): self.projects[path] = project

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

    def resolve(self, name, owner):
        name = name.replace('global::','').rstrip('?')
        allowed, queue = {owner['project']}, [owner['project']]
        while queue:
            for target in self.projects.get(queue.pop(), {}).get('project_references', []):
                if target not in allowed: allowed.add(target); queue.append(target)
        matches = [m for m in self.models if m['project'] in allowed and m['qualified'] in {name, owner['namespace']+'.'+name}]
        if not matches: matches = [m for m in self.models if m['project'] in allowed and m['name'] == name]
        if len(matches) != 1:
            self.gap(owner['path'],owner['line'],'Referenced model is missing or ambiguous; resolve its namespace/project mapping')
            return None
        return matches[0]

    def span(self, model, line):
        text = self.sources[model['path']]['text']; lines=text.splitlines()
        return {'path':model['path'],'start_line':line,'end_line':line,'start_column':1,
                'end_column':len(lines[line-1])+1,'sha256':hashlib.sha256((self.repo/model['path']).read_bytes()).hexdigest(),
                'extractor':'dotnet-structural@1'}

    def properties(self, model):
        try:
            return self._properties(model)
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
                    props.append({'name':words[-1].value,'type':''.join(t.value for t in words[:-1]),'line':words[-1].line,'attributes':attributes})
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
                if IDENT.fullmatch(words[-1].value) and re.fullmatch(r'get ; (?:(?:private|protected|internal) )?(?:set|init) ;',access):
                    props.append({'name':words[-1].value,'type':''.join(t.value for t in words[:-1]),'line':words[-1].line,'attributes':attributes})
                else: self.gap(model['path'],head[0].line,'Public member is not a supported auto-property; investigate persisted fields, computed accessors or read-only mapping')
            i=end+1
            # Auto-property initializers do not change the declared shape; never emit their values.
            if i<len(body) and body[i].value=='=':
                while i<len(body) and body[i].value!=';':
                    if body[i].value=='{': i=close(body,i)
                    i+=1
                i+=1
        return props

    def fields(self, model, dialect, seen=()):
        if model['qualified'] in seen or len(seen)>=8:
            self.gap(model['path'],model['line'],'Recursive or deep nested model requires shape investigation'); return []
        if 'partial' in model['modifiers'] or any(t.value in {':','<'} for t in model['header']):
            self.gap(model['path'],model['line'],'Partial, inherited or generic model requires investigation beyond declared members')
        out=[]
        for prop in self.properties(model):
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
            known={ignored,rename,'Key','Required','MaxLength','StringLength','BsonId','BsonIgnoreIfNull','BsonIgnoreIfDefault','DynamoDBHashKey','DynamoDBRangeKey','DynamoDBVersion'}
            if any(key.split('.')[-1] not in known for key,_ in attributes):
                self.gap(model['path'],prop['line'],'Unsupported member attribute may change stored shape; investigate serializer/converter semantics')
            shape=prop['type']; base=shape.rstrip('?'); optional=shape.endswith('?')
            members=[]
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
                    child=self.resolve(target,model)
                    if child and child['kind'] == 'enum':
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
            if item: info={'kind':'array','name':shape,'items':[info], 'open':info.get('open',False)}
            if members: info['members']=[{'name':member['name']} for member in members]
            field={'name':name,'line':prop['line'],'shape':shape,'type':info,'optional':optional,
                   'evidence':[self.span(model,prop['line'])], 'members':members}
            if any(f['name']==name for f in out): self.gap(model['path'],prop['line'],'Multiple properties map to the same stored name')
            else: out.append(field)
        if not out: self.gap(model['path'],model['line'],'Mapped model has no extracted persisted fields')
        return out

    def bind(self, owner, model, table, store, dialect, line, kind):
        if not model or not RESOURCE.fullmatch(table):
            self.gap(owner['path'],line,'Table/collection name is dynamic or unsupported'); return
        identity=json.dumps([owner.get('binding_scope', owner['project']),dialect,store],sort_keys=True)
        key='dotnet_'+dialect+'_'+hashlib.sha256(identity.encode()).hexdigest()[:16]
        fields=self.fields(model,dialect)
        refs=sorted({f"{owner['path']}:{line}",f"{model['path']}:{model['line']}"})
        self.relationships.append({'dataset':key,'collection':table,'model':model['qualified'],'kind':kind,
                                   'schema_path':model['path'],'identity_basis':'modeled_reference','physical_identity':'unverified','refs':refs})
        if fields:
            self.observations.append({'path':model['path'],'boundary':key,'relationship_bound':True,'binding_refs':refs,
                                     'kind':'dotnet','role':'canonical','collections':[{'name':table,'refs':refs,
                                     'fields':[{'name':f['name'],'shape':f['shape'],'refs':[f"{model['path']}:{f['line']}"]} for f in fields]}]})
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
        bindings = []
        for path, source in self.sources.items():
            if source['project'] not in self.projects: continue
            allowed, queue = {source['project']}, [source['project']]
            while queue:
                for ref in self.projects.get(queue.pop(), {}).get('project_references', []):
                    if ref not in allowed: allowed.add(ref); queue.append(ref)
            if model['project'] not in allowed: continue
            tokens = source['tokens']
            for i, token in enumerate(tokens):
                if token.kind != 'code' or token.value not in {'AddDbContext','AddDbContextPool','AddNpgsqlDbContext'}: continue
                if values(tokens[i+1:i+4]) != '< '+model['name']+' >': continue
                candidates = [m for m in self.models if m['name'] == model['name'] and m['project'] in allowed]
                if len(candidates) != 1:
                    self.gap(path, token.line, 'Context registration is ambiguous across referenced projects'); continue
                opening = i + 4
                if opening >= len(tokens) or tokens[opening].value != '(': continue
                try: end = close(tokens, opening, '(', ')')
                except ValueError: continue
                args = tokens[opening+1:end]; key = None; provider = None
                if token.value == 'AddNpgsqlDbContext':
                    key = literal_arg(split(args)[0]); provider = 'postgresql'
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
                else: self.gap(path, token.line, 'Context connection registration is dynamic or unsupported; supply a logical connection binding')
        unique = {(scope, json.dumps(identity)) for scope,identity,_ in bindings}
        if len(unique) == 1:
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

    def relationships_from_source(self):
        global_imports = self.global_ef_imports()
        self.aspire_bindings = self.aspire_connections()
        for model in self.models:
            src=self.sources[model['path']]; code=values(src['tokens']); header=values(model['header'])
            if model['project'] is None: continue
            import_refs = global_imports.get(model['project'], [])
            local_ef = 'EntityFrameworkCore' in code or 'System . Data . Entity' in code
            global_ef = bool(import_refs) and bool(re.search(r'(?<!:):\s*DbContext\b', header))
            context = bool(re.search(r'\bDbContext\b', header)) and (local_ef or global_ef)
            if context:
                scope, store, connection_refs = self.connection_bindings(model)
                owner = dict(model, binding_scope=scope)
                for prop in self.properties(model):
                    match=re.fullmatch(r'(?:Microsoft\.EntityFrameworkCore\.)?DbSet<(.+)>',prop['type'])
                    if not match: continue
                    target=self.resolve(match[1],model)
                    if not target: continue
                    table=prop['name']; annotation=attr(target['attributes'],'Table')
                    if annotation is not None:
                        parts=split(annotation); table=literal_arg(parts[0])
                        if table is None:
                            self.gap(target['path'],target['line'],'Table name is not a supported literal; resolve mapping before collection')
                            continue
                        if len(parts)>1:
                            if len(parts)==2 and values(parts[1][:2])=='Schema =' and literal_arg(parts[1][2:]): table=literal_arg(parts[1][2:])+'.'+table
                            else: self.gap(target['path'],target['line'],'Unsupported table attribute arguments')
                    if attr(target['attributes'],'NotMapped') is not None: continue
                    before=len(self.observations)
                    self.bind(owner,target,table,store,'sql',prop['line'],'context_entity_table')
                    if len(self.observations)>before:
                        self.observations[-1]['binding_refs']+= import_refs + connection_refs
                        self.relationships[-1]['refs'] = sorted(set(self.relationships[-1]['refs'] + import_refs + connection_refs))
                if any(t.value in {'OnModelCreating','OnModelCreatingPartial'} for t in model['body']):
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
            if words & {'BsonClassMap','ConventionRegistry','IEntityTypeConfiguration','HasConversion','ToTable','TableNamePrefix','TableNameOverride','RegisterTableDefinition'}:
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
                self.gap(path,1,'C# extraction limitation: ' + str(exc) + '; inspect declarations before claiming coverage')
        shadowed = {'DbContext','DbSet','TableAttribute','ColumnAttribute','NotMappedAttribute','DynamoDBTableAttribute','DynamoDBPropertyAttribute','BsonElementAttribute','BsonIgnoreAttribute'}
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
        return {'solutions':self.solutions,'projects':list(self.projects.values()),'technologies':sorted(self.technologies,key=lambda r:(r['ref'],r['technology'])),
                'relationships':self.relationships,'observations':self.observations,'tables':self.tables,
                'gaps':sorted(self.gaps,key=lambda r:(r['ref'],r['reason']))}


def discover(repo,files,excluded=()):
    return Reader(repo,files,excluded).run()


if __name__=='__main__':
    request = json.load(sys.stdin)
    print(json.dumps(discover(sys.argv[1], request['files'], request.get('excluded', [])), sort_keys=True))
