"""LE-KEEPER-OPERATOR-FLAGS-NOT-CLEARABLE: the router lifts an operator gate with a recorded release.

The release is ONE structured fact: the request says ``decision="release"`` and names a decision row whose
whole fields read ``Decision-Kind: release; Item: <this key>; Gate: operator`` and which no later row
retires.  Every other row lifts nothing; the rest of a row's text is stored and never read.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from door_util import Doors
from keeper_util import World, defn
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.events import read_events, verify_integrity

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from apply_batch import BatchError, build_request, validate_op  # noqa: E402

KEY = "demo:GATE"
HEADER = "| ID | PROJECT | DECISION | BY | DATE |\n| --- | --- | --- | --- | --- |\n"
FACT = "Decision-Kind: release; Item: {key}; Gate: operator"
_ATTEST = "(06:xxZ) OPERATOR 2026-11-24T05:59:56.804Z, relayed by relay-session"
#: rows that are NOT the release of demo:GATE (each is linked by its position below); a head that read
#: the caller's value alone lifted every one of them
NOT_RELEASES = {
    "conditional approval": _ATTEST + ': "ok" DECISION: the operator step of demo:GATE is lifted only after review',
    "conditional approval after a colon": _ATTEST + ': "ok" DECISION: the operator step of demo:GATE is lifted: only if review passes',
    "release phrase and a withdrawal": _ATTEST + ': "ok" DECISION: the operator step of demo:GATE is lifted. Withdrawn: keep it blocked',
    "denial": "Keep demo:GATE blocked until the review is done",
    "unrelated decision": "Rotate the review seats on Monday; nothing about gates",
    "another item": FACT.format(key="demo:GATEWAY"),
    "another gate": "Decision-Kind: release; Item: demo:GATE; Gate: review",
    "another kind": "Decision-Kind: keep; Item: demo:GATE; Gate: operator",
    "item with trailing words": "Decision-Kind: release; Item: demo:GATE until reviewed; Gate: operator",
    "fields inside prose": "Do not add Decision-Kind: release; Item: demo:GATE; Gate: operator",
    "field missing": "Decision-Kind: release; Item: demo:GATE",
    "field twice": FACT.format(key="demo:GATE") + "; Item: demo:GATEWAY",
    "release in the quoted words": _ATTEST + ': "Decision-Kind: release; Item: demo:GATE; Gate: operator" DECISION: no',
}
ROWS = (
    "| D-10 | demo | " + FACT.format(key=KEY) + "; Note: the rest is never read | router (sonnet 5.5) | e |\n"
    "| D-11 | demo | " + FACT.format(key="demo:GATE2") + " | router (sonnet 5.5) | e |\n"
    "| D-12 | demo | " + FACT.format(key="demo:GATE2") + " | router (sonnet 5.5) | e |\n"
    "| D-13 | demo | Supersedes D-12; keep demo:GATE2 blocked | router (sonnet 5.5) | e |\n"
    "| D-14 | demo | " + FACT.format(key="demo:NEW") + " | router (sonnet 5.5) | e |\n"
    + "".join(f"| D-{100 + n} | demo | {text} | OPERATOR | 2026-11-24 |\n"
              for n, text in enumerate(NOT_RELEASES.values())))


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


@pytest.fixture
def doors(w, tmp_path):
    d = Doors(w, tmp_path)
    yield d
    d.close()


def _flags(w, key=KEY):
    item = w.store._item(key)
    row = next(r for r in w.store.snapshot(w.p("reader"))["items"] if r["key"] == key)
    return bool(item["operator"]), row["waits_on_operator"]


def _lifted(w):
    return [e for e in read_events(w.conn) if e["type"] == "operator_gate_lifted"]


def _lift(w, did="D-10", decision="release", key=KEY, who="router"):
    return w.store.lift_operator_gate(w.p(who), key, did, decision)


def test_the_router_clears_the_flags_with_the_release_value_and_logs_it(w):
    assert _flags(w) == (True, True)
    out = _lift(w)
    assert out == {"key": KEY, "operator": False, "waits_on_operator": False,
                   "decision_id": "D-10", "decision": "release"}
    assert _flags(w) == (False, False)
    (event,) = _lifted(w)
    assert event["actor"] == "router" and event["key"] == KEY
    assert event["payload"] == {"decision_id": "D-10", "decision": "release", "operator": False}
    assert verify_integrity(w.conn) == []


@pytest.mark.parametrize("decision", [None, "", "approve", "RELEASE", "released", True])
def test_a_request_without_the_release_value_lifts_nothing(w, decision):
    with pytest.raises(Refused, match="decision='release'") as exc:
        _lift(w, "D-10", decision)
    assert exc.value.code == "validation"
    assert _flags(w) == (True, True) and _lifted(w) == []


@pytest.mark.parametrize("n", range(len(NOT_RELEASES)), ids=list(NOT_RELEASES))
def test_a_row_that_is_not_the_release_of_this_item_lifts_nothing(w, n):
    """Conditional approval, withdrawal, unrelated decision, another item/gate/kind: refused with the value set."""
    with pytest.raises(Refused, match="records no release") as exc:
        _lift(w, f"D-{100 + n}")
    assert exc.value.code == "evidence_unverified"
    assert _flags(w) == (True, True) and _lifted(w) == []


def test_a_release_a_later_row_retires_lifts_nothing(w):
    with pytest.raises(Refused, match="retired by D-13"):
        _lift(w, "D-12", key="demo:GATE2")
    assert _flags(w, "demo:GATE2") == (True, True) and _lifted(w) == []
    assert _lift(w, "D-11", key="demo:GATE2")["decision_id"] == "D-11"    # the unretired twin lifts


def test_only_the_whole_fields_are_read(w):
    """D-10 carries a free Note field: the lift reads Decision-Kind, Item and Gate and nothing else."""
    assert _lift(w, "D-10")["decision"] == "release"
    assert _flags(w) == (False, False)


@pytest.mark.parametrize("did,code", [
    ("D-99", "evidence_unverified"), ("10", "validation"), (None, "validation"), ("", "validation"),
    ("D-10 ", "validation")])
def test_a_decision_id_that_is_not_recorded_is_refused(w, did, code):
    with pytest.raises(Refused) as exc:
        _lift(w, did)
    assert exc.value.code == code and _flags(w) == (True, True) and _lifted(w) == []


@pytest.mark.parametrize("who", ["l1main", "workerA", "reviewer", "reader", "system"])
def test_only_the_router_may_lift(w, who):
    with pytest.raises(Refused) as exc:
        _lift(w, who=who)
    assert exc.value.code == "forbidden" and _flags(w) == (True, True)


def test_an_item_without_the_gate_has_nothing_to_lift(w):
    _lift(w)
    with pytest.raises(Refused) as exc:
        _lift(w)
    assert exc.value.code == "bad_state" and len(_lifted(w)) == 1


def test_a_cancelled_item_is_locked(w):
    w.store.cancel(w.p("router"), KEY, "obsolete")
    with pytest.raises(Refused) as exc:
        _lift(w)
    assert exc.value.code == "locked"


def test_an_unreadable_decisions_file_fails_closed(w):
    w.store.ctx = SimpleNamespace(decisions=None)
    with pytest.raises(Refused) as exc:
        _lift(w)
    assert exc.value.code == "evidence_unverified" and _flags(w) == (True, True)


def test_a_key_the_keeper_accepts_lifts_whatever_follows_it(w):
    """Item keys are the keeper's own: a qualified id with a qualifier is just a key."""
    w.store.create_items(w.p("router"), [defn("GATE(A)", operator=True),
                                         defn("G2", ["GATE(A)"], kind="goal")])
    path = w.store.ctx.decisions._path
    path.write_text(path.read_text(encoding="utf-8") + "| D-20 | demo | " + FACT.format(key="demo:GATE(A)")
                    + " | router | e |\n", encoding="utf-8")
    assert _lift(w, "D-20", key="demo:GATE(A)")["key"] == "demo:GATE(A)"
    assert _flags(w, "demo:GATE(A)") == (False, False)
    with pytest.raises(Refused) as exc:
        _lift(w, "D-20", key="demo:GATE(B)")
    assert exc.value.code in ("evidence_unverified", "not_found")


