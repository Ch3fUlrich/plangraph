"""KEEPER-PROJECT-SCOPED-L1-APPLY: a granted l1 principal applies batches in its own
projects; one foreign key refuses the whole batch; only the router edits grants."""
import pytest

from keeper_util import World, defn, seed, to_claimed
from test_keeper_close import CLOSES, KEY as CLOSE_KEY, _world
from test_keeper_legacy_state import _legacy_world
from plangraph.keeper.errors import Refused

GOAL = "demo:GOAL"
L1 = "l1main"
SHA = "ab" * 32


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    seed(world, [("F", [])], project="other")
    world.store.grant_apply(world.p("router"), L1, "demo")
    yield world
    world.close()


def create(pid, **kw):
    return {"op": "create", "item": defn(pid, **kw)}


def link_goal(*lanes):
    return {"op": "edit", "key": GOAL, "fields": {"depends_on": list(lanes)}}


def apply(w, ops, who=L1, **kw):
    return w.store.apply_ops(w.p(who), ops, **kw)


def counts(w):
    q = w.conn.execute
    return (q("SELECT COUNT(*) FROM items").fetchone()[0],
            q("SELECT COUNT(*) FROM events").fetchone()[0])


def refused(w, ops, code="forbidden", who=L1, **kw):
    before = counts(w)
    with pytest.raises(Refused) as exc:
        apply(w, ops, who=who, **kw)
    assert exc.value.code == code
    assert counts(w) == before
    return exc.value


def test_granted_l1_creates_edits_cancels_and_sets_priority(w):
    out = apply(w, [create("X"), link_goal("A", "X"),
                    {"op": "priority", "key": "demo:X", "priority": 7}])
    assert out["applied"] == 3
    assert w.store._item("demo:X")["priority"] == 7
    out = apply(w, [{"op": "edit", "key": "demo:X",
                     "fields": {"title": "renamed"}},
                    link_goal("A"),
                    {"op": "cancel", "key": "demo:X", "reason": "D-1 dup"}])
    assert out["applied"] == 3
    assert w.store._item("demo:X")["state"] == "cancelled"


def test_granted_l1_add_evidence(w):
    to_claimed(w, "demo:A")
    out = apply(w, [{"op": "add_evidence", "key": "demo:A", "kind": "decision",
                     "ref": {"id": "D-1"}}])
    assert out["results"][0]["verified"] is True


def test_granted_l1_close_on_decision(tmp_path):
    cw = _world(tmp_path, CLOSES)
    cw.store.grant_apply(cw.p("router"), L1, "demo")
    out = cw.store.apply_ops(cw.p(L1), [create("X"), link_goal("A", "X"),
                                       {"op": "close_on_decision",
                                        "key": CLOSE_KEY}],
                             batch_ref=cw.batch_ref)
    assert out["results"][2]["state"] == "done"
    cw.close()


def test_granted_l1_legacy_state(tmp_path):
    lw = _legacy_world(tmp_path, review_tier="none")
    lw.store.grant_apply(lw.p("router"), L1, "demo")
    out = lw.store.apply_ops(lw.p(L1), [{"op": "legacy_state", "key": "demo:A",
                                        "state": "claimed", "note": "n"}])
    assert out["results"][0]["state"] == "claimed"
    lw.close()


@pytest.mark.parametrize("ops, key", [
    ([{"op": "cancel", "key": "other:F", "reason": "r"}], "other:F"),
    ([{"op": "priority", "key": "other:F", "priority": 3}], "other:F"),
    ([{"op": "edit", "key": "other:F", "fields": {"title": "t"}}], "other:F"),
    ([create("X"), link_goal("A", "X"),
      {"op": "cancel", "key": "other:F", "reason": "r"}], "other:F"),
    ([create("Y", deps=["other:F"])], "other:F"),
    ([create("Y", under="other:GOAL")], "other:GOAL"),
    ([{"op": "edit", "key": GOAL, "fields": {"depends_on": ["A", "other:F"]}}],
     "other:F"),
    ([{"op": "edit", "key": GOAL,
       "fields": {"depends_on": {"add": ["other:F"]}}}], "other:F"),
    ([create("Y", project="other")], "other:Y"),
    ([{"op": "legacy_state", "key": "other:F", "state": "claimed"}], "other:F"),
    ([{"op": "add_evidence", "key": "other:F", "kind": "decision",
       "ref": {"id": "D-1"}}], "other:F"),
    ([{"op": "close_on_decision", "key": "other:F"}], "other:F"),
])
def test_one_foreign_key_refuses_the_whole_batch_and_names_it(w, ops, key):
    err = refused(w, ops, batch_ref="x@y")
    assert key in err.msg
    assert "grant" in err.msg


