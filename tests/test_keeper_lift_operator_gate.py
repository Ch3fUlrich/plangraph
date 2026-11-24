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
_RELAY = ("| D-{did} | demo | (06:xxZ, date -u 06:06Z at writing) OPERATOR 2026-11-24T05:59:56.804Z, relayed by "
          "relay-session (cross-session message, human CLI turn, text pinned, sha256 ab12): \"why do I need to "
          "do something for this?\" DECISION: the operator step of {key} (p9) is lifted: an agent applies it. "
          "| {by} | evidence |\n")
HEADER = "| ID | PROJECT | DECISION | BY | DATE |\n| --- | --- | --- | --- | --- |\n"
ROWS = (
    "| D-10 | demo | Gate lifted; Lifts-Gate: demo:GATE; apply now | OPERATOR | 2026-11-24 |\n"
    "| D-11 | demo | Gate lifted; Lifts-Gate: demo:GATE; apply now | ROUTER | 2026-11-24 |\n"
    "| D-12 | demo | Gate lifted; Lifts-Gate: demo:OTHER; apply now | OPERATOR | 2026-11-24 |\n"
    "| D-13 | demo | Gate lifted; Lifts-Gate: demo:GATEWAY; apply now | OPERATOR | 2026-11-24 |\n"
    "| D-14 | demo | Keep demo:GATE blocked until the review is done | OPERATOR | 2026-11-24 |\n"
    "| D-15 | demo | Lifts-Gate: demo:OTHER demo:GATE; Note: both | OPERATOR | 2026-11-24 |\n"
    "| D-16 | demo | Do not add Lifts-Gate: demo:GATE yet | OPERATOR | 2026-11-24 |\n"
    "| D-17 | demo | Lifts-Gate: demo:GATE; apply now | OPERATOR | 2026-11-24 |\n"
    "| D-18 | demo | Supersedes D-17; keep demo:GATE blocked | OPERATOR | 2026-11-25 |\n"
    # the recorded shape of a relayed operator answer: the router's row, the operator's words quoted
    + _RELAY.format(did=20, key="demo:GATE", by="router (sonnet 5.5, rules 2.1A, 3.76)")
    + _RELAY.format(did=21, key="demo:OTHER", by="router (sonnet 5.5, rules 2.1A, 3.76)")
    + _RELAY.format(did=22, key="demo:GATE", by="router (sonnet 5.5, rules 2.1A, 3.76)")
    + "| D-23 | demo | Supersedes D-22; the answer was withdrawn | router (sonnet 5.5) | e |\n"
    + "| D-30 | demo | Lifts-Gate: demo:GATE; Operator-Answer: D-20 | router (sonnet 5.5) | e |\n"
    "| D-31 | demo | Lifts-Gate: demo:GATE; Operator-Answer: D-21 | router (sonnet 5.5) | e |\n"
    "| D-32 | demo | Lifts-Gate: demo:GATE; Operator-Answer: D-11 | router (sonnet 5.5) | e |\n"
    "| D-33 | demo | Lifts-Gate: demo:GATE; Operator-Answer: D-22 | router (sonnet 5.5) | e |\n"
    "| D-34 | demo | Lifts-Gate: demo:GATE | router (sonnet 5.5) | e |\n"
    "| D-35 | demo | Lifts-Gate: Do not lift demo:GATE until reviewed | OPERATOR | 2026-11-24 |\n"
    "| D-36 | demo | Lifts-Gate: demo:GATE; Operator-Answer: D-99 | router (sonnet 5.5) | e |\n"
    # the answer behind an Operator-Answer link must itself release the exact item
    + "| D-40 | demo | (06:xxZ) OPERATOR 2026-11-24T05:59:56.804Z, relayed by relay-session: \"keep it\" DECISION: keep demo:GATE blocked until review | router (sonnet 5.5) | e |\n"
    "| D-41 | demo | Lifts-Gate: demo:GATE; Operator-Answer: D-40 | router (sonnet 5.5) | e |\n"
    "| D-42 | demo | Lifts-Gate: demo:GATE; Operator-Answer: D-14 | router (sonnet 5.5) | e |\n"
    "| D-43 | demo | Lifts-Gate: demo:GATE; apply now | OPERATOR | 2026-11-24 |\n"
    "| D-44 | demo | Lifts-Gate: demo:GATE; Operator-Answer: D-43 | router (sonnet 5.5) | e |\n"
    "| D-37 | demo | Lifts-Gate: demo:GATE demo:NEW; apply now | OPERATOR | 2026-11-24 |\n")


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("GATE", operator=True), defn("GATE2", operator=True), defn("FREE"),
        defn("G", ["GATE", "GATE2", "FREE"], kind="goal")])
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
    ("D-35", "evidence_unverified"),      # prose inside the Lifts-Gate field value
    ("D-34", "evidence_unverified"),      # a router row with no operator answer behind it
    ("D-31", "evidence_unverified"),      # the relayed answer names another item
    ("D-32", "evidence_unverified"),      # Operator-Answer names a router row that is no relay
    ("D-33", "evidence_unverified"),      # the relayed answer was superseded
    ("D-36", "evidence_unverified"),
    ("D-41", "evidence_unverified"),      # a relayed answer that keeps the gate blocked (names the key)
    ("D-42", "evidence_unverified"),      # an OPERATOR row that keeps it blocked, linked as the answer      # Operator-Answer names an absent row
    ("D-16", "evidence_unverified"),      # prose that mentions the token is no release field
    ("D-17", "evidence_unverified"),      # a release a later row supersedes
    ("D-14", "evidence_unverified"),      # an OPERATOR row that names the key but keeps it blocked
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