def test_the_recorded_d6753_row_is_prose_that_alone_lifts_nothing(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "decisions" / "d6753-operator-relay-row.md"
    recorded = fixture.read_text(encoding="utf-8")
    assert recorded.startswith("| D-6753 | plangraph | (06:xxZ") and "(p9, D-4743 option (a)) is lifted:" in recorded
    key = "plangraph:OG-MEM-SCHEMA-APPLY"
    world = World(tmp_path)
    try:
        world.store.create_items(world.p("router"), [
            defn("OG-MEM-SCHEMA-APPLY", project="plangraph", operator=True),
            defn("G", ["OG-MEM-SCHEMA-APPLY"], project="plangraph", kind="goal")])
        log = tmp_path / "decisions-log.md"
        log.write_text("| id | project | decision | by | why / evidence |\n|---|---|---|---|---|\n" + recorded
                       + f"| D-9000 | plangraph | {FACT.format(key=key)}; Relays: D-6753 | router (sonnet 5.5) | e |\n",
                       encoding="utf-8")
        world.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(log))
        for decision in (None, "release"):    # the recorded prose alone: no value, or the value with prose behind it
            with pytest.raises(Refused):
                world.store.lift_operator_gate(world.p("router"), key, "D-6753", decision)
        assert world.store._item(key)["operator"] == 1
        assert world.store.lift_operator_gate(world.p("router"), key, "D-9000", "release")["operator"] is False
        assert world.store._item(key)["operator"] == 0
    finally:
        world.close()


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
    _lift(w)
    assert seen == [False]


