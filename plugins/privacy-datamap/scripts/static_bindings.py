"""Read literal frozen-dataclass store declarations without importing target code."""
import ast
import json
import pathlib
import re
import sys


def discover(repo, paths):
    repo = pathlib.Path(repo).resolve()
    allowed = set(paths)
    bindings = {}
    for path in sorted(allowed):
        if not path.endswith('.py'):
            continue
        source = repo / path
        if not source.resolve().is_relative_to(repo) or source.is_symlink():
            continue
        try:
            if source.stat().st_size > 1_000_000:
                continue
            tree = ast.parse(source.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError):
            continue
        decorators = {alias.asname or alias.name for node in tree.body
                      if isinstance(node, ast.ImportFrom) and node.module == 'dataclasses' and node.level == 0
                      for alias in node.names if alias.name == 'dataclass'}
        assignments = {}
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            for target in targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(node)
        constructors = {}
        shadowed = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        shadowed.update(alias.asname or alias.name.split('.')[0] for node in tree.body
                        if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
                        if not (isinstance(node, ast.ImportFrom) and node.module == 'dataclasses'))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.bases or node.keywords or node.name in assignments or node.name in shadowed:
                continue
            if sum(isinstance(other, ast.ClassDef) and other.name == node.name for other in tree.body) != 1:
                continue
            if len(node.decorator_list) != 1:
                continue
            decorator = node.decorator_list[0]
            if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)
                    and decorator.func.id in decorators and decorator.func.id not in assignments and decorator.func.id not in shadowed
                    and not decorator.args and len(decorator.keywords) == 1
                    and decorator.keywords[0].arg == 'frozen'
                    and isinstance(decorator.keywords[0].value, ast.Constant)
                    and decorator.keywords[0].value.value is True):
                continue
            if any(isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name in ('__init__', '__post_init__', '__new__') for member in node.body):
                continue
            fields = [member.target.id for member in node.body if isinstance(member, ast.AnnAssign)
                      and isinstance(member.target, ast.Name) and member.value is None
                      and isinstance(member.annotation, ast.Name) and member.annotation.id == 'str']
            if (set(fields) == {'destination', 'schema_path'} and len(fields) == 2
                    and sum(isinstance(member, ast.AnnAssign) for member in node.body) == 2
                    and not any(isinstance(member, ast.Assign) for member in node.body)):
                constructors[node.name] = (fields, node.lineno)
        for name, nodes in assignments.items():
            if len(nodes) != 1:
                continue
            node = nodes[0]
            if node not in tree.body:
                continue
            call = node.value
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id not in constructors:
                continue
            fields, class_line = constructors[call.func.id]
            if node.lineno < class_line:
                continue
            if len(call.args) > 2 or any(not isinstance(value, ast.Constant) or not isinstance(value.value, str) for value in call.args):
                continue
            values = dict(zip(fields, [arg.value for arg in call.args]))
            valid = True
            for keyword in call.keywords:
                if keyword.arg not in fields or keyword.arg in values or not isinstance(keyword.value, ast.Constant) or not isinstance(keyword.value.value, str):
                    valid = False
                    break
                values[keyword.arg] = keyword.value.value
            if not valid or set(values) != set(fields):
                continue
            schema, destination = values['schema_path'], values['destination']
            if schema not in allowed or not re.fullmatch(r'[a-z][a-z0-9_]*', destination):
                continue
            target = repo / schema
            if target.is_symlink() or not target.resolve().is_relative_to(repo):
                continue
            bindings.setdefault(schema, []).append({'key': destination, 'refs': [f'{path}:{class_line}', f'{path}:{node.lineno}'], 'client': name})
    return bindings


if __name__ == '__main__':
    print(json.dumps(discover(sys.argv[1], json.load(sys.stdin)), sort_keys=True))
