"""The slice items the spawner's replan job defines are accepted by the real keeper store.

The fixture is the golden output of `rust/pg-spawnd/src/round_replan_ports_tests.rs`: the Rust
test asserts the job generates exactly this file, this test creates it in a keeper store.
"""
import json
from pathlib import Path

import pytest

from keeper_util import World, defn
from plangraph.keeper.errors import Refused

FIXTURE = Path(__file__).parent / "fixtures" / "replan_slice_definition.json"


def _world(tmp_path):
    w = World(tmp_path, backend="sqlite")
    w.store.create_items(w.p("router"), [
        defn("G", project="p", kind="goal", deps=["I"]), defn("I", project="p")])
    return w


def test_the_generated_slices_are_created_under_their_parent(tmp_path):
    w = _world(tmp_path)
    items = json.loads(FIXTURE.read_text())["items"]
    out = w.store.create_items(w.p("router"), items)
    assert out["created"] == ["p:I-S1", "p:I-S2"]
    assert {a["under"] for a in out["attached"]} == {"p:I"}


def test_a_slice_requires_matching_under_to_attach_it_to_the_parent(tmp_path):
    w = _world(tmp_path)
    item = {k: v for k, v in json.loads(FIXTURE.read_text())["items"][0].items() if k != "under"}
    with pytest.raises(Refused, match="slice_parent must match under"):
        w.store.create_items(w.p("router"), [item])
