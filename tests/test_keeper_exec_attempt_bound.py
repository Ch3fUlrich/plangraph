"""Exec attempts fit a signed 32-bit database integer at both entry points."""
import pytest

from door_util import Doors
from keeper_util import World, seed, seed_exec_owner
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


@pytest.fixture
def doors(w, tmp_path):
    live = Doors(w, tmp_path)
    try:
        yield live
    finally:
        live.close()


@pytest.mark.parametrize("attempt", [2**70, 2**31, 0, -1, True, "3"])
def test_set_state_refuses_invalid_attempt(w, attempt):
    key, = seed(w, [("A", [])])
    with pytest.raises(Refused) as info:
        w.store.set_state(w.p("router"), key, "running",
                          run=("wf-1", "run-1", attempt))
    assert info.value.code == "validation"
    row = w.conn.execute("SELECT state, exec_attempt FROM items WHERE key=?",
                         (key,)).fetchone()
    assert tuple(row) == ("open", None)


@pytest.mark.parametrize("attempt", [2**70, 2**31, 0, -1, True, "3"])
def test_worker_park_refuses_invalid_attempt(doors, w, attempt):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "running")
    status, body = doors.http("workerA", "POST", "/v1/park", {
        "card": key, "tier": "free", "exit_class": "check_fail",
        "check_tail": "boom", "workflow_id": "wf-1", "run_id": "run-1",
        "attempt": attempt,
    })
    assert (status, body["error"]) == (400, "validation")
    row = w.conn.execute("SELECT state, exec_attempt FROM items WHERE key=?",
                         (key,)).fetchone()
    assert tuple(row) == ("running", None)


@pytest.mark.parametrize("door", ["set_state", "park"])
def test_maximum_attempt_is_accepted(doors, w, door):
    key, = seed(w, [("A", [])])
    if door == "set_state":
        # Only a worker may RECORD an owner (V2-1), so the run starts owned at
        # attempt 1; the router run-carrying move then advances it to the
        # signed 32-bit maximum, which is what this bound pins.
        seed_exec_owner(w, key, ("wf-1", "run-1", 1))
        w.store.set_state(w.p("router"), key, "running",
                          run=("wf-1", "run-1", 2**31 - 1))
    else:
        w.store.set_state(w.p("router"), key, "running")
        assert doors.http("workerA", "POST", "/v1/park", {
            "card": key, "tier": "free", "exit_class": "check_fail",
            "check_tail": "boom", "workflow_id": "wf-1", "run_id": "run-1",
            "attempt": 2**31 - 1,
        }) == (204, "")
    row = w.conn.execute("SELECT exec_attempt FROM items WHERE key=?",
                         (key,)).fetchone()
    assert row[0] == 2**31 - 1
