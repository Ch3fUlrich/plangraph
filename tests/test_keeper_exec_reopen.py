"""Keeper reopen frees ownership; the first fact-writing run can acquire it."""
import pytest

from door_util import Doors
from keeper_util import World, seed, to_claimed
from test_keeper_exec_ownership import owner, refused, seed_owner
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
    # Only a worker principal may record an owner now (RT-P1 V2-1), so the
    # record goes through the worker principal; the router's run-carrying
    # admin moves and the explicit hand-back keep their exact event history.
    w.store.set_state(w.p("router"), key, "running")
    with w.store._write():
        w.store._exec_record(w.conn, key, *RUN1, w.p("workerA"))
    w.store.set_state(w.p("router"), key, "open", hand_back=True)
    # The hand-back above is a revival: it frees -- and RETIRES -- the owner
    # (RT-P1 V1-1), so re-record it past the fence -- this helper must hand
    # back a CLAIMED card RUN1 still owns.
    with w.store._write():
        w.store._exec_record(w.conn, key, *RUN1, w.p("workerA"))
    epoch = to_claimed(w, key, "workerA")
    assert owner(w, key) == RUN1
    return epoch


@pytest.mark.parametrize("late_run_first", [False, True])
def test_lapsed_lease_late_retired_run_is_refused(w, doors, late_run_first):
    """RT-P1 sq5 fix 2 (V1-1/V5-1): a clear keeps a retired-run fence.

    Reap clears the owner; the retired ``(run_id, max attempt)`` fence stays,
    so a late call from the old run is refused ``exec_stale`` and cannot
    reacquire -- even when it is the first call at the cleared card.  A NEW
    run still gets the card.  (Renamed from
    ``test_lapsed_lease_first_writer_wins_even_for_late_run``, which pinned
    the retired run's 204 acceptance as correct.)
    """
    key, = seed(w, [("A", [])])
    claimed_owner(w, key)
    body = park(key, **RUN2)
    status, response = doors.http("workerA", "POST", "/v1/park", body)
    assert (status, response["error"]) == (409, "exec_stale")
    assert owner(w, key) == RUN1

    assert w.store.reap(w.p("system"), w.clock.advance(7201))["expired"] == [key]
    assert owner(w, key) == (None, None, None)
    refused("forbidden", w.store.worker_park, w.p("workerA"), body)
    late = {"workflow_id": RUN1[0], "run_id": RUN1[1], "attempt": RUN1[2]}
    if late_run_first:
        # FIRST at the cleared card, and still refused: the retired fence
        # keeps the old run out; it cannot reacquire the owner.
        status, response = doors.http("workerA", "POST", "/v1/park",
                                      park(key, **late))
        assert (status, response["error"]) == (409, "exec_stale")
        assert owner(w, key) == (None, None, None)
        assert w.store._item(key)["state"] == "open"
        # ...and a NEW run still gets the card.
        to_claimed(w, key, "workerA")
        assert doors.http("workerA", "POST", "/v1/park", body) == (204, "")
        assert owner(w, key) == ("wf-2", "run-2", 1)
        return
    to_claimed(w, key, "workerA")
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


def test_second_review_change_reopens_without_owner_and_new_run_can_park(w):
    key, = seed(w, [("A", [])])
    epoch = claimed_owner(w, key)
    worker = w.p("workerA")
    w.store.add_evidence(worker, key, epoch, "decision", {"id": "D-1"})
    for expected in ("claimed", "open"):
        w.store.submit(worker, key, epoch)
        assert w.store._item(key)["state"] == "in_review"
        assert owner(w, key) == (RUN1 if expected == "claimed" else (None, None, None))
        result = w.store.verdict(w.p("reviewer"), key, False, "needs changes")
        assert result["state"] == expected
        assert owner(w, key) == (None, None, None)
    assert owner(w, key) == (None, None, None)
    to_claimed(w, key, "workerA")
    assert w.store.worker_park(worker, park(key, **RUN2))["state"] == "parked"
    assert owner(w, key) == ("wf-2", "run-2", 1)


def test_null_exec_keys_behave_as_omitted(w):
    key, = seed(w, [("A", [])])
    assert w.store.worker_park(w.p("workerA"), park(
        key, workflow_id=None, run_id=None, attempt=None))["state"] == "parked"
    assert owner(w, key) == (None, None, None)
    # A new run must take a real claim before recording.
    w.store.requeue(w.p("router"), key)
    to_claimed(w, key, "workerA")
    w.store.worker_complete(w.p("workerA"), complete(
        key, workflow_id=RUN1[0], run_id=RUN1[1], attempt=RUN1[2]))
    assert owner(w, key) == RUN1
    before = dict(w.store._item(key))
    for fields in ({}, {"attempt": None},
                   {"workflow_id": None, "run_id": None, "attempt": None}):
        refused("exec_stale", w.store.worker_park, w.p("workerA"),
                park(key, **fields))
        assert dict(w.store._item(key)) == before
    assert owner(w, key) == RUN1


