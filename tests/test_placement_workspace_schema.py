#!/usr/bin/env python3
"""tests/test_placement_workspace_schema.py — workspace schema vs its examples (PLACEMENT-R1B)."""
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")
ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "docs/schemas/workspace.schema.json").read_text())
A = "a" * 40
BAD = {
    "writer": {"repo_id": "r", "mode": "writer", "base_sha": A, "revision_sha": A, "branch": "main"},
    "seat": {"repo_id": "r", "mode": "seat", "base_sha": A},
    "read": {"repo_id": "r", "mode": "read", "branch": "lane/x"},
    "none": {"repo_id": "r", "mode": "none", "revision_sha": A},
}


@pytest.mark.parametrize("mode", ["writer", "seat", "read", "none"])
def test_example_valid(mode):
    doc = json.loads((ROOT / f"docs/schemas/examples/workspace.{mode}.json").read_text())
    jsonschema.validate(doc, SCHEMA)


@pytest.mark.parametrize("mode", ["writer", "seat", "read", "none"])
def test_invalid_refused(mode):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(BAD[mode], SCHEMA)
