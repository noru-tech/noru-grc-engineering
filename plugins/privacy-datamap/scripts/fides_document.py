"""Strict, dependency-free reader for JSON and the collector's block YAML export.

Unsupported YAML features fail closed; this is not a general YAML implementation.
"""
import json
import re


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError('Document keys must be unique strings')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('Nonfinite JSON number')


def scalar(value):
    if value.startswith('"') or value in ('[]', '{}', 'true', 'false', 'null'):
        return json.loads(value, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    if value.startswith("'"):
        if not re.fullmatch(r"'(?:[^']|'')*'", value):
            raise ValueError('Invalid quoted string')
        return value[1:-1].replace("''", "'")
    if re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', value):
        return json.loads(value)
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9 _./@-]*', value):
        raise ValueError('Unsupported YAML scalar; use the generated export or JSON')
    if value.lower() in ('yes', 'no', 'on', 'off', 'true', 'false', 'null'):
        raise ValueError('Quote ambiguous YAML strings')
    return value


def load_document(text):
    if text.lstrip().startswith(('{', '[')):
        return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    lines = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if '\t' in line[:len(line) - len(line.lstrip())]:
            raise ValueError('Tabs are not supported for indentation')
        lines.append((len(line) - len(line.lstrip(' ')), line.strip()))
    if not lines or lines[0][0] != 0:
        raise ValueError('Expected a document starting at column one')

    def node(index, indent):
        sequence = lines[index][1] == '-' or lines[index][1].startswith('- ')
        values, pairs = [], []
        while index < len(lines) and lines[index][0] == indent:
            content = lines[index][1]
            is_item = content == '-' or content.startswith('- ')
            if is_item != sequence:
                break
            if sequence:
                rest = content[1:].strip()
                if re.match(r'[A-Za-z_][A-Za-z0-9_]*:(?: |$)', rest):
                    # Expand the inline first map key into an ordinary indented map.
                    lines[index] = (indent + 2, rest)
                    value, index = node(index, indent + 2)
                else:
                    index += 1
                    if rest:
                        value = scalar(rest)
                    elif index < len(lines) and lines[index][0] > indent:
                        value, index = node(index, lines[index][0])
                    else:
                        value = None
                values.append(value)
            else:
                match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*):(?: (.*))?', content)
                if not match:
                    raise ValueError('Unsupported YAML mapping')
                key, rest = match.groups()
                index += 1
                if rest:
                    value = scalar(rest)
                elif index < len(lines) and lines[index][0] > indent:
                    value, index = node(index, lines[index][0])
                else:
                    value = None
                pairs.append((key, value))
        return (values if sequence else unique_pairs(pairs)), index

    result, end = node(0, 0)
    if end != len(lines):
        raise ValueError('Unsupported YAML structure or trailing content')
    json.dumps(result, allow_nan=False)
    return result