@pytest.mark.parametrize("verb", ["park", "complete", "ask"])
def test_null_expected_state_behaves_as_omitted(w, verb):
    key, = seed(w, [("A", [])])
    from keeper_util import seed_exec_owner
    seed_exec_owner(w, key, ("wf-2", "run-2", 1))
    worker = w.p("workerA")
    if verb == "park":
        assert w.store.worker_park(worker, park(
            key, expected_state=None, **RUN2))["state"] == "parked"
    elif verb == "complete":
        w.store.worker_complete(worker, complete(key, expected_state=None, **RUN2))
        assert w.store._item(key)["state"] == "open"
    else:
        w.store.ask_question(worker, key, "Which path?", expected_state=None,
                             exec_workflow_id=RUN2["workflow_id"],
                             exec_run_id=RUN2["run_id"], exec_attempt=RUN2["attempt"])
        assert w.store._item(key)["state"] == "open"
    assert owner(w, key) == ("wf-2", "run-2", 1)


def test_workflow_mismatch_is_stale_even_with_same_run(w):
    key, = seed(w, [("A", [])])
    from keeper_util import seed_exec_owner
    seed_exec_owner(w, key, RUN1)
    seed_owner(w, key, RUN1)
    refused("exec_stale", w.store.set_state, w.p("router"), key, "waiting",
            run=("other-wf", RUN1[1], 3))
    assert owner(w, key) == RUN1


@pytest.mark.parametrize("attempt", [1, 2])
def test_new_run_requires_owner_clear_before_acquiring(w, attempt):
    key, = seed(w, [("A", [])])
    from keeper_util import seed_exec_owner
    seed_exec_owner(w, key, RUN1)
    w.store.worker_park(w.p("workerA"), park(key, workflow_id=RUN1[0],
                                              run_id=RUN1[1],
                                              attempt=RUN1[2]))
    assert owner(w, key) == RUN1
    before = dict(w.store._item(key))
    refused("validation", w.store.set_state, w.p("router"), key, "open",
            run=("wf-2", "run-2", attempt), new_run=True)
    assert dict(w.store._item(key)) == before
    refused("exec_stale", w.store.worker_park, w.p("workerA"),
            park(key, **dict(RUN2, attempt=attempt)))
    # Requeue retires the old run; the new run must claim before recording.
    w.store.requeue(w.p("router"), key)
    assert owner(w, key) == (None, None, None)
    to_claimed(w, key, "workerA")
    assert owner(w, key) == (None, None, None)
    w.store.worker_park(w.p("workerA"), park(key, **dict(RUN2, attempt=attempt)))
    assert w.store._item(key)["state"] == "parked"
    assert owner(w, key) == ("wf-2", "run-2", attempt)


def test_noop_records_no_owner_and_checks_existing_owner(w):
    key, = seed(w, [("A", [])])
    before = dict(w.store._item(key))
    result = w.store.set_state(w.p("router"), key, "open", run=RUN1)
    assert result["changed"] is False
    assert dict(w.store._item(key)) == before
    seed_owner(w, key, RUN1)              # only a worker records an owner (V2-1)
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
    from keeper_util import to_claimed
    to_claimed(w, key, "workerA")
    for attempt in (1, 2):
        before = w.store._item(key)["updated_at"]
        w.clock.advance(10)
        w.store.ask_question(w.p("workerA"), key, "Which path?",
                             exec_workflow_id="wf-1", exec_run_id="run-1",
                             exec_attempt=attempt)
        assert owner(w, key) == ("wf-1", "run-1", attempt)
        assert w.store._item(key)["updated_at"] == w.store._iso(w.clock())
        assert w.store._item(key)["updated_at"] != before


@pytest.mark.parametrize("new_run", [False, True])
def test_unowned_back_move_with_run_is_refused(w, new_run):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "parked")
    before = dict(w.store._item(key))
    refused("validation" if new_run else "bad_transition", w.store.set_state, w.p("router"), key, "open",
            run=RUN1, new_run=new_run)
    assert dict(w.store._item(key)) == before
    assert owner(w, key) == (None, None, None)


def test_exec_helpers_without_now_preserve_timestamp_and_clear_owner(w):
    key, = seed(w, [("A", [])])
    before = w.store._item(key)["updated_at"]
    w.clock.advance(10)
    with w.store._write():
        w.store._exec_record(w.conn, key, *RUN1, w.p("workerA"))
        assert owner(w, key) == RUN1
        assert w.store._item(key)["updated_at"] == before
        w.store._exec_check(w.conn, key, "wf-1", "run-1", 3, w.p("workerA"))
        assert owner(w, key) == ("wf-1", "run-1", 3)
        assert w.store._item(key)["updated_at"] == before
        w.store._exec_clear(w.conn, key, w.p("router"), w.clock())
        assert owner(w, key) == (None, None, None)
