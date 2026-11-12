"""PLACEMENT-R1a: hosts schema and example agree (D-3676, D-3681)."""
import copy
import json
import pathlib

import pytest

jsonschema = pytest.importorskip("jsonschema")

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "docs/schemas/hosts.schema.json").read_text())
EXAMPLE = json.loads((ROOT / "docs/schemas/examples/hosts.example.json").read_text())


def test_example_validates_against_schema():
    jsonschema.validate(EXAMPLE, SCHEMA)


def test_class_fixture_and_optional_schema_agree():
    host_schema = SCHEMA["$defs"]["hostEntry"]
    assert "class" in host_schema["properties"]
    assert "class" not in host_schema["required"]
    assert host_schema["properties"]["class"]["enum"] == ["worker", "restricted"]
    assert all("class" in host for host in EXAMPLE["hosts"])
    for label in ["worker", "restricted", None]:
        doc = copy.deepcopy(EXAMPLE)
        if label is None:
            del doc["hosts"][0]["class"]
        else:
            doc["hosts"][0]["class"] = label
        jsonschema.validate(doc, SCHEMA)
    doc["hosts"][0]["class"] = "unknown"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, SCHEMA)


def test_schema_property_names_match_example():
    defs = SCHEMA["$defs"]
    samples = {
        "$root": EXAMPLE,
        "hostEntry": EXAMPLE["hosts"][0],
        "caps": EXAMPLE["hosts"][0]["caps"],
        "warmup": EXAMPLE["hosts"][0]["warmup"],
        "repoEntry": next(iter(EXAMPLE["hosts"][0]["repos"].values())),
    }
    for name, sample in samples.items():
        node = SCHEMA if name == "$root" else defs[name]
        keys = set(sample)
        if name == "hostEntry":
            keys = set().union(*(host.keys() for host in EXAMPLE["hosts"]))
        assert sorted(node["properties"]) == sorted(keys), name


@pytest.mark.parametrize("pointer,value", [
    (("roles",), ["implement", "implement"]),
    (("warmup", "ramp_mb"), {"deploy": 1}),
    *(( ("repos", "plangraph", "import_roots"), [path]) for path in
      ["/etc", "C:relative", "C:/absolute", "\\root", "../rust", "a/../b", "a\\..\\b"]),
])
def test_schema_refuses_duplicate_roles_and_invalid_paths(pointer, value):
    doc = copy.deepcopy(EXAMPLE)
    target = doc["hosts"][0]
    for key in pointer[:-1]:
        target = target[key]
    target[pointer[-1]] = value
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, SCHEMA)


def test_schema_accepts_null_python_and_relative_imports():
    doc = copy.deepcopy(EXAMPLE)
    repo = doc["hosts"][0]["repos"]["plangraph"]
    repo["python"] = None
    repo["import_roots"] = ["rust", "a/b", "a\\b", "..hidden"]
    jsonschema.validate(doc, SCHEMA)