@pytest.mark.parametrize("op", [
    {"op": "rehome", "key": "demo:A", "new_project": "other"},
    {"op": "set_kind", "key": "demo:A", "kind": "gate"},
    {"op": "add_dep", "key": "demo:A", "dep": "demo:GOAL"},
    {"op": "remove_dep", "key": "demo:GOAL", "dep": "demo:A"},
    {"op": "role", "key": "demo:A"},
    {"op": "token", "key": "demo:A"},
    {"op": "principal", "key": "demo:A"},
    {"op": "grant", "key": "demo:A"},
])
def test_forbidden_ops_for_a_granted_l1(w, op):
    err = refused(w, [op], code="forbidden" if op["op"] in (
        "rehome", "set_kind", "add_dep", "remove_dep") else "validation")
    assert op["op"] in err.msg


def test_ungranted_l1_stays_forbidden(w):
    refused(w, [create("X"), link_goal("A", "X")], who="l1other")
    w.store.revoke_apply(w.p("router"), L1, "demo")
    refused(w, [create("X"), link_goal("A", "X")])


def test_grant_in_another_project_does_not_open_demo(w):
    w.store.revoke_apply(w.p("router"), L1, "demo")
    w.store.grant_apply(w.p("router"), L1, "other")
    refused(w, [create("X"), link_goal("A", "X")])


def test_only_router_edits_grants(w):
    for who in (L1, "l1other", "workerA", "reader"):
        with pytest.raises(Refused) as exc:
            w.store.grant_apply(w.p(who), "l1other", "other")
        assert exc.value.code == "forbidden"
        with pytest.raises(Refused) as exc:
            w.store.revoke_apply(w.p(who), L1, "demo")
        assert exc.value.code == "forbidden"
    assert w.store.apply_grants(w.p("router")) == [
        {"principal": L1, "project": "demo", "granted_by": "router",
         "granted_at": w.store.apply_grants(w.p("router"))[0]["granted_at"]}]


def test_grant_needs_an_l1_principal_and_a_project(w):
    for target in ("workerA", "router", "nobody"):
        with pytest.raises(Refused) as exc:
            w.store.grant_apply(w.p("router"), target, "demo")
        assert exc.value.code in ("validation", "not_found")
    with pytest.raises(Refused):
        w.store.grant_apply(w.p("router"), L1, "")


def test_one_report_line_per_applied_batch_reaches_the_routing_inbox(w):
    ops = [create("X"), link_goal("A", "X")]
    before = w.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    apply(w, ops, dry_run=True)
    refused(w, [{"op": "cancel", "key": "other:F", "reason": "r"}])
    assert w.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before
    apply(w, ops, batch_ref=f"/plans/batch26.src@{SHA}", batch_name="batch26")
    rows = w.conn.execute(
        "SELECT * FROM messages WHERE \"to\"='routing'").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["from"] == L1 and row["posted_by"] == L1
    import json
    body = json.loads(row["body"])
    assert body["event"] == "granted_apply"
    assert body["batch"] == "batch26"
    assert body["source"] == "/plans/batch26.src"
    assert body["sha256"] == SHA
    assert len(body["ops_sha256"]) == 64
    assert body["ops"] == 2
    assert body["projects"] == ["demo"]


def test_router_apply_writes_no_report_line(w):
    apply(w, [create("X"), link_goal("A", "X")], who="router")
    assert w.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_grant_is_audited_and_removed_by_revoke(w):
    rows = w.store.apply_grants(w.p("router"))
    assert [(r["principal"], r["project"]) for r in rows] == [(L1, "demo")]
    w.store.revoke_apply(w.p("router"), L1, "demo")
    assert w.store.apply_grants(w.p("router")) == []
    acts = [r["action"] for r in w.conn.execute(
        "SELECT action FROM audit_log WHERE ok=1").fetchall()]
    assert "grant_apply" in acts and "revoke_apply" in acts
