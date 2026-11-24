"""LE-KEEPER-OPERATOR-FLAGS-NOT-CLEARABLE: the router lifts an operator gate on a named D-id."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from door_util import Doors
from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.events import read_events, verify_integrity

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from apply_batch import BatchError, build_request, validate_op  # noqa: E402

KEY = "demo:GATE"
HEADER = "| ID | PROJECT | DECISION | BY | DATE |\n| --- | --- | --- | --- | --- |\n"
ROWS = (
    "| D-10 | demo | Gate lifted: apply demo:GATE now | OPERATOR | 2026-11-24 |\n"
    "| D-11 | demo | Gate lifted: apply demo:GATE now | ROUTER | 2026-11-24 |\n"
    "| D-12 | demo | Gate lifted: apply demo:OTHER now | OPERATOR | 2026-11-24 |\n"
    "| D-13 | demo | Gate lifted: apply demo:GATEWAY now | OPERATOR | 2026-11-24 |\n")


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("GATE", operator=True), defn("FREE"), defn("G", ["GATE", "FREE"], kind="goal")])
    path = tmp_path / "DECISIONS.md"
    path.write_text(HEADER + ROWS, encoding="utf-8")
    world.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(path))
    yield world
    world.close()


def _flags(w, key=KEY):
    item = w.store._item(key)
    row = next(r for r in w.store.snapshot(w.p("reader"))["items"] if r["key"] == key)
    return bool(item["operator"]), row["waits_on_operator"]


def _lifted(w):
    return [e for e in read_events(w.conn) if e["type"] == "operator_gate_lifted"]


def test_the_router_clears_the_flags_with_an_operator_decision_and_logs_it(w):
    assert _flags(w) == (True, True)
    out = w.store.lift_operator_gate(w.p("router"), KEY, "D-10")
    assert out == {"key": KEY, "operator": False, "waits_on_operator": False, "decision_id": "D-10"}
    assert _flags(w) == (False, False)
    (event,) = _lifted(w)
    assert event["actor"] == "router" and event["key"] == KEY
    assert event["payload"] == {"decision_id": "D-10", "operator": False}
    assert verify_integrity(w.conn) == []


@pytest.mark.parametrize("did,code", [
    ("D-11", "evidence_unverified"),      # a router row is no operator answer
    ("D-12", "evidence_unverified"),      # an operator row that names another item
    ("D-13", "evidence_unverified"),      # a longer key is not this key
    ("D-99", "evidence_unverified"),      # absent from the decisions file
    ("10", "validation"), (None, "validation"), ("", "validation")])
def test_it_is_refused_without_an_operator_decision_naming_the_key(w, did, code):
    with pytest.raises(Refused) as exc:
        w.store.lift_operator_gate(w.p("router"), KEY, did)
    assert exc.value.code == code
    assert _flags(w) == (True, True) and _lifted(w) == []


@pytest.mark.parametrize("who", ["l1main", "workerA", "reviewer", "reader", "system"])
def test_only_the_router_may_lift(w, who):
    with pytest.raises(Refused) as exc:
        w.store.lift_operator_gate(w.p(who), KEY, "D-10")
    assert exc.value.code == "forbidden"
    assert _flags(w) == (True, True)


def test_an_item_without_the_gate_has_nothing_to_lift(w):
    w.store.lift_operator_gate(w.p("router"), KEY, "D-10")
    with pytest.raises(Refused) as exc:
        w.store.lift_operator_gate(w.p("router"), KEY, "D-10")
    assert exc.value.code == "bad_state" and len(_lifted(w)) == 1


def test_a_done_item_is_locked(w):
    w.store.cancel(w.p("router"), KEY, "obsolete")
    with pytest.raises(Refused) as exc:
        w.store.lift_operator_gate(w.p("router"), KEY, "D-10")
    assert exc.value.code == "locked"


def test_an_unreadable_decisions_file_fails_closed(w):
    w.store.ctx = SimpleNamespace(decisions=None)
    with pytest.raises(Refused) as exc:
        w.store.lift_operator_gate(w.p("router"), KEY, "D-10")
    assert exc.value.code == "evidence_unverified" and _flags(w) == (True, True)


def test_the_batch_op_lifts_it_and_a_granted_l1_cannot_send_it(w):
    w.store.grant_apply(w.p("router"), "l1main", "demo")
    with pytest.raises(Refused, match="router-only"):
        w.store.apply_ops(w.p("l1main"), [{"op": "lift_operator_gate", "key": KEY, "decision_id": "D-10"}])
    with pytest.raises(Refused, match="decision_id"):
        w.store.apply_ops(w.p("router"), [{"op": "lift_operator_gate", "key": KEY}])
    w.store.apply_ops(w.p("router"), [{"op": "lift_operator_gate", "key": KEY, "decision_id": "D-10"}])
    assert _flags(w) == (False, False) and len(_lifted(w)) == 1


def test_the_fold_replays_the_lift(w):
    w.store.lift_operator_gate(w.p("router"), KEY, "D-10")
    w.conn.execute("UPDATE items SET operator=1 WHERE key=?", (KEY,))
    w.conn.commit()
    assert any(p.get("field") == "operator" for p in verify_integrity(w.conn))


@pytest.fixture
def doors(w, tmp_path):
    d = Doors(w, tmp_path)
    yield d
    d.close()


def test_http_door(w, doors):
    body = {"key": KEY, "decision_id": "D-10"}
    status, _ = doors.http("l1main", "POST", "/v1/lift_operator_gate", body)
    assert status == 403 and _flags(w) == (True, True)
    status, _ = doors.http("router", "POST", "/v1/lift_operator_gate", {"key": KEY})
    assert status == 400 and _flags(w) == (True, True)       # decision_id is required
    status, _ = doors.http("router", "POST", "/v1/lift_operator_gate", dict(body, extra=1))
    assert status == 400 and _flags(w) == (True, True)       # parity with apply_ops/batch
    status, _ = doors.http("l1main", "POST", "/v1/lift_operator_gate", dict(body, extra=1))
    assert status == 403
    status, _ = doors.http("router", "POST", "/v1/lift_operator_gate", dict(body, decision_id="D-11"))
    assert status == 400 and _flags(w) == (True, True)       # a ROUTER row is no operator answer
    status, out = doors.http("router", "POST", "/v1/lift_operator_gate", body)
    assert status == 200 and out["decision_id"] == "D-10" and out["operator"] is False
    assert _flags(w) == (False, False) and len(_lifted(w)) == 1


def test_cli_door(w, doors):
    code, _, _ = doors.pg("l1main", "lift-operator-gate", KEY, "D-10")
    assert code != 0 and _flags(w) == (True, True)
    code, _, _ = doors.pg("router", "lift-operator-gate", KEY, "D-11")
    assert code != 0 and _flags(w) == (True, True)
    code, out, err = doors.pg("router", "lift-operator-gate", KEY, "D-10")
    assert code == 0, err
    assert out.strip() == f"{KEY} operator gate lifted by D-10"
    assert _flags(w) == (False, False)


def test_the_batch_file_op_is_validated_and_sent_to_the_single_route():
    op = {"op": "lift_operator_gate", "key": KEY, "decision_id": "D-10"}
    validate_op(op)
    assert build_request(op, "b.yaml", "0" * 64) == (
        "lift_operator_gate", {"key": KEY, "decision_id": "D-10"})
    for bad in ({"op": "lift_operator_gate", "key": KEY},
                {"op": "lift_operator_gate", "key": KEY, "decision_id": 10},
                dict(op, extra=1)):
        with pytest.raises(BatchError):
            validate_op(bad)
