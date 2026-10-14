"""Bounded C# lexical helpers; no target execution or compiler loading."""
import re
from dataclasses import dataclass

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LEX = re.compile(r'@?[A-Za-z_][A-Za-z0-9_]*|[0-9]+|=>|::|\?\.|[^\s]')
RESOURCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")

@dataclass
class Token:
    value: str
    line: int
    kind: str = "code"
    column: int = 1


class SourceError(ValueError):
    def __init__(self, message, line):
        super().__init__(message)
        self.line = line


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


def lex(text, *, allow_preprocessor=False):
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
        if ch == '#' and not allow_preprocessor and not re.match(r'#(?:nullable\b|pragma\s+warning\b|region\b|endregion\b)', text[pos:]):
            raise SourceError('Conditional or preprocessor source requires investigation',line)
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
        value = match.group(); tokens.append(Token(value.lstrip('@'), line, column=pos-text.rfind('\n',0,pos))); pos += len(value)
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



def namespace_usings(tokens):
    result = []
    def visit(start, end, namespace=''):
        i = start; declarations_seen = False
        while i < end:
            token = tokens[i]
            if token.kind != 'code': i += 1; continue
            if token.value == 'namespace':
                j = i+1
                while j < end and tokens[j].value not in {';', '{'}: j += 1
                if j == end: return
                name = ''.join(t.value for t in tokens[i+1:j])
                name = '.'.join(filter(None,[namespace,name]))
                if tokens[j].value == ';': namespace = name; i = j+1
                else:
                    stop = close(tokens,j); visit(j+1,stop,name); i = stop+1
                continue
            global_using = token.value=='global' and values(tokens[i+1:i+2])=='using'
            if token.value=='using' or global_using:
                j=i+(2 if global_using else 1)
                stop=j
                while stop < end and tokens[stop].value!=';': stop+=1
                name=''.join(t.value for t in tokens[j:stop]).removeprefix('global::')
                if re.fullmatch(r'\w+(?:\.\w+)*',name) and (not global_using or (not namespace and not declarations_seen)):
                    result.append((name,global_using,namespace,token.line))
                i=stop+1; continue
            if token.value in {'class','record','struct','interface','enum'}: declarations_seen=True
            if token.value=='{': i=close(tokens,i)+1
            else: i+=1
    visit(0,len(tokens))
    return result
