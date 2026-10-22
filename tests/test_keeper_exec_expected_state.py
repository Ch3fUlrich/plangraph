"""Observed-state CAS fences delayed duplicates within the same exec attempt."""
import pytest

from door_util import Doors
from keeper_util import World, seed
from test_keeper_exec_verbs import RUN1, complete, owner, park, state


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
    w.store.set_state(w.p("router"), key, "running", run=run)
    w.clock.advance(10)
    w.store.set_state(w.p("router"), key, "waiting", run=run)
    return key


@pytest.mark.parametrize("verb,body", [("park", park), ("complete", complete)])
@pytest.mark.parametrize("attempt", [1, 2])
def test_delayed_verb_preserves_projected_state_owner_and_timestamp(w, doors, verb, body, attempt):
    key = waiting(w)
    before = dict(w.store._item(key))
    w.clock.advance(10)
    ids = dict(RUN1, attempt=attempt)
    status, response = doors.http("workerA", "POST", f"/v1/{verb}",
                                  body(key, expected_state="running", **ids))
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
@pytest.mark.parametrize("expected", [{}, {"expected_state": "waiting"}])
def test_matching_or_absent_expected_state_preserves_existing_behavior(w, doors, verb, body, expected):
    key = waiting(w)
    assert doors.http("workerA", "POST", f"/v1/{verb}",
                      body(key, **expected, **RUN1)) == (204, "")
    assert state(w, key) == ("parked" if verb == "park" else "waiting")
    assert owner(w, key) == ("wf-1", "run-1", 1)


@pytest.mark.parametrize("verb,body", [("park", park), ("complete", complete)])
@pytest.mark.parametrize("expected", ["bogus", "claimed", 1, True, [], {}, None])
def test_invalid_expected_state_is_validation(w, doors, verb, body, expected):
    key = waiting(w)
    before = dict(w.store._item(key))
    status, response = doors.http("workerA", "POST", f"/v1/{verb}",
                                  body(key, expected_state=expected, **RUN1))
    assert (status, response["error"]) == (400, "validation")
    assert dict(w.store._item(key)) == before


@pytest.mark.parametrize("verb,body", [("park", park), ("complete", complete)])
def test_matching_state_does_not_bypass_exec_identity(w, doors, verb, body):
    key = waiting(w)
    before = dict(w.store._item(key))
    status, response = doors.http("workerA", "POST", f"/v1/{verb}",
                                  body(key, expected_state="waiting",
                                       workflow_id="wf-2", run_id="run-2", attempt=1))
    assert (status, response["error"]) == (409, "exec_stale")
    assert dict(w.store._item(key)) == before
