#!/usr/bin/env python3
"""A small JSON Schema evaluator, standard library only.

Why not a real one: this repository is dependency-free on purpose (see CONTRIBUTING.md), and CI
must run with nothing but python3 and node. The subset implemented here is the subset the contract
schemas actually use, and scripts/check_repo.py fails if a schema starts using a keyword this
evaluator does not understand — so the schemas can never quietly outgrow the checker.

Supported: type, enum, const, required, properties, additionalProperties (boolean), patternProperties,
propertyNames, items, minItems, maxItems, uniqueItems, minLength, maxLength, pattern, minimum,
maximum, allOf, anyOf, oneOf, not, if/then/else, $ref (local #/$defs/... and #/properties/... only),
$defs.

Ignored (annotations): $schema, $id, title, description, default, examples, deprecated, _comment.

Usage:
    python3 scripts/jsonschema_mini.py <schema.json> <instance.json>
Exit codes: 0 = valid, 1 = invalid, 2 = usage / load error.
"""
import json
import pathlib
import re
import sys

ANNOTATIONS = {
    "$schema", "$id", "title", "description", "default", "examples", "deprecated",
    "_comment", "$comment",
}
SUPPORTED = {
    "type", "enum", "const", "required", "properties", "additionalProperties",
    "patternProperties", "propertyNames", "items", "minItems", "maxItems", "uniqueItems",
    "minLength", "maxLength", "pattern", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "$ref", "$defs",
} | ANNOTATIONS

TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


class UnsupportedKeyword(Exception):
    pass


def unsupported_keywords(schema, path="#"):
    """Walk a schema and report keywords this evaluator does not implement."""
    found = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key not in SUPPORTED:
                found.append(f"{path}.{key}")
            if key in ("properties", "$defs", "patternProperties"):
                if isinstance(value, dict):
                    for name, sub in value.items():
                        found.extend(unsupported_keywords(sub, f"{path}.{key}.{name}"))
            elif key in ("allOf", "anyOf", "oneOf"):
                for i, sub in enumerate(value or []):
                    found.extend(unsupported_keywords(sub, f"{path}.{key}[{i}]"))
            elif key in ("items", "not", "if", "then", "else", "propertyNames",
                         "additionalProperties"):
                if isinstance(value, dict):
                    found.extend(unsupported_keywords(value, f"{path}.{key}"))
    return found


def _matches_type(value, expected):
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        py = TYPE_MAP.get(name)
        if py is None:
            continue
        if name == "integer":
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return True
            continue
        if name == "number" and isinstance(value, bool):
            continue
        if name == "boolean":
            if isinstance(value, bool):
                return True
            continue
        if isinstance(value, py):
            return True
    return False


def _resolve(ref, root):
    if not ref.startswith("#/"):
        raise UnsupportedKeyword(f"only local $ref is supported, got {ref}")
    node = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise UnsupportedKeyword(f"cannot resolve $ref {ref}")
        node = node[part]
    return node


def validate(instance, schema, root=None, path="$", errors=None):
    if errors is None:
        errors = []
    if root is None:
        root = schema
    if schema is True:
        return errors
    if schema is False:
        errors.append((path, "schema is false: nothing is valid here"))
        return errors
    if not isinstance(schema, dict):
        return errors

    if "$ref" in schema:
        validate(instance, _resolve(schema["$ref"], root), root, path, errors)

    if "type" in schema and not _matches_type(instance, schema["type"]):
        errors.append((path, f"expected type {schema['type']}, got {type(instance).__name__}"))
        return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append((path, f"{instance!r} is not one of {schema['enum']}"))
    if "const" in schema and instance != schema["const"]:
        errors.append((path, f"expected {schema['const']!r}, got {instance!r}"))

    if isinstance(instance, str):
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append((path, f"{instance!r} does not match /{schema['pattern']}/"))
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append((path, f"shorter than minLength {schema['minLength']}"))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append((path, f"longer than maxLength {schema['maxLength']}"))

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append((path, f"{instance} is below minimum {schema['minimum']}"))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append((path, f"{instance} is above maximum {schema['maximum']}"))

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append((path, f"needs at least {schema['minItems']} item(s)"))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append((path, f"allows at most {schema['maxItems']} item(s)"))
        if schema.get("uniqueItems") is True:
            seen = []
            for item in instance:
                if item in seen:
                    errors.append((path, f"duplicate item {item!r}"))
                seen.append(item)
        if "items" in schema:
            for i, item in enumerate(instance):
                validate(item, schema["items"], root, f"{path}[{i}]", errors)

    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                errors.append((path, f"missing required property '{name}'"))
        properties = schema.get("properties", {})
        pattern_properties = schema.get("patternProperties", {})
        for name, value in instance.items():
            handled = False
            if name in properties:
                validate(value, properties[name], root, f"{path}.{name}", errors)
                handled = True
            for pattern, sub in pattern_properties.items():
                if re.search(pattern, name):
                    validate(value, sub, root, f"{path}.{name}", errors)
                    handled = True
            if not handled:
                extra = schema.get("additionalProperties")
                if extra is False:
                    errors.append((path, f"unexpected property '{name}'"))
                elif isinstance(extra, dict):
                    validate(value, extra, root, f"{path}.{name}", errors)
            if "propertyNames" in schema:
                validate(name, schema["propertyNames"], root, f"{path}.<key {name}>", errors)

    for i, sub in enumerate(schema.get("allOf", [])):
        validate(instance, sub, root, path, errors)
    if "anyOf" in schema:
        if not any(not validate(instance, sub, root, path, []) for sub in schema["anyOf"]):
            errors.append((path, "does not match any branch of anyOf"))
    if "oneOf" in schema:
        matches = sum(1 for sub in schema["oneOf"] if not validate(instance, sub, root, path, []))
        if matches != 1:
            errors.append((path, f"matches {matches} branches of oneOf, expected exactly 1"))
    if "not" in schema and not validate(instance, schema["not"], root, path, []):
        errors.append((path, "matches a schema it must not match"))
    if "if" in schema:
        if not validate(instance, schema["if"], root, path, []):
            if "then" in schema:
                validate(instance, schema["then"], root, path, errors)
        elif "else" in schema:
            validate(instance, schema["else"], root, path, errors)

    return errors


def validate_file(schema_path, instance):
    schema = json.loads(pathlib.Path(schema_path).read_text(encoding="utf-8"))
    return validate(instance, schema, schema)


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: jsonschema_mini.py <schema.json> <instance.json>\n")
        return 2
    try:
        schema = json.loads(pathlib.Path(argv[0]).read_text(encoding="utf-8"))
        instance = json.loads(pathlib.Path(argv[1]).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"error: {exc}\n")
        return 2

    gaps = unsupported_keywords(schema)
    if gaps:
        sys.stderr.write(
            "error: schema uses keywords this evaluator does not implement: "
            + ", ".join(gaps)
            + "\n"
        )
        return 2

    errors = validate(instance, schema, schema)
    for path, message in errors:
        print(f"  ERROR {path}: {message}")
    if errors:
        print(f"\nFAILED: {len(errors)} error(s).")
        return 1
    print("OK: instance is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
