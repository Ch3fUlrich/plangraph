"""Observed-state CAS fences delayed duplicates within the same exec attempt."""
import pytest

from door_util import Doors
from keeper_util import World, seed, seed_exec_owner, to_claimed
from test_keeper_exec_verbs import RUN1, complete, owner, park, state

#: The worker's live set: ``waiting`` is in it because the projector writes
#: waiting after usage_limit/outage and on a timer.  Under EXEC-OBSERVED-STATE
#: it is a no-observation FALLBACK: it never stands in for the state a run
#: observed (see ``test_live_set_is_not_a_park_observation``).
LIVE_SET = ["open", "ready", "running", "needs_input", "waiting"]


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


def waiting(w):
    key, = seed(w, [("A", [])])
    # Projector._apply_state does not yet forward exec ids in this checkout.
    run = ("wf-1", "run-1", 1)
    # Only a worker may RECORD an owner (V2-1): seed it, then let the router
    # run-carrying moves below only check the triple.
    seed_exec_owner(w, key, run)
    w.store.set_state(w.p("router"), key, "running", run=run)
    w.clock.advance(10)
    w.store.set_state(w.p("router"), key, "waiting", run=run)
    return key


@pytest.mark.parametrize("verb,body", [("park", park), ("complete", complete)])
@pytest.mark.parametrize("attempt", [1, 2])
# "running" and the live set as it stood before waiting joined it: both
# exclude the observed waiting state, so both are still refused.
@pytest.mark.parametrize("expected", ["running", ["open", "ready", "running", "needs_input"]])
def test_delayed_verb_preserves_projected_state_owner_and_timestamp(w, doors, verb, body, attempt, expected):
    key = waiting(w)
    before = dict(w.store._item(key))
    w.clock.advance(10)
    ids = dict(RUN1, attempt=attempt)
    status, response = doors.http("workerA", "POST", f"/v1/{verb}",
                                  body(key, expected_state=expected, **ids))
    assert (status, response["error"]) == (409, "exec_stale_state")
    assert state(w, key) == "waiting"
    assert owner(w, key) == ("wf-1", "run-1", 1)
    assert dict(w.store._item(key)) == before


@pytest.mark.parametrize("verb,body", [("park", park), ("complete", complete)])
def test_stale_state_cannot_acquire_first_owner(w, doors, verb, body):
    key, = seed(w, [("A", [])])
    before = dict(w.store._item(key))
    status, response = doors.http("workerA", "POST", f"/v1/{verb}",
                                  body(key, expected_state="running", **RUN1))
    assert (status, response["error"]) == (409, "exec_stale_state")
    assert owner(w, key) == (None, None, None)
    assert dict(w.store._item(key)) == before


@pytest.mark.parametrize("verb,body", [("park", park), ("complete", complete)])
@pytest.mark.parametrize("expected", [{}, {"expected_state": None},
                                      {"expected_state": "waiting"}])
def test_matching_or_absent_expected_state_preserves_existing_behavior(w, doors, verb, body, expected):
    key = waiting(w)
    assert doors.http("workerA", "POST", f"/v1/{verb}",
                      body(key, **expected, **RUN1)) == (204, "")
    assert state(w, key) == ("parked" if verb == "park" else "waiting")
    assert owner(w, key) == ("wf-1", "run-1", 1)


