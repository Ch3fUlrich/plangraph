"""B2: L5 Closes: fail-closed, the ``priority`` batch op and the seat/earlier lows."""
import pytest

from keeper_util import World, defn, seed
from plangraph.keeper.errors import Refused
from test_keeper_apply_ops import create, link_goal, snap
from test_keeper_close import DID, KEY, _world


def _row(cell):
    return f"| {DID} | note | {cell} |\n"


# ------------------------------------------------------ L5: Closes: fail-closed
@pytest.mark.parametrize("cell", ["Closes: demo: A", "Closes:", "Closes: ,",
                                  "Closes: ,  ,", "Closes: demo:"])
def test_closes_keyword_without_valid_token_fails_closed(tmp_path, cell):
    w = _world(tmp_path, _row(cell))
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert w.store._item(KEY)["state"] != "done"
    w.close()


def test_valid_closes_token_closes(tmp_path):
    w = _world(tmp_path, _row("Closes: demo:A"))
    assert w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)["state"] == "done"
    w.close()


def test_no_closes_keyword_batch_ref_authorises(tmp_path):
    w = _world(tmp_path, f"| {DID} | recorded, no token |\n")
    assert w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)["state"] == "done"
    w.close()


def test_fail_closed_also_inside_apply_ops(tmp_path):
    w = _world(tmp_path, _row("Closes:"))
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [
            create("X"), link_goal("A", "X"),
            {"op": "close_on_decision", "key": KEY}], batch_ref=w.batch_ref)
    assert exc.value.code == "evidence_unverified" and snap(w) == before
    w.close()


# ------------------------------------------------- B1c: the priority batch op
@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


def prio(key, n):
    return {"op": "priority", "key": key, "priority": n}


def test_priority_op_works_inside_a_batch(w):
    out = w.store.apply_ops(w.p("router"), [create("X"), link_goal("A", "X"),
                                            prio("demo:X", 7), prio("demo:A", 2)])
    assert out["applied"] == 4 and out["results"][2] == {"key": "demo:X",
                                                          "priority": 7}
    assert w.store._item("demo:X")["priority"] == 7
    assert w.store._item("demo:A")["priority"] == 2
    assert w.store.verify_integrity() == []


def test_priority_op_rolls_back_with_the_batch(w):
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [
            prio("demo:A", 9), {"op": "cancel", "key": "demo:NOPE"}])
    assert exc.value.msg.startswith("op 2 cancel demo:NOPE: not_found ")
    assert snap(w) == before and w.store._item("demo:A")["priority"] == 0


def test_priority_op_non_router_forbidden(w):
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("l1main"), [prio("demo:A", 1)])
    assert exc.value.code == "forbidden" and snap(w) == before


@pytest.mark.parametrize("bad", ["x", True, 1.5, None])
def test_priority_op_shape_refused(w, bad):
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [prio("demo:A", bad)])
    assert exc.value.code == "validation"
    assert exc.value.msg.startswith("op 1 priority demo:A: validation ")


# ------------------------------------- apply_batch: priority op, usage, replies
import io  # noqa: E402

from test_apply_batch import (  # noqa: E402,F401
    apply_batch, ops_for, run, srv, state_of, token_file, world,
    write_batch)


def _prio_ops():
    ops = ops_for()[:2]
    return ops + [{"op": "priority", "key": "demo:B", "priority": 5}]


def _priority_of(world, key):
    return world.conn.execute("SELECT priority FROM items WHERE key=?",
                              (key,)).fetchone()[0]


@pytest.mark.parametrize("extra", [(), ("--per-op",)])
def test_batch_priority_op_applies(world, srv, token_file, tmp_path, extra):
    rc, out, err = run(write_batch(tmp_path, ops=_prio_ops()), srv.base_url,
                       token_file, extra=extra)
    assert rc == 0, out + err
    assert "ok 3 priority demo:B" in out and "applied 3 of 3" in out
    assert _priority_of(world, "demo:B") == 5


@pytest.mark.parametrize("bad", ["x", True, None])
def test_dry_run_validates_priority_op(world, srv, token_file, tmp_path, bad):
    ops = _prio_ops()
    ops[2]["priority"] = bad
    rc, out, err = run(write_batch(tmp_path, ops=ops), srv.base_url,
                       token_file, extra=["--dry-run"])
    assert rc == 1 and "op 3 priority demo:B: error" in out
    rc, out, err = run(write_batch(tmp_path, ops=_prio_ops()), srv.base_url,
                       token_file, extra=["--dry-run"])
    assert rc == 0 and "plan 3 priority demo:B" in out


def test_priority_op_rolls_back_in_default_mode(world, srv, token_file,
                                                tmp_path):
    ops = _prio_ops() + [{"op": "cancel", "key": "demo:NOPE"}]
    rc, out, err = run(write_batch(tmp_path, ops=ops), srv.base_url, token_file)
    assert rc == 1 and "op 4 cancel demo:NOPE: not_found" in out
    assert world.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


@pytest.mark.parametrize("extra", [(), ("--per-op",)])
def test_unparseable_2xx_reply_exits_non_zero(world, srv, token_file, tmp_path,
                                              monkeypatch, extra):
    monkeypatch.setattr(apply_batch.Session, "post",
                        lambda self, verb, body: b"<html>ok</html>")
    rc, out, err = run(write_batch(tmp_path), srv.base_url, token_file,
                       extra=extra)
    assert rc != 0 and "applied 6 of 6" not in out


def test_short_reply_exits_non_zero(world, srv, token_file, tmp_path,
                                    monkeypatch):
    monkeypatch.setattr(apply_batch.Session, "post",
                        lambda self, verb, body: b'{"applied": 2}')
    rc, out, err = run(write_batch(tmp_path), srv.base_url, token_file)
    assert rc != 0 and "applied 6 of 6" not in out


def test_usage_lists_allow_no_whoami():
    out = io.StringIO()
    apply_batch.print_help(out)
    assert "--allow-no-whoami" in out.getvalue()
