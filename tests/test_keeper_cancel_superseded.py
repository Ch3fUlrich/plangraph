"""LE-KEEPER-CANCEL-SUPERSEDED-WITH-DEPENDENTS: the owner role retires a superseded
open item that has dependents (a D-id reason repairs every dependent's edge), a
gate with more than 200 dependencies is repairable by naming the edge to drop, and
a define under a gate with a standing cancelled dependency is accepted."""
import pytest

from keeper_util import World, defn, seed, to_claimed
from plangraph import views
from plangraph.keeper.errors import Refused

L1 = "l1main"


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("X", [])])
    world.store.create_items(world.p("router"), [
        defn("GATE", ["X", "A"], kind="gate", under="GOAL"),
        defn("WORK", ["X"], under="GOAL")])
    yield world
    world.close()


def problems(w):
    return [p["code"] for p in views.validate(w.store._all_items())]


def deps(w, key):
    return w.store._item(key)["depends_on"]


def test_owner_cancels_open_item_with_d_id_and_dependents_lose_the_edge(w):
    out = w.store.cancel(w.p(L1), "demo:X", "superseded by R-batch-05, D-6020")
    assert out["state"] == "cancelled"
    assert sorted(out["dropped_from"]) == ["demo:GATE", "demo:GOAL", "demo:WORK"]
    assert deps(w, "demo:GATE") == ["demo:A"]          # its other dep stays
    assert deps(w, "demo:GOAL") == ["demo:A", "demo:GATE", "demo:WORK"]
    assert deps(w, "demo:WORK") == []
    assert "depends_on_cancelled" not in problems(w)
    assert problems(w) == []                           # all stay reachable
    assert w.store.verify_integrity() == []


def test_the_router_cancel_with_a_d_id_repairs_lane_dependents_too(w):
    w.store.cancel(w.p("router"), "demo:X", "superseded D-77")
    assert deps(w, "demo:WORK") == []
    assert "depends_on_cancelled" not in problems(w)


def test_cancel_without_a_d_id_keeps_the_old_rules(w):
    with pytest.raises(Refused) as exc:
        w.store.cancel(w.p(L1), "demo:X", "superseded")
    assert exc.value.code == "conflict" and "active dependents" in exc.value.msg
    w.store.cancel(w.p("router"), "demo:X", "superseded")
    assert deps(w, "demo:WORK") == ["demo:X"]          # a lane edge stays: no D-id


def test_l1_cannot_cancel_a_held_item_this_way(w):
    to_claimed(w, "demo:X")
    with pytest.raises(Refused) as exc:
        w.store.cancel(w.p(L1), "demo:X", "superseded D-6020")
    assert exc.value.code == "conflict" and "held" in exc.value.msg
    assert w.store._item("demo:X")["state"] == "claimed"


def test_another_projects_l1_stays_forbidden(w):
    with pytest.raises(Refused) as exc:
        w.store.cancel(w.p("l1other"), "demo:X", "superseded D-6020")
    assert exc.value.code == "forbidden"


def test_an_edge_the_cancel_cannot_repair_is_kept_and_named(w):
    # WORK is claimed (legacy row): dropping its edge would reset its claim.
    w.conn.execute("UPDATE items SET state='claimed' WHERE key='demo:WORK'")
    with pytest.raises(Refused) as exc:
        w.store.cancel(w.p(L1), "demo:X", "superseded D-6020")
    assert exc.value.code == "conflict" and "demo:WORK" in exc.value.msg
    assert w.store._item("demo:X")["state"] == "open"
    out = w.store.cancel(w.p("router"), "demo:X", "superseded D-6020")
    assert out["kept"] == [{"key": "demo:WORK", "why": "claimed"}]
    assert deps(w, "demo:WORK") == ["demo:X"]


def test_an_operator_added_edge_is_never_dropped_silently(tmp_path):
    w = World(tmp_path)
    seed(w, [("A", [])])
    w.store.create_items(w.p("router"), [
        defn("OP", kind="operator_task", operator=True, wait="task", under="GOAL"),
        defn("WORK", ["OP"], under="GOAL")])
    with pytest.raises(Refused) as exc:
        w.store.cancel(w.p(L1), "demo:OP", "superseded D-6020")
    assert "demo:WORK" in exc.value.msg and "operator" in exc.value.msg
    out = w.store.cancel(w.p("router"), "demo:OP", "superseded D-6020")
    assert out["kept"] == [{"key": "demo:WORK", "why": "operator edge"}]
    assert deps(w, "demo:WORK") == ["demo:OP"]
    w.close()


