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
