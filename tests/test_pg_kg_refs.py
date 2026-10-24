"""PG-KG-REFS (D-879): plan items reference Omnigraph knowledge nodes by graph:Type:slug.

Mutations killed: a malformed/oversize ref accepted, an unknown node type accepted, an L1
editing another project's refs, the fold disagreeing with the cache, duplicates kept.
"""
from __future__ import annotations

import pytest

from keeper_util import World, defn, seed
from plangraph.keeper.errors import Refused
from plangraph.keeper.item_fields import KG_TYPES, MAX_KG_REFS, check_kg_refs


@pytest.mark.parametrize("bad", [
    "not-a-list", ["autoos"], ["autoos:Decision"], ["autoos:Task:x"], ["Autoos:Decision:x"],
    ["autoos:decision:x"], ["autoos:Decision:X Y"], ["autoos:Decision:" + "a" * 129],
    ["a" * 64 + ":Decision:x"], [1], ["autoos:Decision:x\n"], ["autoos:Decision:../etc"],
    [f"autoos:Decision:d{i}" for i in range(MAX_KG_REFS + 1)]])
def test_check_kg_refs_refuses(bad):
    with pytest.raises(Refused):
        check_kg_refs(bad)


def test_check_kg_refs_canonical_sorted_unique():
    out = check_kg_refs(["homelab-server:Rule:b", "autoos:Decision:a", "autoos:Decision:a"])
    assert out == '["autoos:Decision:a","homelab-server:Rule:b"]'
    assert check_kg_refs([]) is None and check_kg_refs(None) is None
    assert set(KG_TYPES) == {"Decision", "Rule", "Convention", "Component", "Preference", "Project"}


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def test_create_show_edit_fold(w):
    r = w.p("router")
    (key,) = seed(w, [("A", [])], kg_refs=["autoos:Decision:d-646"])
    assert w.store.show(r, key)["kg_refs"] == ["autoos:Decision:d-646"]
    w.store.edit_definition(w.p("l1main"), key, {"kg_refs": ["autoos:Rule:r1", "autoos:Decision:d-646"]})
    assert w.store.show(r, key)["kg_refs"] == ["autoos:Decision:d-646", "autoos:Rule:r1"]
    snap = {i["key"]: i for i in w.store.snapshot(r)["items"]}
    assert snap[key]["kg_refs"] == ["autoos:Decision:d-646", "autoos:Rule:r1"]
    w.store.edit_definition(r, key, {"kg_refs": None})
    assert w.store.show(r, key)["kg_refs"] == []
    assert w.store.verify_integrity() == []


def test_l1_cannot_edit_another_projects_refs(w):
    (key,) = seed(w, [("A", [])])
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("l1other"), key, {"kg_refs": ["autoos:Decision:x"]})
    assert exc.value.code == "forbidden"


def test_bad_refs_refused_on_create_and_edit(w):
    with pytest.raises(Refused):
        seed(w, [("A", [])], kg_refs=["autoos:Task:x"])
    (key,) = seed(w, [("B", [])], project="other")
    with pytest.raises(Refused):
        w.store.edit_definition(w.p("router"), key, {"kg_refs": ["nope"]})
