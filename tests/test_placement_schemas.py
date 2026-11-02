"""PLACEMENT-R1a: hosts schema and example agree (D-3676, D-3681)."""
import json
import pathlib

import pytest

jsonschema = pytest.importorskip("jsonschema")

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "docs/schemas/hosts.schema.json").read_text())
EXAMPLE = json.loads((ROOT / "docs/schemas/examples/hosts.example.json").read_text())


def test_example_validates_against_schema():
    jsonschema.validate(EXAMPLE, SCHEMA)


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
        assert sorted(node["properties"]) == sorted(sample), name