def test_the_recorded_relay_format_lifts_through_a_structured_router_row(w):
    out = w.store.lift_operator_gate(w.p("router"), KEY, "D-30")
    assert out["decision_id"] == "D-30" and _flags(w) == (False, False)
    (event,) = _lifted(w)
    assert event["payload"] == {"decision_id": "D-30", "operator": False}


def test_an_operator_row_with_a_release_field_is_a_valid_answer_behind_the_link(w):
    assert w.store.lift_operator_gate(w.p("router"), KEY, "D-44")["decision_id"] == "D-44"


def test_a_denying_answer_behind_the_link_refuses_over_http_too(w, doors):
    status, _ = doors.http("router", "POST", "/v1/lift_operator_gate", {"key": KEY, "decision_id": "D-41"})
    assert status == 400 and _flags(w) == (True, True) and _lifted(w) == []


def test_a_release_list_may_name_several_keys(w):
    assert w.store.lift_operator_gate(w.p("router"), KEY, "D-15")["decision_id"] == "D-15"
    assert _flags(w) == (False, False)


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


def _watch_reads(w, monkeypatch):
    seen = []
    real = w.store._read_lift_decisions

    def watch(*args, **kwargs):
        seen.append(w.conn.in_transaction)
        return real(*args, **kwargs)

    monkeypatch.setattr(w.store, "_read_lift_decisions", watch)
    return seen


def test_a_single_lift_reads_the_decision_before_any_lock(w, monkeypatch):
    seen = _watch_reads(w, monkeypatch)
    w.store.lift_operator_gate(w.p("router"), KEY, "D-10")
    assert seen == [False]


def test_a_create_then_lift_batch_reads_once_before_its_transaction(w, monkeypatch):
    """The key an earlier op creates has no row before the batch; the read still precedes BEGIN."""
    seen = _watch_reads(w, monkeypatch)
    w.store.apply_ops(w.p("router"), [
        {"op": "create", "item": defn("NEW", operator=True)},
        {"op": "edit", "key": "demo:G", "fields": {"depends_on": {"add": ["demo:NEW"]}}},
        {"op": "lift_operator_gate", "key": "demo:NEW", "decision_id": "D-37"},
        {"op": "lift_operator_gate", "key": KEY, "decision_id": "D-37"}])   # the same D-id: one read
    assert seen == [False]
    assert _flags(w, "demo:NEW") == (False, False) and _flags(w) == (False, False)
    assert len(_lifted(w)) == 2


def test_a_refused_lift_rolls_the_whole_batch_back(w):
    with pytest.raises(Refused, match="Lifts-Gate"):
        w.store.apply_ops(w.p("router"), [
            {"op": "lift_operator_gate", "key": KEY, "decision_id": "D-10"},
            {"op": "lift_operator_gate", "key": "demo:GATE2", "decision_id": "D-10"}])
    assert _flags(w) == (True, True) and _lifted(w) == []


def test_a_principal_revoked_before_the_write_lifts_nothing(w, monkeypatch):
    from plangraph.keeper import auth
    real = w.store._read_lift_decisions

    def proof_then_revoke(*args, **kwargs):
        out = real(*args, **kwargs)
        auth.revoke(w.conn, "router")
        return out

    monkeypatch.setattr(w.store, "_read_lift_decisions", proof_then_revoke)
    with pytest.raises(Refused) as exc:
        w.store.lift_operator_gate(w.p("router"), KEY, "D-10")
    assert exc.value.code == "forbidden" and _flags(w) == (True, True) and _lifted(w) == []