# ------------------------------------------------- a gate over 200 dependencies
@pytest.fixture
def big(w):
    """A gate with 300 dependencies, as legacy data holds them (create caps at 200)."""
    ids = [f"B{i}" for i in range(300)]
    for chunk in range(0, 300, 100):
        w.store.create_items(w.p("router"), [defn(i) for i in ids[chunk:chunk + 100]]
                             + [defn(f"BG{chunk}", ids[chunk:chunk + 100],
                                     kind="gate", under="GOAL")])
    w.store.create_items(w.p("router"), [defn("BIG", [], kind="gate", under="GOAL")])
    w.execute_many("INSERT INTO edges(src,dst,rel) VALUES(?,?,'depends_on')",
                   [("demo:BIG", f"demo:{i}") for i in ids])
    w.conn.execute("UPDATE items SET state='cancelled' WHERE key='demo:B7'")
    return w


def test_named_edge_drop_repairs_a_gate_over_200_dependencies(big):
    w = big
    assert len(deps(w, "demo:BIG")) == 300
    out = w.store.edit_definition(w.p("router"), "demo:BIG",
                                  {"depends_on": {"remove": ["demo:B7"]}})
    assert out["key"] == "demo:BIG"
    after = deps(w, "demo:BIG")
    assert len(after) == 299 and "demo:B7" not in after and "demo:B8" in after


def test_the_list_form_stays_capped_and_add_cannot_grow_a_big_gate(big):
    w = big
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:BIG",
                                {"depends_on": [f"demo:B{i}" for i in range(250)]})
    assert exc.value.code == "validation"
    assert "249 open dependencies" in exc.value.msg
    with pytest.raises(Refused, match="249 open dependencies"):
        w.store.apply_ops(w.p("router"), [{
            "op": "edit", "key": "demo:BIG",
            "fields": {"depends_on": [f"demo:B{i}" for i in range(250)]},
        }])
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:BIG",
                                {"depends_on": {"add": ["demo:A"]}})
    assert exc.value.code == "validation" and "exceeds" in exc.value.msg


def test_remove_dep_op_repairs_a_big_gate_in_a_batch(big):
    w = big
    w.store.apply_ops(w.p("router"), [{"op": "remove_dep", "key": "demo:BIG",
                                       "dep": "demo:B7"}])
    assert len(deps(w, "demo:BIG")) == 299


def test_define_under_a_gate_with_a_standing_cancelled_dependency_is_accepted(w):
    w.store.create_items(w.p("router"), [
        defn("STALE"), defn("STALE-GATE", ["STALE"], kind="gate", under="GOAL")])
    w.conn.execute("UPDATE items SET state='cancelled' WHERE key='demo:STALE'")
    out = w.store.create_items(w.p("router"), [defn("NEXT", under="STALE-GATE")])
    assert out["created"] == ["demo:NEXT"] and "refused" not in out
    assert any("demo:STALE-GATE" in x and "demo:STALE" in x and "cancelled" in x
               for x in out["warnings"])
    assert "demo:NEXT" in deps(w, "demo:STALE-GATE")
    out = w.store.apply_ops(w.p("router"), [{"op": "create", "item": [
        defn("NEXT2", under="STALE-GATE")]}])
    assert out["applied"] == 1
    w.store.edit_definition(w.p("router"), "demo:STALE-GATE",
                            {"depends_on": {"remove": ["demo:STALE"]}})
    assert not [p for p in views.validate(w.store._all_items())
                if p["key"] == "demo:STALE-GATE" and p["code"] == "depends_on_cancelled"]


def test_withdraw_usage_says_in_review_only():
    from plangraph.keeper import cli
    sub = next(a for a in cli.build_parser()._actions if hasattr(a, "choices")
               and a.choices and "withdraw" in a.choices)
    helps = {c.dest: c.help for c in sub._choices_actions}
    assert "in_review only" in helps["withdraw"]
