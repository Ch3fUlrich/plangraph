"""PG-L1-CREATE-WITH-EDGE: ``create_items`` per-item ``under`` attaches G -> N.

``under: <key>`` names an existing gate/goal item; the new item N is created and,
in the SAME transaction, ``edges(src=G, dst=N, rel='depends_on')`` is written, so
N reaches the goal without a follow-up router edit.  The gate also gets one
``item_defined {fields: [depends_on]}`` event and an ``updated_at`` touch.

Mutations killed: an ``under`` that is not resolved/authorised (a forged
cross-project attach by an L1, or a gate in a project the L1 does not manage), a
missing gate, a gate created in the very same batch, a DONE/cancelled gate, a
cycle through the new edge written anyway, ``under`` smuggled through
``import_items``, ``under`` folded into ``note``, a fold that cannot carry the
gate's ``item_defined`` event, and priority/ordering work moving with it.
"""
from __future__ import annotations

import json

import pytest

from keeper_util import World, defn, to_done
from plangraph import views
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("X"),                                    # demo:X
        defn("GOAL", ["X"], kind="goal"),             # demo:GOAL (own-project gate)
        defn("OG", project="other", kind="goal")])    # other:OG (foreign gate)
    # An L1 that manages BOTH projects: the forged cross-project attach must
    # still be forbidden (same project or nothing for a non-router).
    token = auth.add_principal(world.conn, "l1both", "l1", ["demo", "other"],
                               family="teamB")
    world.tokens["l1both"] = token
    world.principals["l1both"] = auth.authenticate(world.conn, token)
    yield world
    world.close()


def _edges(w):
    return {(r["src"], r["dst"], r["rel"])
            for r in w.conn.execute("SELECT src,dst,rel FROM edges")}


def _snapshot(w):
    """Items, edges and events: everything a refusal must leave untouched."""
    return (w.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            sorted(_edges(w)),
            w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])


def _problems(w):
    return views.validate(w.store._all_items())


def _ready(w, project=None):
    return {i["key"] for i in views.ready(w.store._all_items(), project=project)}


# ------------------------------------------------------------------ happy path
def test_a_bare_new_item_still_needs_a_path_to_a_goal(w):
    # why ``under`` exists: without it the new item is unreachable...
    before = _snapshot(w)
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("NEW")])
    assert exc.value.code == "validation" and "unreachable_goal" in exc.value.msg
    assert _snapshot(w) == before


def test_router_attaches_under_a_goal_in_another_project(w):
    out = w.store.create_items(w.p("router"), [defn("NEW", under="other:OG")])
    assert out["created"] == ["demo:NEW"]
    assert out["attached"] == [{"key": "demo:NEW", "under": "other:OG"}]
    assert ("other:OG", "demo:NEW", "depends_on") in _edges(w)   # G -> N
    assert w.store._item("demo:NEW")["depends_on"] == []         # not N -> G
    assert _problems(w) == []                                    # reachable, acyclic
    assert "demo:NEW" in _ready(w, project="demo")
    assert verify_integrity(w.conn) == []
    # ``under`` is a known create field: never folded into ``note``
    assert not w.store._item("demo:NEW").get("note")
    # a create without ``under`` returns no ``attached`` key at all
    out = w.store.create_items(w.p("router"), [defn("G3", kind="goal")])
    assert out["created"] == ["demo:G3"] and "attached" not in out


def test_l1_attaches_under_its_own_project_goal(w):
    stamp = w.conn.execute(
        "SELECT updated_at FROM items WHERE key='demo:GOAL'").fetchone()[0]
    w.clock.advance(60)
    out = w.store.create_items(w.p("l1main"), [defn("NEW", under="GOAL")])
    assert out["attached"] == [{"key": "demo:NEW", "under": "demo:GOAL"}]
    assert ("demo:GOAL", "demo:NEW", "depends_on") in _edges(w)
    event = w.conn.execute(
        "SELECT actor, role, payload FROM events WHERE key='demo:GOAL'"
        " AND type='item_defined'").fetchone()
    assert event is not None
    assert event[0] == "l1main" and event[1] == "l1"
    assert json.loads(event[2]) == {"fields": ["depends_on"]}
    # the gate's row was touched inside the same transaction
    touched = w.conn.execute(
        "SELECT updated_at FROM items WHERE key='demo:GOAL'").fetchone()[0]
    assert touched != stamp
    assert _problems(w) == []
    assert "demo:NEW" in _ready(w)
    # the item_defined with fields=[depends_on] must not break the fold
    assert verify_integrity(w.conn) == [] and w.store.verify_integrity() == []


