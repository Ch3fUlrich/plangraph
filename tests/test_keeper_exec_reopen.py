"""Keeper reopen frees exec ownership; worker calls never take it over."""
import pytest

from door_util import Doors
from keeper_util import World, seed, to_claimed
from test_keeper_exec_ownership import owner, refused
from test_keeper_exec_verbs import park, complete


RUN1 = ("wf-1", "run-1", 2)
RUN2 = {"workflow_id": "wf-2", "run_id": "run-2", "attempt": 1}


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


def claimed_owner(w, key):
    # The planner service has no run-carrying projector path in this checkout.
    w.store.set_state(w.p("router"), key, "running", run=RUN1)
    w.store.set_state(w.p("router"), key, "open")
    epoch = to_claimed(w, key, "workerA")
    assert owner(w, key) == RUN1
    return epoch


def test_lapsed_lease_frees_owner_and_late_worker_cannot_take_over(w, doors):
    key, = seed(w, [("A", [])])
    claimed_owner(w, key)
    body = park(key, **RUN2)
    status, response = doors.http("workerA", "POST", "/v1/park", body)
    assert (status, response["error"]) == (409, "exec_stale")
    assert owner(w, key) == RUN1

    assert w.store.reap(w.p("system"), w.clock.advance(7201))["expired"] == [key]
    assert owner(w, key) == (None, None, None)
    assert doors.http("workerA", "POST", "/v1/park", body) == (204, "")
    assert doors.http("workerA", "POST", "/v1/complete",
                      complete(key, **RUN2)) == (204, "")
    status, response = doors.http("workerA", "POST", "/v1/park",
                                  park(key, workflow_id=RUN1[0],
                                       run_id=RUN1[1], attempt=RUN1[2]))
    assert (status, response["error"]) == (409, "exec_stale")
    assert owner(w, key) == ("wf-2", "run-2", 1)


def test_release_clears_owner(w):
    key, = seed(w, [("A", [])])
    epoch = claimed_owner(w, key)
    w.store.release(w.p("workerA"), key, epoch)
    assert owner(w, key) == (None, None, None)


def test_workflow_mismatch_is_stale_even_with_same_run(w):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "running", run=RUN1)
    refused("exec_stale", w.store.set_state, w.p("router"), key, "waiting",
            run=("other-wf", RUN1[1], 3))
    assert owner(w, key) == RUN1


@pytest.mark.parametrize("attempt", [1, 2])
def test_takeover_needs_strictly_higher_attempt(w, attempt):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "parked", run=RUN1)
    refused("exec_stale", w.store.set_state, w.p("router"), key, "open",
            run=("wf-2", "run-2", attempt), new_run=True)
    assert owner(w, key) == RUN1
    assert w.store._item(key)["state"] == "parked"
    w.store.set_state(w.p("router"), key, "open",
                      run=("wf-2", "run-2", 3), new_run=True)
    assert owner(w, key) == ("wf-2", "run-2", 3)


def test_noop_records_no_owner_and_checks_existing_owner(w):
    key, = seed(w, [("A", [])])
    before = dict(w.store._item(key))
    result = w.store.set_state(w.p("router"), key, "open", run=RUN1)
    assert result["changed"] is False
    assert dict(w.store._item(key)) == before
    w.store.set_state(w.p("router"), key, "running", run=RUN1)
    before = dict(w.store._item(key))
    for run in (("wf-2", "run-2", 3), ("wf-1", "run-1", 1)):
        refused("exec_stale", w.store.set_state, w.p("router"), key,
                "running", run=run)
        assert dict(w.store._item(key)) == before
    w.store.set_state(w.p("router"), key, "running", run=("wf-1", "run-1", 3))
    assert dict(w.store._item(key)) == before


def test_first_owner_and_attempt_bump_touch_updated_at(w):
    key, = seed(w, [("A", [])])
    for attempt in (1, 2):
        before = w.store._item(key)["updated_at"]
        w.clock.advance(10)
        w.store.ask_question(w.p("router"), key, "Which path?",
                             exec_workflow_id="wf-1", exec_run_id="run-1",
                             exec_attempt=attempt)
        assert owner(w, key) == ("wf-1", "run-1", attempt)
        assert w.store._item(key)["updated_at"] == w.store._iso(w.clock())
        assert w.store._item(key)["updated_at"] != before


def test_unowned_back_move_can_record_a_first_owner(w):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "parked")
    w.store.set_state(w.p("router"), key, "open", run=RUN1, new_run=True)
    assert owner(w, key) == RUN1


def test_exec_helpers_without_now_preserve_timestamp_and_clear_owner(w):
    key, = seed(w, [("A", [])])
    before = w.store._item(key)["updated_at"]
    w.clock.advance(10)
    with w.store._write():
        w.store._exec_record(w.conn, key, *RUN1)
        assert owner(w, key) == RUN1
        assert w.store._item(key)["updated_at"] == before
        w.store._exec_check(w.conn, key, "wf-1", "run-1", 3)
        assert owner(w, key) == ("wf-1", "run-1", 3)
        assert w.store._item(key)["updated_at"] == before
        w.store._exec_clear(w.conn, key)
        assert owner(w, key) == (None, None, None)
