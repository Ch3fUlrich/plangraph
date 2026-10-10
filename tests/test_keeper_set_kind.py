"""PG-SET-KIND: router-only ``set_kind`` (open lane -> question | operator_task).

Mutations killed: a non-router caller allowed; a claimed/in_review/done/cancelled item
re-kinded; a target kind other than question/operator_task accepted (gate, goal, decision,
lane, junk); a non-lane source re-kinded; no hash-chained event; the fold ignoring the
event (verify_integrity blind to a raw kind tamper); the operator/wait defaults dropped;
any door (HTTP, MCP, CLI, apply_ops op, apply_batch op) missing or skipping the router role.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from door_util import Doors
from keeper_util import World, defn, to_claimed, to_done, to_in_review
from plangraph import views
from plangraph.keeper import events
from plangraph.keeper.errors import Refused
from plangraph.keeper.mcp import HttpKeeper, serve

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from apply_batch import BatchError, build_request, validate_op  # noqa: E402


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("A"), defn("B"), defn("C"), defn("D"), defn("GATE", kind="gate"),
        defn("DEC", kind="decision", operator=True, wait="answer"),
        defn("G", ["A", "B", "C", "D", "GATE", "DEC"], kind="goal")])
    yield world
    world.close()


@pytest.fixture
def doors(w, tmp_path):
    d = Doors(w, tmp_path)
    yield d
    d.close()


def _row(w, key):
    return w.conn.execute("SELECT kind, operator, wait, state FROM items WHERE key=?",
                          (key,)).fetchone()


def _kind_events(w, key):
    return [e for e in events.read_events(w.conn)
            if e["type"] == "kind_set" and e["key"] == key]


# ------------------------------------------------------------------ store
@pytest.mark.parametrize("kind,wait", [("question", "answer"), ("operator_task", "task")])
def test_router_sets_kind_with_operator_defaults_and_chained_event(w, kind, wait):
    out = w.store.set_kind(w.p("router"), "demo:A", kind)
    assert out == {"key": "demo:A", "kind": kind, "was": "lane"}
    row = _row(w, "demo:A")
    assert (row["kind"], row["operator"], row["wait"], row["state"]) == (kind, 1, wait, "open")
    (event,) = _kind_events(w, "demo:A")
    assert event["payload"] == {"kind": kind, "was": "lane", "operator": True, "wait": wait}
    assert event["actor"] == "router"
    assert events.verify_chain(w.conn) == []
    assert w.store.verify_integrity() == []
    assert "demo:A" in [r["key"] for r in w.store.needs_you(w.p("router"))]


def test_an_explicit_wait_is_kept(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"wait": "task"})
    w.store.set_kind(w.p("router"), "demo:A", "question")
    assert _row(w, "demo:A")["wait"] == "task"
    assert w.store.verify_integrity() == []


@pytest.mark.parametrize("who", ["l1main", "workerA", "reviewer", "reader", "system"])
def test_non_router_is_forbidden_and_writes_nothing(w, who):
    with pytest.raises(Refused) as exc:
        w.store.set_kind(w.p(who), "demo:A", "question")
    assert exc.value.code == "forbidden"
    assert _row(w, "demo:A")["kind"] == "lane" and _kind_events(w, "demo:A") == []


def test_non_open_items_are_refused(w):
    to_in_review(w, "demo:B")                       # claimed -> in_review
    to_claimed(w, "demo:C")
    to_done(w, "demo:D")
    for key, code in (("demo:B", "bad_state"), ("demo:C", "bad_state"),
                      ("demo:D", "locked")):
        with pytest.raises(Refused) as exc:
            w.store.set_kind(w.p("router"), key, "question")
        assert exc.value.code == code, key
        assert _row(w, key)["kind"] == "lane" and _kind_events(w, key) == []


@pytest.mark.parametrize("kind", ["lane", "gate", "goal", "decision", "bogus", "", None, 3])
def test_other_target_kinds_are_refused(w, kind):
    with pytest.raises(Refused) as exc:
        w.store.set_kind(w.p("router"), "demo:A", kind)
    assert exc.value.code == "validation"
    assert _row(w, "demo:A")["kind"] == "lane" and _kind_events(w, "demo:A") == []


@pytest.mark.parametrize("key", ["demo:GATE", "demo:DEC", "demo:G"])
def test_only_lane_items_are_re_kinded(w, key):
    before = _row(w, key)["kind"]
    with pytest.raises(Refused) as exc:
        w.store.set_kind(w.p("router"), key, "question")
    assert exc.value.code == "validation" and _row(w, key)["kind"] == before


def test_second_set_kind_is_refused_not_a_silent_flip(w):
    w.store.set_kind(w.p("router"), "demo:A", "question")
    with pytest.raises(Refused) as exc:
        w.store.set_kind(w.p("router"), "demo:A", "operator_task")
    assert exc.value.code == "validation" and len(_kind_events(w, "demo:A")) == 1


def test_unknown_key_is_not_found(w):
    with pytest.raises(Refused) as exc:
        w.store.set_kind(w.p("router"), "demo:NOPE", "question")
    assert exc.value.code == "not_found"


def test_integrity_sees_a_raw_kind_tamper_and_a_forged_cache(w):
    w.store.set_kind(w.p("router"), "demo:A", "question")
    raw = sqlite3.connect(w.path)
    raw.execute("UPDATE items SET kind='lane' WHERE key='demo:A'")
    raw.commit()
    raw.close()
    assert any(p["code"] == "cache_mismatch" and p["key"] == "demo:A" and p["field"] == "kind"
               for p in w.store.verify_integrity())


def test_integrity_sees_a_raw_wait_tamper(w):
    w.store.set_kind(w.p("router"), "demo:A", "operator_task")
    raw = sqlite3.connect(w.path)
    raw.execute("UPDATE items SET wait='answer' WHERE key='demo:A'")
    raw.commit()
    raw.close()
    assert any(p["field"] == "wait" and p["key"] == "demo:A" for p in w.store.verify_integrity())


def test_fold_replays_the_event():
    events_ = [{"key": "p:X", "type": "item_created", "payload": {"state": "open"}, "actor": "r"},
               {"key": "p:X", "type": "kind_set", "actor": "r",
                "payload": {"kind": "question", "was": "lane", "operator": True, "wait": "answer"}}]
    state = events.fold(events_)["p:X"]
    assert state["kind"] == "question" and state["wait"] == "answer"


def test_the_snapshot_and_views_see_the_new_kind(w):
    w.store.set_kind(w.p("router"), "demo:A", "question")
    item = next(i for i in w.store._all_items() if i["key"] == "demo:A")
    assert item["kind"] == "question"
    assert "demo:A" not in [i["key"] for i in views.ready([item], agent=True)]


# ------------------------------------------------------------------ HTTP
def test_http_door(w, doors):
    status, body = doors.http("l1main", "POST", "/v1/set_kind",
                              {"key": "demo:A", "kind": "question"})
    assert status == 403 and _row(w, "demo:A")["kind"] == "lane"
    status, body = doors.http("router", "POST", "/v1/set_kind",
                              {"key": "demo:A", "kind": "question"})
    assert status == 200 and body["kind"] == "question" and body["was"] == "lane"
    status, body = doors.http("router", "POST", "/v1/set_kind", {"key": "demo:B"})
    assert status == 400                              # kind is required
    status, _ = doors.http("router", "POST", "/v1/set_kind",
                           {"key": "demo:B", "kind": "goal"})
    assert status == 400 and _row(w, "demo:B")["kind"] == "lane"


# ------------------------------------------------------------------ MCP
def test_mcp_door(w, doors):
    out = io.StringIO()
    tok = doors.tmp / "router.mcp.token"
    tok.write_text(w.tok("router"), encoding="utf-8")
    tok.chmod(0o600)
    client = HttpKeeper(doors.url, str(tok), timeout=10)
    serve(io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "set_kind", "arguments": {
                          "key": "demo:A", "kind": "question"}}}) + "\n"), out, client)
    first, second = [json.loads(line) for line in out.getvalue().splitlines()]
    tool = next(t for t in first["result"]["tools"] if t["name"] == "set_kind")
    assert tool["inputSchema"]["required"] == ["key", "kind"]
    assert second["result"]["isError"] is False
    assert _row(w, "demo:A")["kind"] == "question"


# ------------------------------------------------------------------ CLI
def test_cli_door(w, doors):
    code, out, err = doors.pg("l1main", "set-kind", "demo:A", "question")
    assert code != 0 and _row(w, "demo:A")["kind"] == "lane"
    code, out, err = doors.pg("router", "set-kind", "demo:A", "question")
    assert code == 0, err
    assert out.strip() == "demo:A kind lane -> question"
    assert _row(w, "demo:A")["kind"] == "question"
    code, _, _ = doors.pg("router", "set-kind", "demo:B", "goal")
    assert code != 0 and _row(w, "demo:B")["kind"] == "lane"


# ------------------------------------------------------- apply_ops + batch
def test_apply_ops_op_is_atomic_with_the_rest_of_the_batch(w):
    out = w.store.apply_ops(w.p("router"), [
        {"op": "set_kind", "key": "demo:A", "kind": "question"},
        {"op": "set_kind", "key": "demo:B", "kind": "operator_task"}])
    assert out["applied"] == 2
    assert (_row(w, "demo:A")["kind"], _row(w, "demo:B")["kind"]) == ("question", "operator_task")
    assert w.store.verify_integrity() == []
    with pytest.raises(Refused) as exc:   # the second op refuses -> the first rolls back
        w.store.apply_ops(w.p("router"), [
            {"op": "set_kind", "key": "demo:C", "kind": "question"},
            {"op": "set_kind", "key": "demo:D", "kind": "goal"}])
    assert "op 2 set_kind demo:D: validation" in exc.value.msg
    assert _row(w, "demo:C")["kind"] == "lane" and _kind_events(w, "demo:C") == []


def test_apply_ops_shape_checks(w):
    for bad in ({"op": "set_kind", "key": "demo:A"},
                {"op": "set_kind", "key": "demo:A", "kind": "question", "x": 1}):
        with pytest.raises(Refused) as exc:
            w.store.apply_ops(w.p("router"), [bad])
        assert exc.value.code == "validation"


def test_apply_ops_non_router_forbidden(w):
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("l1main"), [{"op": "set_kind", "key": "demo:A", "kind": "question"}])
    assert exc.value.code == "forbidden"


def test_apply_batch_validates_and_builds_the_single_route_request():
    op = {"op": "set_kind", "key": "demo:A", "kind": "question"}
    validate_op(op)
    assert build_request(op, "b.yaml", "0" * 64) == (
        "set_kind", {"key": "demo:A", "kind": "question"})
    for bad in ({"op": "set_kind", "key": "demo:A"},
                {"op": "set_kind", "key": "demo:A", "kind": 3},
                {"op": "set_kind", "key": "demo:A", "kind": "question", "extra": 1}):
        with pytest.raises(BatchError):
            validate_op(bad)
