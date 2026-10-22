"""PG-LE-DEPS-OPS: ``apply_ops`` ``add_dep``/``remove_dep`` + cancelled-dep drops.

``add_dep``/``remove_dep`` are batch ops that delegate to the item's own
``depends_on`` edit, so they carry exactly the edit's authorisation, state and
graph checks, append one ``item_defined`` event, and commit in the batch's
single transaction.  A dep that is already present / not present is a no-op
that only warns (no write, no event); a dep naming a cancelled item is dropped
with a warning instead of refused, while an unknown dep or a cycle still
refuses the whole batch and rolls everything back.  F4: a *claimed* item
accepts the router's dep edit.

Mutations killed: an edge left behind by a rolled-back batch, no-op dep ops
that append events, a non-router driving ``apply_ops``, a ``depends_on`` edit
that keeps a cancelled dep (``views.validate``'s ``depends_on_cancelled``),
cycles or unknown deps written anyway, and ``tools/apply_batch.py`` accepting
a non-str ``dep``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from keeper_util import World, defn, to_claimed
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from apply_batch import BatchError, validate_op  # noqa: E402

@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("A"), defn("B"), defn("C"),
        defn("GOAL", ["A", "B", "C"], kind="goal"),
    ])
    yield world
    world.close()

@pytest.fixture
def wcyc(tmp_path):
    # B depends on A already, so ``add_dep A->B`` would close a cycle; the goal
    # reaches A only through B (goal -> B -> A).
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("A"), defn("B", ["A"]), defn("GOAL", ["B"], kind="goal"),
    ])
    yield world
    world.close()

def _edges(w):
    return {(r["src"], r["dst"])
            for r in w.conn.execute("SELECT src,dst,rel FROM edges")}

def _events(w):
    return w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

def _snapshot(w):
    """Items, edges and events: everything a refusal must leave untouched."""
    return (w.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            sorted(_edges(w)), _events(w))

def _integrity(w):
    return verify_integrity(w.conn)

# ------------------------------------------------------------------ happy path
def test_add_dep_writes_the_edge(w):
    results = w.store.apply_ops(w.p("router"),
                                [{"op": "add_dep", "key": "demo:A",
                                  "dep": "demo:B"}])
    assert results["results"][0]["key"] == "demo:A"
    assert ("demo:A", "demo:B") in _edges(w)
    assert _integrity(w) == []

def test_remove_dep_drops_the_edge(w):
    w.store.apply_ops(w.p("router"),
                      [{"op": "add_dep", "key": "demo:A", "dep": "demo:B"}])
    assert ("demo:A", "demo:B") in _edges(w)
    w.store.apply_ops(w.p("router"),
                      [{"op": "remove_dep", "key": "demo:A", "dep": "demo:B"}])
    assert ("demo:A", "demo:B") not in _edges(w)
    assert _integrity(w) == []

def test_add_then_remove_in_one_batch_nets_zero_edges(w):
    before = sorted(_edges(w))
    results = w.store.apply_ops(w.p("router"), [
        {"op": "add_dep", "key": "demo:A", "dep": "demo:B"},
        {"op": "remove_dep", "key": "demo:A", "dep": "demo:B"},
    ])
    assert len(results["results"]) == 2
    assert sorted(_edges(w)) == before
    assert _integrity(w) == []

# -------------------------------------------------------------------- no-ops
def test_noop_dep_ops_warn_and_write_no_event(w):
    w.store.apply_ops(w.p("router"),
                      [{"op": "add_dep", "key": "demo:A", "dep": "demo:B"}])
    before = _events(w)
    edges_before = _edges(w)
    results = w.store.apply_ops(w.p("router"), [
        {"op": "add_dep", "key": "demo:A", "dep": "demo:B"},
        {"op": "remove_dep", "key": "demo:A", "dep": "demo:C"},
    ])
    assert results["results"][0]["warnings"] == ["dependency demo:B already present"]
    assert results["results"][1]["warnings"] == ["dependency demo:C not present"]
    assert _edges(w) == edges_before and ("demo:A", "demo:B") in _edges(w)
    assert _events(w) == before

# ----------------------------------------------------------------- rollback
def test_later_failing_op_rolls_back_the_earlier_add(w):
    before = _snapshot(w)
    with pytest.raises(Refused) as ei:
        w.store.apply_ops(w.p("router"), [
            {"op": "add_dep", "key": "demo:A", "dep": "demo:B"},
            {"op": "add_dep", "key": "demo:A", "dep": "demo:NOPE"},
        ])
    assert "add_dep" in ei.value.msg and "unknown_dep" in ei.value.msg
    assert _snapshot(w) == before
    assert ("demo:A", "demo:B") not in _edges(w)

def test_apply_ops_is_router_only(w):
    with pytest.raises(Refused) as ei:
        w.store.apply_ops(w.p("l1main"),
                          [{"op": "add_dep", "key": "demo:A", "dep": "demo:B"}])
    assert ei.value.code == "forbidden"
    assert ("demo:A", "demo:B") not in _edges(w)

# --------------------------------------------------- cancelled dependencies
def test_edit_drops_cancelled_dependency_with_warning(w):
    w.store.cancel(w.p("router"), "demo:C", "superseded")
    result = w.store.edit_definition(w.p("router"), "demo:A",
                                     {"depends_on": ["demo:B", "demo:C"]})
    assert result["key"] == "demo:A"
    assert result["warnings"] == ["dropped cancelled dependency demo:C"]
    mine = {(s, d) for (s, d) in _edges(w) if s == "demo:A"}
    assert mine == {("demo:A", "demo:B")}
    assert ("demo:A", "demo:C") not in _edges(w)
    assert _integrity(w) == []

def test_add_dep_naming_a_cancelled_item_is_dropped(w):
    w.store.cancel(w.p("router"), "demo:C", "superseded")
    results = w.store.apply_ops(w.p("router"),
                                [{"op": "add_dep", "key": "demo:A",
                                  "dep": "demo:C"}])
    assert results["results"][0]["warnings"] == ["dropped cancelled dependency demo:C"]
    assert ("demo:A", "demo:C") not in _edges(w)
    assert _integrity(w) == []

def test_edit_without_cancelled_dep_returns_exactly_the_key(w):
    result = w.store.edit_definition(w.p("router"), "demo:A",
                                     {"depends_on": ["demo:B"]})
    assert result == {"key": "demo:A"}
    assert ("demo:A", "demo:B") in _edges(w)

# ------------------------------------------------------------ graph refuses
def test_add_dep_that_would_close_a_cycle_is_refused(wcyc):
    before = _snapshot(wcyc)
    with pytest.raises(Refused):
        wcyc.store.apply_ops(wcyc.p("router"),
                             [{"op": "add_dep", "key": "demo:A",
                               "dep": "demo:B"}])
    assert _snapshot(wcyc) == before
    assert _integrity(wcyc) == []

def test_add_dep_on_a_claimed_item_is_a_router_edit(w):
    """F4: the router may rewrite a claimed item's dependencies (and the
    delegation makes add_dep exactly that edit, item_defined event included)."""
    epoch = to_claimed(w, "demo:A")
    results = w.store.apply_ops(w.p("router"),
                                [{"op": "add_dep", "key": "demo:A",
                                  "dep": "demo:B"}])
    assert results["results"][0]["key"] == "demo:A"
    assert ("demo:A", "demo:B") in _edges(w)
    item = w.store._item("demo:A")
    assert (item["state"], item["epoch"]) == ("claimed", epoch + 1)
    assert item["round"] == 1          # the active-edit bump (D-434/F4; epoch bumped too)
    assert _integrity(w) == []

# --------------------------------------------------------------- batch door
def test_validate_op_accepts_a_dep_op_and_rejects_a_non_str_dep():
    validate_op({"op": "add_dep", "key": "demo:A", "dep": "demo:B"})
    with pytest.raises(BatchError):
        validate_op({"op": "add_dep", "key": "demo:A", "dep": 7})
