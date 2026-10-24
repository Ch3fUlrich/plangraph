import pytest

from keeper_util import World
from plangraph.keeper.errors import Refused
from test_phase_s3d_red import claim_as_runner, gate, set_state


LIVE_STATES = ["open", "ready", "running", "waiting", "needs_input"]


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "keeper")
    yield world
    world.close()


def complete(w, actor="system"):
    return w.store.worker_complete(
        w.p(actor),
        {"card": "demo:C1", "merged_sha": "abcdef1", "expected_state": LIVE_STATES},
    )


def test_system_phase_holder_accepts_real_worker_live_set(w):
    gate(w)
    claim_as_runner(w, "demo:C1")
    complete(w)
    assert w.store._get_or_refuse("demo:C1")["state"] == "in_review"


def complete_with_exec(w, *, run_id="R1", attempt=2, expected_state=LIVE_STATES):
    return w.store.worker_complete(w.p("system"), {
        "card": "demo:C1", "merged_sha": "abcdef1",
        "expected_state": expected_state, "workflow_id": "wf",
        "run_id": run_id, "attempt": attempt,
    })


def test_system_phase_holder_exec_triple_does_not_record_owner(w):
    gate(w)
    claim_as_runner(w, "demo:C1")
    complete_with_exec(w)
    item = w.store._get_or_refuse("demo:C1")
    assert item["state"] == "in_review"
    assert item["exec_principal"] is None
    assert w.store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE key=? AND type='exec_owner_changed'",
        ("demo:C1",),
    ).fetchone()[0] == 0


@pytest.mark.parametrize("run_id,attempt", [("R2", 2), ("R1", 1)])
def test_system_phase_holder_exec_triple_preserves_stale_owner_checks(w, run_id, attempt):
    gate(w)
    w.store.claim(w.p("workerA"), "demo:C1")
    with w.store._write():
        w.store._exec_check(w.store.conn, "demo:C1", "wf", "R1", 2,
                            w.p("workerA"), now=w.clock())
    claim_as_runner(w, "demo:C1")
    with pytest.raises(Refused) as exc:
        complete_with_exec(w, run_id=run_id, attempt=attempt)
    assert exc.value.code == "exec_stale"
    item = w.store._get_or_refuse("demo:C1")
    assert item["state"] == "claimed"
    assert item["exec_principal"] == "workerA"
    assert item["exec_run_id"] == "R1"
    assert item["exec_attempt"] == 2


def test_plain_system_claim_exec_triple_cannot_record_owner(w):
    gate(w)
    claim_as_runner(w, "demo:C1", via=None)
    # Omit the optional state guard so it does not mask owner-recording auth.
    with pytest.raises(Refused) as exc:
        complete_with_exec(w, expected_state=None)
    assert exc.value.code == "forbidden"
    assert w.store._get_or_refuse("demo:C1")["exec_principal"] is None


@pytest.mark.parametrize("case", ["plain_claim", "non_lane", "cancelled_gate", "no_gate",
                                      "router", "workerA", "older_marker", "wrong_epoch",
                                      "wrong_holder"])
def test_live_set_exception_does_not_widen_system_door(w, case):
    gate(w)
    claim_as_runner(w, "demo:C1", via=None if case == "plain_claim" else "worker_claim")
    if case == "non_lane":
        w.store.conn.execute("UPDATE items SET kind='decision' WHERE key='demo:C1'")
    elif case == "cancelled_gate":
        set_state(w, "cancelled", "demo:PH-X")
    elif case == "no_gate":
        w.store.conn.execute("DELETE FROM edges WHERE dst='demo:C1'")
    elif case == "older_marker":
        claim_as_runner(w, "demo:C1", via=None)
    elif case == "wrong_epoch":
        w.store.conn.execute("UPDATE items SET epoch=2 WHERE key='demo:C1'")
    elif case == "wrong_holder":
        w.store.conn.execute("UPDATE items SET claimed_by='workerA' WHERE key='demo:C1'")
    w.store.conn.commit()
    with pytest.raises(Refused) as exc:
        complete(w, case if case in ("router", "workerA") else "system")
    assert exc.value.code == "exec_stale_state"
    assert w.store._get_or_refuse("demo:C1")["state"] == "claimed"


def test_system_phase_holder_expired_lease_is_refused(w):
    gate(w)
    claim_as_runner(w, "demo:C1")
    w.store.conn.execute(
        "UPDATE items SET lease_until='2000-01-01T00:00:00Z' WHERE key='demo:C1'"
    )
    w.store.conn.commit()
    with pytest.raises(Refused) as exc:
        complete(w)
    assert exc.value.code == "epoch_stale"
    assert "claim lease expired" in str(exc.value)
    assert w.store._get_or_refuse("demo:C1")["state"] == "claimed"
