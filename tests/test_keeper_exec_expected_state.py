"""Observed-state CAS fences delayed duplicates within the same exec attempt."""
import pytest

from door_util import Doors
from keeper_util import World, seed, seed_exec_owner, to_claimed
from test_keeper_exec_verbs import RUN1, complete, owner, park, state

#: The worker's live set: ``waiting`` is in it because the projector writes
#: waiting after usage_limit/outage and on a timer, and the cap-park must land.
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
                                      {"expected_state": "waiting"},
                                      {"expected_state": LIVE_SET}])
def test_matching_or_absent_expected_state_preserves_existing_behavior(w, doors, verb, body, expected):
    key = waiting(w)
    assert doors.http("workerA", "POST", f"/v1/{verb}",
                      body(key, **expected, **RUN1)) == (204, "")
    assert state(w, key) == ("parked" if verb == "park" else "waiting")
    assert owner(w, key) == ("wf-1", "run-1", 1)


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
def test_live_set_refuses_a_claimed_card(w, doors, verb, body):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    before = dict(w.store._item(key))
    status, response = doors.http("workerA", "POST", f"/v1/{verb}",
                                  body(key, expected_state=LIVE_SET, **RUN1))
    assert (status, response["error"]) == (409, "exec_stale_state")
    assert state(w, key) == "claimed"
    assert owner(w, key) == (None, None, None)     # checked before any ownership
    assert dict(w.store._item(key)) == before
