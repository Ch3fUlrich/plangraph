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


def test_l1_alias_resolving_to_another_projects_gate_is_forbidden(w):
    """An alias key is a pointer, not a licence: ``under`` may name the old key
    of a gate that ``rehome`` moved to another project, but the attach is still
    judged on where the gate LIVES (project B), not on the alias' project A."""
    w.store.create_items(w.p("router"), [defn("GATE", kind="goal")])
    w.store.rehome(w.p("router"), "demo:GATE", "other")   # -> other:GATE
    assert w.store.resolve_key("demo:GATE") == "other:GATE"
    # the L1 under test owns project A only (the fixture's l1both owns both)
    assert w.p("l1main").projects == ["demo"]

    def gate_edges():
        return [(r["src"], r["dst"]) for r in w.conn.execute(
            "SELECT src,dst FROM edges WHERE src=? OR dst=?",
            ("other:GATE", "other:GATE"))]

    assert gate_edges() == []                     # nothing yet for the gate
    before = _snapshot(w)
    with pytest.raises(Refused) as exc:           # alias -> other:GATE (project B)
        w.store.create_items(w.p("l1main"), [defn("NEW", under="demo:GATE")])
    assert exc.value.code == "forbidden"
    assert _snapshot(w) == before                 # no item, no edge, no event
    assert w.store._item("demo:NEW") is None      # NO item was created
    assert gate_edges() == []                     # NO edge row for the gate
    # control: the router runs the very same attach and it succeeds
    out = w.store.create_items(w.p("router"), [defn("CTRL", under="demo:GATE")])
    assert out["created"] == ["demo:CTRL"]
    assert out["attached"] == [{"key": "demo:CTRL", "under": "other:GATE"}]
    assert gate_edges() == [("other:GATE", "demo:CTRL")]   # G -> N, written at last
    assert _problems(w) == [] and verify_integrity(w.conn) == []


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


def test_cancel_drops_gate_edges_and_records_drop(w):
    w.store.create_items(w.p("router"), [
        defn("GATE", ["X"], kind="gate", under="GOAL"),
        defn("WORK", ["X"], under="GOAL")])
    w.store.cancel(w.p("router"), "demo:X", "superseded")
    assert w.store._item("demo:GATE")["depends_on"] == []
    assert w.store._item("demo:GOAL")["depends_on"] == ["demo:GATE", "demo:WORK"]
    assert w.store._item("demo:WORK")["depends_on"] == ["demo:X"]
    event = w.conn.execute(
        "SELECT payload FROM events WHERE key='demo:GATE'"
        " AND type='item_defined' ORDER BY seq DESC LIMIT 1").fetchone()
    assert json.loads(event[0])["dropped_cancelled_dependency"] == "demo:X"
    assert w.store.create_items(w.p("router"), [
        defn("NEXT", under="GATE")])["created"] == ["demo:NEXT"]
    assert w.store.verify_integrity() == []


def test_define_refuses_only_item_under_gate_with_cancelled_dep(w):
    """Name kept from the refusing rule: a standing cancelled dependency of the gate
    is no longer a refusal (the define did not create the edge); it is reported."""
    # Model legacy data without relying on cancel's new edge cleanup.
    w.conn.execute("UPDATE items SET state='cancelled' WHERE key='demo:X'")
    out = w.store.create_items(w.p("router"), [
        defn("ACCEPTED", under="GOAL"),
        defn("CREATED", project="other", under="OG")])
    assert out["created"] == ["demo:ACCEPTED", "other:CREATED"]
    assert "refused" not in out
    assert w.store._item("demo:ACCEPTED") is not None
    warning, = [x for x in out["warnings"] if "cancelled" in x]
    assert "demo:GOAL" in warning and "demo:X" in warning and "did not create" in warning


def test_cancel_gate_cleanup_invalidates_active_epoch(w):
    from keeper_util import to_claimed
    w.store.create_items(w.p("router"), [
        defn("GATE", kind="gate", under="GOAL")])
    epoch = to_claimed(w, "demo:GATE")
    w.store.edit_definition(w.p("router"), "demo:GATE", {"depends_on": ["demo:X"]})
    before = w.store._item("demo:GATE")
    assert before["epoch"] > epoch
    w.store.cancel(w.p("router"), "demo:X", "abandoned")
    after = w.store._item("demo:GATE")
    assert after["depends_on"] == []
    assert after["epoch"] == before["epoch"] + 1
    assert after["round"] == before["round"] + 1
    assert w.store.verify_integrity() == []


def test_cancel_cleanup_is_atomic_with_batch_failure(w):
    before = _snapshot(w)
    with pytest.raises(Refused):
        w.store.apply_ops(w.p("router"), [
            {"op": "cancel", "key": "demo:X"},
            {"op": "remove_dep", "key": "demo:NOT-THERE", "dep": "demo:X"}])
    assert _snapshot(w) == before
    assert w.store._item("demo:X")["state"] == "open"
    assert w.store.verify_integrity() == []


def test_all_cancelled_gate_refusals_create_nothing_and_batch_stays_atomic(w):
    """Name kept from the refusing rule: nothing is refused now, so a batch define
    under a gate with a standing cancelled dependency goes through whole."""
    w.conn.execute("UPDATE items SET state='cancelled' WHERE key='demo:X'")
    out = w.store.apply_ops(w.p("router"), [{"op": "create", "item": [
        defn("CREATED", project="other", under="OG"),
        defn("ALSO", under="GOAL")]}])
    assert out["applied"] == 1
    assert w.store._item("demo:ALSO") is not None
    assert any("demo:X" in x for x in out["results"][0]["warnings"])


