"""JSON-schema checks against ``docs/contract/keeper-contract-v1.json``.

Uses ``jsonschema`` (Draft 2020-12) when it is installed; otherwise a tiny
validator covering the keywords the contract uses: ``$ref`` (local ``#/$defs``),
``type``, ``enum``, ``const``, ``required``, ``properties``,
``additionalProperties``, ``items``, ``minItems``, ``maxItems``, ``minimum``,
``pattern``, ``oneOf`` and ``anyOf``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "docs" / "contract" / "keeper-contract-v1.json"
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
DEFS = CONTRACT["$defs"]

try:  # pragma: no cover - depends on the environment
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    Draft202012Validator = None

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _errors(value, schema, path):
    """Yield ``path: message`` strings for every violation (tiny validator)."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[1]
        yield from _errors(value, DEFS[name], path)
        return
    if "type" in schema:
        want = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[t](value) for t in want):
            yield f"{path}: expected {want}, got {type(value).__name__}"
            return
    if "enum" in schema and value not in schema["enum"]:
        yield f"{path}: {value!r} not in {schema['enum']}"
    if "const" in schema and value != schema["const"]:
        yield f"{path}: {value!r} != {schema['const']!r}"
    if "minimum" in schema and _TYPES["number"](value) and value < schema["minimum"]:
        yield f"{path}: {value} < {schema['minimum']}"
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        yield f"{path}: {value!r} does not match {schema['pattern']}"
    for word in ("oneOf", "anyOf"):
        if word in schema:
            hits = sum(1 for sub in schema[word] if not list(_errors(value, sub, path)))
            if hits == 0 or (word == "oneOf" and hits != 1):
                yield f"{path}: matches {hits} of {word}"
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                yield f"{path}: missing required {name!r}"
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for name, sub in value.items():
            if name in props:
                yield from _errors(sub, props[name], f"{path}.{name}")
            elif extra is False:
                yield f"{path}: unexpected property {name!r}"
            elif isinstance(extra, dict):
                yield from _errors(sub, extra, f"{path}.{name}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            yield f"{path}: fewer than {schema['minItems']} items"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            yield f"{path}: more than {schema['maxItems']} items"
        if "items" in schema:
            for index, sub in enumerate(value):
                yield from _errors(sub, schema["items"], f"{path}[{index}]")


def problems(value, name, use_library=True):
    """All violations of ``$defs/<name>`` by *value* (empty list = valid)."""
    if use_library and Draft202012Validator is not None:
        schema = {"$ref": f"#/$defs/{name}", "$defs": DEFS}
        validator = Draft202012Validator(schema)
        return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
                for e in validator.iter_errors(value)]
    return list(_errors(value, {"$ref": f"#/$defs/{name}"}, "$"))


def assert_valid(value, name):
    found = problems(value, name)
    assert not found, f"{name} violated: {found[:5]} in {json.dumps(value)[:300]}"