@pytest.mark.parametrize("verb,body", [("park", park), ("complete", complete)])
def test_live_set_is_not_a_park_observation(w, doors, verb, body):
    """EXEC-OBSERVED-STATE: the live set says "I observed nothing".

    A park closes the card, so onto the ``waiting`` a projector event wrote
    while this run was away it needs that state as an observation; a complete
    does not move the card and keeps the sq5 live-set CAS it shipped with.
    """
    key = waiting(w)
    before = dict(w.store._item(key))
    status, response = doors.http("workerA", "POST", f"/v1/{verb}",
                                  body(key, expected_state=LIVE_SET, **RUN1))
    if verb == "complete":
        assert (status, response) == (204, "")
        assert state(w, key) == "waiting"
        assert owner(w, key) == ("wf-1", "run-1", 1)
        return
    assert (status, response["error"]) == (409, "exec_stale_state")
    assert dict(w.store._item(key)) == before
    # The same run, having OBSERVED waiting, parks there: the wait-cap park
    # after usage_limit/outage still lands.
    assert doors.http("workerA", "POST", "/v1/park",
                      body(key, expected_state="waiting", **RUN1)) == (204, "")
    assert state(w, key) == "parked"
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_worker_reads_the_observation_it_parks_with(w, doors):
    """EXEC-OBSERVED-STATE's read side: a worker token may observe the state.

    ``GET /v1/show?key=<card>&fmt=json`` is the seam ``Keeper::card_state``
    reads (rust/worker/src/keeper.rs); the state it answers with is the
    ``expected_state`` park and ask send when they could read one, while a run
    that read nothing keeps the live-set fallback.
    """
    key = waiting(w)
    status, info = doors.http("workerA", "GET", f"/v1/show?key={key}&fmt=json")
    assert (status, info["state"]) == (200, "waiting")
    # The fallback alone loses the park on the projector's waiting card ...
    status, response = doors.http("workerA", "POST", "/v1/park",
                                  park(key, expected_state=LIVE_SET, **RUN1))
    assert (status, response["error"]) == (409, "exec_stale_state")
    # ... the observation that same run just read wins it back.
    assert doors.http("workerA", "POST", "/v1/park",
                      park(key, expected_state=info["state"], **RUN1)) == (204, "")
    assert state(w, key) == "parked"


@pytest.mark.parametrize("verb,body", [("park", park), ("complete", complete)])
@pytest.mark.parametrize("expected", ["bogus", "claimed", 1, True, [], {},
                                      ["running", True], ["running", "bogus"]])
def test_invalid_expected_state_is_validation(w, doors, verb, body, expected):
    key = waiting(w)
    before = dict(w.store._item(key))
    status, response = doors.http("workerA", "POST", f"/v1/{verb}",
                                  body(key, expected_state=expected, **RUN1))
    assert (status, response["error"]) == (400, "validation")
    assert dict(w.store._item(key)) == before


@pytest.mark.parametrize("verb,body", [("park", park), ("complete", complete)])
def test_any_of_expected_state(w, doors, verb, body):
    key = waiting(w)
    assert doors.http("workerA", "POST", f"/v1/{verb}", body(
        key, expected_state=["running", "waiting"], **RUN1)) == (204, "")


@pytest.mark.parametrize("verb,body", [("park", park), ("complete", complete)])
def test_matching_state_does_not_bypass_exec_identity(w, doors, verb, body):
    key = waiting(w)
    before = dict(w.store._item(key))
    status, response = doors.http("workerA", "POST", f"/v1/{verb}",
                                  body(key, expected_state="waiting",
                                       workflow_id="wf-2", run_id="run-2", attempt=1))
    assert (status, response["error"]) == (409, "exec_stale")
    assert dict(w.store._item(key)) == before


@pytest.mark.parametrize("verb,body", [("park", park), ("complete", complete)])
@pytest.mark.parametrize("left", ["parked", "failed", "done"])
def test_live_set_refuses_a_card_that_left_the_live_states(w, doors, verb, body, left):
    key, = seed(w, [("A", [])])
    if left == "done":                       # done has its own verb, not set_state
        w.conn.execute("UPDATE items SET state=? WHERE key=?", (left, key))
        w.conn.commit()
    else:
        w.store.set_state(w.p("router"), key, left)
    before = dict(w.store._item(key))
    status, response = doors.http("workerA", "POST", f"/v1/{verb}",
                                  body(key, expected_state=LIVE_SET, **RUN1))
    assert (status, response["error"]) == (409, "exec_stale_state")
    assert state(w, key) == left
    assert owner(w, key) == (None, None, None)     # checked before any ownership
    assert dict(w.store._item(key)) == before


@pytest.mark.parametrize("verb,body", [("park", park), ("complete", complete)])
@pytest.mark.parametrize("who", ["workerA", "workerB"])
def test_claimed_live_set_accepts_only_the_holder(w, doors, verb, body, who):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    before = dict(w.store._item(key))
    status, response = doors.http(who, "POST", f"/v1/{verb}",
                                  body(key, expected_state=LIVE_SET, **RUN1))
    if who == "workerA":
        assert (status, response) == (204, "")
        assert owner(w, key) == ("wf-1", "run-1", 1)
        assert state(w, key) == ("parked" if verb == "park" else "claimed")
        return
    assert (status, response["error"]) == (409, "exec_stale_state")
    assert state(w, key) == "claimed"
    assert owner(w, key) == (None, None, None)     # checked before any ownership
    assert dict(w.store._item(key)) == before