def test_cancel_gate_with_live_children_stays_legal_and_keeps_stranding_edge(w):
    """Cancelling a gate with live children was legal before the edge drop and
    stays so; the goal's edge to the gate is the children's only path to the
    goal, so it is kept (legacy ``depends_on_cancelled``) instead of dropped."""
    w.store.create_items(w.p("router"), [
        defn("CHILD"), defn("GATE", ["CHILD"], kind="gate", under="GOAL")])
    edges = _edges(w)
    events = w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    out = w.store.cancel(w.p("router"), "demo:GATE", "abandoned")
    assert out == {"key": "demo:GATE", "state": "cancelled"}
    assert _edges(w) == edges
    assert w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events + 1
    assert w.conn.execute(
        "SELECT COUNT(*) FROM events WHERE payload LIKE '%dropped_cancelled%'"
    ).fetchone()[0] == 0
    assert {p["code"] for p in _problems(w)} == {"depends_on_cancelled"}
    assert w.store.verify_integrity() == []


@pytest.mark.parametrize("json_mode", [False, True])
def test_define_file_reports_partial_refusal(w, tmp_path, capsys, json_mode):
    """Name kept from the refusing rule: ``pg define`` of a file under a gate with a
    standing cancelled dependency creates every item and prints the warning."""
    from types import SimpleNamespace
    from plangraph.keeper.cli_output_verbs import cmd_define

    w.conn.execute("UPDATE items SET state='cancelled' WHERE key='demo:X'")
    path = tmp_path / "definitions.json"
    path.write_text(json.dumps({"items": [
        defn("ACCEPTED", under="GOAL", description="Why: needed\nHow: implement"),
        defn("CREATED", project="other", under="OG",
             description="Why: needed\nHow: implement")]}))

    class Session:
        def call(self, method, verb, *, body):
            assert (method, verb) == ("POST", "create_items")
            return w.store.create_items(w.p("router"), body["items"])

    session = Session()
    session.json_mode = json_mode
    assert cmd_define(SimpleNamespace(file=str(path), edit=False), session) == 0
    out = capsys.readouterr().out
    assert w.store._item("demo:ACCEPTED") is not None
    assert w.store._item("other:CREATED") is not None
    if json_mode:
        assert "refused" not in json.loads(out)
    else:
        assert "created 2 items" in out


def test_define_under_gate_at_capacity_names_open_split(w):
    router = w.p("router")
    ids = [f"DEP-{n}" for n in range(200)]
    w.store.create_items(router, [*(defn(key) for key in ids),
                                 defn("GATE", ids, kind="gate", under="GOAL"),
                                 defn("GATE-A", kind="gate", under="GOAL")])
    before = _snapshot(w)
    with pytest.raises(Refused, match="split gate demo:GATE-A"):
        w.store.create_items(router, [defn("NEW", under="GATE")])
    assert _snapshot(w) == before
    assert w.store.create_items(router, [defn("NEW", under="GATE-A")])["created"] == ["demo:NEW"]


def test_reap_raises_one_gate_capacity_alert_at_190_open_or_done(w):
    router = w.p("router")
    ids = [f"DEP-{n}" for n in range(190)]
    w.store.create_items(router, [*(defn(key) for key in ids[:-1]),
                                 defn(ids[-1], under="GOAL"),
                                 defn("GATE", ids[:-1], kind="gate", under="GOAL")])
    w.conn.execute("UPDATE items SET state='done' WHERE key='demo:DEP-0'")
    system = w.p("system")
    w.store.reap(system)
    sql = "SELECT key FROM alerts WHERE code='gate_dependency_capacity' AND state='open'"
    assert w.conn.execute(sql).fetchall() == []
    w.store.edit_definition(router, "demo:GATE", {"depends_on": ids})
    w.store.reap(system)
    w.store.reap(system)
    assert [row["key"] for row in w.conn.execute(sql)] == ["demo:GATE"]
    w.store.edit_definition(router, "demo:GATE", {"depends_on": ids[:-1]})
    w.store.reap(system)
    assert w.conn.execute(sql).fetchall() == []


def test_define_refuses_a_nested_slice_even_after_the_parent_criteria_are_edited(w):
    router = w.p("router")
    first = defn("X-S1", under="X", done_criteria="Slice of demo:X: fix the first part")
    assert w.store.create_items(router, [first])["created"] == ["demo:X-S1"]
    w.store.edit_definition(router, "demo:X-S1", {
        "done_criteria": "Revised criteria",
        "criteria_reason": "Clarify the first slice",
        "criteria_evidence": "D-123",
    })
    nested = defn("X-S1-S1", under="X-S1",
                  done_criteria="Slice of demo:X-S1: fix the inner part")
    before = _snapshot(w)
    with pytest.raises(Refused, match="parent demo:X-S1 is already a slice"):
        w.store.create_items(router, [nested])
    assert _snapshot(w) == before


def test_define_attaches_a_non_slice_under_a_slice(w):
    router = w.p("router")
    first = defn("X-S1", under="X", done_criteria="Slice of demo:X: fix the first part")
    w.store.create_items(router, [first])
    result = w.store.create_items(router, [defn("FOLLOWUP", under="X-S1")])
    assert result["attached"] == [{"key": "demo:FOLLOWUP", "under": "demo:X-S1"}]
    assert w.store._item("demo:X-S1")["depends_on"] == ["demo:FOLLOWUP"]
