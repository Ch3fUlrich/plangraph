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


@pytest.mark.parametrize("name", ["read", "read-with-base"])
def test_read_optional_base(name):
    jsonschema.validate(json.loads((ROOT / f"docs/schemas/examples/workspace.{name}.json").read_text()), SCHEMA)


@pytest.mark.parametrize("mode", ["writer", "seat", "read", "none"])
@pytest.mark.parametrize("field", ["base_sha", "revision_sha", "branch"])
def test_null_and_mode_presence(mode, field):
    doc = json.loads((ROOT / f"docs/schemas/examples/workspace.{mode}.json").read_text())
    null = dict(doc, **{field: None})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(null, SCHEMA)
    required = {"writer": {"base_sha", "revision_sha", "branch"}, "seat": {"base_sha", "revision_sha"}, "read": {"revision_sha"}, "none": set()}
    if field in required[mode]:
        del doc[field]
    elif mode == "read" and field == "base_sha":
        doc[field] = A
        jsonschema.validate(doc, SCHEMA)
        return
    else:
        doc[field] = "lane/x" if field == "branch" else A
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, SCHEMA)


@pytest.mark.parametrize("branch,valid", [
    ("lane/main", True), ("lane/" + "a" * 195, True), ("lane/" + "a" * 196, False),
    ("lane/", False), ("lane/x.", False), ("lane/x/", False), ("lane/x./", False),
    ("lane/a\u0085b", False), ("lane/" + "😀" * 200, False), ("lane/" + "é" * 195, False),
] + [(f"lane/a{c}b", False) for c in "~^:?*[\\ \x00\x7f"])
def test_branch_rules(branch, valid):
    doc = dict(repo_id="p", mode="writer", base_sha=A, revision_sha=A, branch=branch)
    if valid:
        jsonschema.validate(doc, SCHEMA)
    else:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, SCHEMA)


@pytest.mark.parametrize("length,valid", [(64, True), (65, False)])
def test_repo_id_boundary(length, valid):
    doc = dict(repo_id="a" * length, mode="none")
    if valid:
        jsonschema.validate(doc, SCHEMA)
    else:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, SCHEMA)


@pytest.mark.parametrize("field", ["base_sha", "revision_sha"])
@pytest.mark.parametrize("sha", ["a" * 39, "a" * 41, "A" * 40, "g" * 40])
def test_sha_shape(field, sha):
    doc = dict(repo_id="p", mode="seat", base_sha=A, revision_sha=A)
    doc[field] = sha
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, SCHEMA)