def test_a_create_then_lift_batch_reads_once_before_its_transaction(w, monkeypatch):
    """The key an earlier op creates has no row before the batch; the read still precedes BEGIN."""
    seen = _watch_reads(w, monkeypatch)
    w.store.apply_ops(w.p("router"), [
        {"op": "create", "item": defn("NEW", operator=True)},
        {"op": "edit", "key": "demo:G", "fields": {"depends_on": {"add": ["demo:NEW"]}}},
        {"op": "lift_operator_gate", "key": "demo:NEW", "decision_id": "D-14", "decision": "release"},
        {"op": "lift_operator_gate", "key": KEY, "decision_id": "D-10", "decision": "release"}])
    assert seen == [False]                          # both ids in one read, before the transaction
    assert _flags(w, "demo:NEW") == (False, False) and _flags(w) == (False, False)
    assert len(_lifted(w)) == 2


def test_a_refused_lift_rolls_the_whole_batch_back(w):
    with pytest.raises(Refused, match="decision='release'"):
        w.store.apply_ops(w.p("router"), [
            {"op": "lift_operator_gate", "key": KEY, "decision_id": "D-10", "decision": "release"},
            {"op": "lift_operator_gate", "key": "demo:GATE2", "decision_id": "D-10"}])
    assert _flags(w) == (True, True) and _lifted(w) == []


def test_a_principal_revoked_before_the_write_lifts_nothing(w, monkeypatch):
    real = w.store._read_lift_decisions

    def read_then_revoke(*args, **kwargs):
        out = real(*args, **kwargs)
        auth.revoke(w.conn, "router")
        return out

    monkeypatch.setattr(w.store, "_read_lift_decisions", read_then_revoke)
    with pytest.raises(Refused) as exc:
        _lift(w)
    assert exc.value.code == "forbidden" and _flags(w) == (True, True) and _lifted(w) == []


def test_a_granted_l1_cannot_send_the_op_and_the_router_can(w):
    op = {"op": "lift_operator_gate", "key": KEY, "decision_id": "D-10", "decision": "release"}
    w.store.grant_apply(w.p("router"), "l1main", "demo")
    with pytest.raises(Refused, match="router-only"):
        w.store.apply_ops(w.p("l1main"), [op])
    w.store.apply_ops(w.p("router"), [op])
    assert _flags(w) == (False, False) and len(_lifted(w)) == 1


def test_the_fold_replays_the_lift(w):
    _lift(w)
    w.conn.execute("UPDATE items SET operator=1 WHERE key=?", (KEY,))
    w.conn.commit()
    assert any(p.get("field") == "operator" for p in verify_integrity(w.conn))


def test_http_door(w, doors):
    body = {"key": KEY, "decision_id": "D-10", "decision": "release"}
    status, _ = doors.http("l1main", "POST", "/v1/lift_operator_gate", body)
    assert status == 403 and _flags(w) == (True, True)
    for bad in ({"key": KEY, "decision_id": "D-10"}, dict(body, decision="approve"), dict(body, extra=1),
                dict(body, decision_id="D-99")):
        status, _ = doors.http("router", "POST", "/v1/lift_operator_gate", bad)
        assert status == 400 and _flags(w) == (True, True), bad
    status, _ = doors.http("l1main", "POST", "/v1/lift_operator_gate", dict(body, extra=1))
    assert status == 403
    status, out = doors.http("router", "POST", "/v1/lift_operator_gate", body)
    assert status == 200 and out["decision"] == "release" and out["operator"] is False
    assert _flags(w) == (False, False) and len(_lifted(w)) == 1


def test_cli_door(w, doors):
    code, _, _ = doors.pg("l1main", "lift-operator-gate", KEY, "D-10", "--decision", "release")
    assert code != 0 and _flags(w) == (True, True)
    with pytest.raises(SystemExit):                                              # the value is required
        doors.pg("router", "lift-operator-gate", KEY, "D-10")
    assert _flags(w) == (True, True)
    code, _, _ = doors.pg("router", "lift-operator-gate", KEY, "D-10", "--decision", "approve")
    assert code != 0 and _flags(w) == (True, True)
    code, out, err = doors.pg("router", "lift-operator-gate", KEY, "D-10", "--decision", "release")
    assert code == 0, err
    assert out.strip() == f"{KEY} operator gate lifted by D-10"
    assert _flags(w) == (False, False)


def test_the_batch_file_op_is_validated_and_sent_to_the_single_route():
    op = {"op": "lift_operator_gate", "key": KEY, "decision_id": "D-10", "decision": "release"}
    validate_op(op)
    assert build_request(op, "b.yaml", "0" * 64) == (
        "lift_operator_gate", {"key": KEY, "decision_id": "D-10", "decision": "release"})
    for bad in ({"op": "lift_operator_gate", "key": KEY, "decision_id": "D-10"},
                {"op": "lift_operator_gate", "key": KEY, "decision": "release"},
                {"op": "lift_operator_gate", "key": KEY, "decision_id": 10, "decision": "release"},
                dict(op, extra=1)):
        with pytest.raises(BatchError):
            validate_op(bad)
