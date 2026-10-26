"""RT-P1-EXEC-OWNERSHIP: reaping and delayed retries respect exec ownership.

Temporal owns running, waiting and needs_input cards: age must not
let the reaper reset them.  A lapsed claimed card is the positive control, so
disabling reaping cannot satisfy the regression.  A delayed worker park must
also leave a newer projector state intact, for both an older attempt and a
stale run.  Driven through the HTTP door like the worker; portable: runs on
the default backend (``World`` follows ``PG_KEEPER_BACKEND``).
"""
import pytest

from door_util import Doors
from keeper_util import World, seed, seed_exec_owner, to_claimed
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


def refused(code, fn, *args, **kw):
    with pytest.raises(Refused) as info:
        fn(*args, **kw)
    assert info.value.code == code, info.value.msg
    return info.value


def snapshot(w, key):
    row = w.conn.execute(
        "SELECT state, claimed_by, exec_workflow_id, exec_run_id, exec_attempt"
        " FROM items WHERE key=?", (key,)).fetchone()
    return (row[0], row[1], row[2], row[3], row[4])


def test_reaper_leaves_temporal_owned_cards_alone(w):
    states = ("running", "waiting", "needs_input")
    keys = seed(w, [(state, []) for state in states] + [("control", [])])
    before = {}
    for key, state in zip(keys, states):
        # Only a worker may RECORD an owner (V2-1); the run-carrying router
        # move below checks the seeded triple instead of planting it.
        seed_exec_owner(w, key, ("wf-1", "run-1", 2))
        w.store.set_state(w.p("router"), key, state, run=("wf-1", "run-1", 2))
        before[key] = snapshot(w, key)
        assert before[key][0] == state
        assert before[key][2:] == ("wf-1", "run-1", 2)
    control = keys[-1]
    to_claimed(w, control)

    reaped = w.store.reap(w.p("system"), w.clock.advance(365 * 24 * 3600))

    for key in keys[:-1]:
        assert snapshot(w, key) == before[key]
        assert key not in reaped["expired"]
    assert reaped["expired"] == [control]
    assert snapshot(w, control) == ("open", None, None, None, None)


def test_reaper_skips_a_lapsed_claim_with_a_live_exec_owner(w):
    # sq12 finding 3: a lapsed lease on a claimed card must not reset it while
    # an exec owner is recorded -- the Temporal side owns that clock, and a
    # reap reset would let a second run claim the card.  The ownerless claim
    # is the positive control, so disabling reaping cannot satisfy this.
    key, control = seed(w, [("A", []), ("B", [])])
    to_claimed(w, key)
    seed_exec_owner(w, key, ("wf-1", "run-1", 2))
    to_claimed(w, control)
    before = snapshot(w, key)

    reaped = w.store.reap(w.p("system"), w.clock.advance(365 * 24 * 3600))

    assert reaped["expired"] == [control]
    assert key not in reaped["expired"]
    assert snapshot(w, key) == before
    assert before[0] == "claimed"
    assert before[2:] == ("wf-1", "run-1", 2)
    assert snapshot(w, control) == ("open", None, None, None, None)


@pytest.mark.parametrize("run_id,attempt", [("run-1", 1), ("stale-run", 2)])
def test_delayed_worker_park_cannot_overwrite_projector(doors, w, run_id, attempt):
    key, = seed(w, [("A", [])])
    seed_exec_owner(w, key, ("wf-1", "run-1", 2))
    w.store.set_state(w.p("router"), key, "waiting", run=("wf-1", "run-1", 2))
    before = snapshot(w, key)
    body = {"card": key, "tier": "free", "exit_class": "check_fail",
            "check_tail": "boom", "workflow_id": "wf-1",
            "run_id": run_id, "attempt": attempt}

    status, response = doors.http("workerA", "POST", "/v1/park", body)

    assert (status, response["error"]) == (409, "exec_stale")
    assert snapshot(w, key) == before
    assert before[0] == "waiting"
    assert before[2:] == ("wf-1", "run-1", 2)
