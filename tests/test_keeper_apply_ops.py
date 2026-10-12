"""B1: the router-only atomic ``apply_ops`` verb (one transaction, all or nothing)."""
import hashlib

import pytest

from keeper_util import World, defn, seed, to_claimed
from test_keeper_close import CLOSES, KEY as CLOSE_KEY, _world
from plangraph.keeper.errors import Refused

GOAL = "demo:GOAL"


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


def snap(w):
    """Row counts plus the hash-chain head: byte-for-byte unchanged proof."""
    q = w.conn.execute
    head = q("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return (q("SELECT COUNT(*) FROM items").fetchone()[0],
            q("SELECT COUNT(*) FROM edges").fetchone()[0],
            q("SELECT COUNT(*) FROM events").fetchone()[0],
            q("SELECT COUNT(*) FROM evidence").fetchone()[0],
            (head["seq"], head["hash"]))


def create(pid, **kw):
    return {"op": "create", "item": defn(pid, **kw)}


def link_goal(*lanes):
    return {"op": "edit", "key": GOAL, "fields": {"depends_on": list(lanes)}}


def test_half_valid_batch_leaves_db_unchanged(w):
    before = snap(w)
    ops = [create("X"), link_goal("A", "X"),
           {"op": "cancel", "key": "demo:NOPE", "reason": "r"}, create("Y")]
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), ops)
    assert exc.value.code == "not_found"
    assert exc.value.msg.startswith("op 3 cancel demo:NOPE: not_found ")
    assert snap(w) == before
    assert w.store._item("demo:X") is None


def test_create_leaf_and_link_goal_is_atomic_but_per_op_fails(w):
    # per-op path: the leaf is unreachable, and the goal cannot link a ghost
    with pytest.raises(Refused):
        w.store.create_items(w.p("router"), [defn("X")])
    with pytest.raises(Refused):
        w.store.edit_definition(w.p("router"), GOAL,
                                {"depends_on": ["A", "X"]})
    assert w.store._item("demo:X") is None
    out = w.store.apply_ops(w.p("router"), [create("X"), link_goal("A", "X")])
    assert out["applied"] == 2
    assert w.store._item("demo:X") is not None
    assert w.store._item(GOAL)["depends_on"] == ["demo:A", "demo:X"]


def _refused(w, ops, code, **kw):
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), ops, **kw)
    assert exc.value.code == code
    assert snap(w) == before
    return exc.value


def test_cycle_in_final_graph_refused_and_rolled_back(w):
    err = _refused(w, [create("X", deps=["Y"]), create("Y", deps=["X"]),
                       link_goal("A", "X")], "cycle")
    assert "op " in err.msg


def test_unreachable_item_in_final_graph_refused(w):
    err = _refused(w, [create("Z")], "validation")
    assert err.msg.startswith("op 1 create demo:Z: validation ")
    assert "demo:Z" in err.msg


def test_non_router_forbidden(w):
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("l1main"), [create("X"), link_goal("A", "X")])
    assert exc.value.code == "forbidden"
    assert snap(w) == before


@pytest.mark.parametrize("ops", [[], "x", [{"op": "nope"}], [{"op": "cancel"}],
                                 [{"op": "edit", "key": GOAL, "fields": {},
                                   "zzz": 1}], ["str"]])
def test_malformed_ops_refused(w, ops):
    _refused(w, ops, "validation")


def test_add_evidence_in_batch_commits_and_rolls_back(w):
    to_claimed(w, "demo:A")
    ev = {"op": "add_evidence", "key": "demo:A", "kind": "decision",
          "ref": {"id": "D-1"}}
    before = snap(w)
    _refused(w, [ev, {"op": "cancel", "key": "demo:NOPE"}], "not_found")
    assert snap(w) == before
    out = w.store.apply_ops(w.p("router"), [ev])
    assert out["results"][0]["verified"] is True
    assert snap(w)[3] == before[3] + 1


def test_state_change_between_prelock_and_lock_is_conflict(w):
    epoch = to_claimed(w, "demo:A")
    real = w.verifiers["decision"].verify

    def racing(item, ref, ctx):
        out = real(item, ref, ctx)
        w.store.release(w.p("l1main"), "demo:A", epoch)
        w.store.claim(w.p("l1main"), "demo:A", lease_s=7200)
        return out

    w.verifiers["decision"].verify = racing
    ev = {"op": "add_evidence", "key": "demo:A", "kind": "decision",
          "ref": {"id": "D-1"}}
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [create("X"), link_goal("A", "X"), ev])
    assert exc.value.code == "conflict"
    assert exc.value.msg.startswith("op 3 add_evidence demo:A: conflict ")
    assert w.store._item("demo:X") is None
    assert snap(w)[3] == before[3]


def _close_batch(w):
    return [create("X"), link_goal("A", "X"),
            {"op": "close_on_decision", "key": CLOSE_KEY}]


def test_close_on_decision_in_batch_and_fold_agrees(tmp_path):
    w = _world(tmp_path, CLOSES)
    out = w.store.apply_ops(w.p("router"), _close_batch(w), batch_ref=w.batch_ref)
    assert out["results"][2]["state"] == "done"
    assert w.store._item(CLOSE_KEY)["state"] == "done"
    assert w.store.verify_integrity() == []
    assert w.store.verify_chain() in (None, True, [], ())
    w.close()


def test_close_with_tampered_batch_file_refused_and_rolled_back(tmp_path):
    w = _world(tmp_path, CLOSES)
    path = w.batch_ref.rsplit("@", 1)[0]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("tampered\n")
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), _close_batch(w), batch_ref=w.batch_ref)
    assert exc.value.msg.startswith(f"op 3 close_on_decision {CLOSE_KEY}: ")
    assert snap(w) == before
    assert w.store._item("demo:X") is None
    w.close()


def test_batch_file_tampered_inside_lock_rolls_back_earlier_ops(tmp_path):
    w = _world(tmp_path, CLOSES)
    real, calls = w.store._read_batch, []

    def flaky(ref):
        calls.append(ref)
        if len(calls) > 1:  # pre-lock read passes, the in-lock re-read does not
            raise Refused("evidence_unverified", "batch file changed")
        return real(ref)

    w.store._read_batch = flaky
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), _close_batch(w), batch_ref=w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert len(calls) == 2
    assert snap(w) == before
    assert w.store._item("demo:X") is None
    w.close()