# ---------------------------------------------------------------- authorisation
def test_forged_cross_project_under_by_l1_is_forbidden(w):
    before = _snapshot(w)
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("l1both"), [defn("NEW", under="other:OG")])
    assert exc.value.code == "forbidden"
    assert _snapshot(w) == before            # no item, no edge, no event


def test_l1_may_not_attach_under_a_project_it_does_not_own(w):
    before = _snapshot(w)
    with pytest.raises(Refused) as exc:      # l1main: gate in `other`
        w.store.create_items(w.p("l1main"), [defn("NEW", under="other:OG")])
    assert exc.value.code == "forbidden"
    with pytest.raises(Refused) as exc:      # l1other: gate in `demo`
        w.store.create_items(
            w.p("l1other"), [defn("NEW", project="other", under="demo:GOAL")])
    assert exc.value.code == "forbidden"
    assert _snapshot(w) == before


@pytest.mark.parametrize("who", ["workerA", "reviewer", "reader", "system"])
def test_roles_that_may_not_create_cannot_attach(w, who):
    before = _snapshot(w)
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p(who), [defn("NEW", under="GOAL")])
    assert exc.value.code == "forbidden"
    assert _snapshot(w) == before


# -------------------------------------------------------------------- refusals
def test_unknown_under_is_not_found(w):
    before = _snapshot(w)
    for under in ("NOPE", "other:GONE"):
        with pytest.raises(Refused) as exc:
            w.store.create_items(w.p("router"), [defn("NEW", under=under)])
        assert exc.value.code == "not_found"
    with pytest.raises(Refused) as exc:      # a non-string is not a key either
        w.store.create_items(w.p("router"), [defn("NEW", under="")])
    assert exc.value.code == "validation"
    assert _snapshot(w) == before


def test_under_must_be_an_existing_item_not_a_batch_peer(w):
    before = _snapshot(w)
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [
            defn("G2", ["X"], kind="goal"),
            defn("N2", under="G2")])         # G2 is not written yet
    assert exc.value.code == "not_found"
    assert _snapshot(w) == before


def test_under_a_done_gate_is_bad_state(w):
    to_done(w, "demo:X")
    to_done(w, "demo:GOAL")
    before = _snapshot(w)
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("NEW", under="GOAL")])
    assert exc.value.code == "bad_state"
    assert _snapshot(w) == before


def test_under_a_cancelled_gate_is_bad_state(w):
    w.store.cancel(w.p("router"), "other:OG", "dropped")
    before = _snapshot(w)
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("NEW", under="other:OG")])
    assert exc.value.code == "bad_state"
    assert _snapshot(w) == before


def test_a_cycle_through_under_is_refused_before_any_write(w):
    before = _snapshot(w)
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"),
                             [defn("NEW", deps=["GOAL"], under="GOAL")])
    assert exc.value.code == "cycle"
    assert _snapshot(w) == before


def test_import_items_may_not_use_under(w):
    before = _snapshot(w)
    with pytest.raises(Refused) as exc:
        w.store.import_items(w.p("router"), [defn("NEW", under="GOAL")])
    assert exc.value.code == "validation"
    assert _snapshot(w) == before


def test_under_is_create_only_never_an_edit_field(w):
    before = _snapshot(w)
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:X", {"under": "demo:GOAL"})
    assert exc.value.code == "validation" and "under" in exc.value.msg
    assert _snapshot(w) == before


# ------------------------------------------------------------------- non-regression
def test_apply_ops_create_may_carry_under(w):
    # the same non-legacy path, batched: one transaction, same attach
    out = w.store.apply_ops(w.p("router"),
                            [{"op": "create", "item": [defn("NEW", under="GOAL")]}])
    assert out["applied"] == 1
    assert out["results"][0]["attached"] == [{"key": "demo:NEW",
                                              "under": "demo:GOAL"}]
    assert ("demo:GOAL", "demo:NEW", "depends_on") in _edges(w)
    assert verify_integrity(w.conn) == []


def test_priority_stays_router_only_and_only_depends_on_edges_exist(w):
    before = _edges(w)
    with pytest.raises(Refused) as exc:
        w.store.set_priority(w.p("l1main"), "demo:GOAL", 5)
    assert exc.value.code == "forbidden" and _edges(w) == before
    w.store.set_priority(w.p("router"), "demo:GOAL", 5)
    out = w.store.create_items(w.p("l1main"), [defn("NEW", under="GOAL")])
    assert out["attached"] == [{"key": "demo:NEW", "under": "demo:GOAL"}]
    assert {rel for _, _, rel in _edges(w)} == {"depends_on"}
    assert w.store._item("demo:GOAL")["priority"] == 5
    assert verify_integrity(w.conn) == []
