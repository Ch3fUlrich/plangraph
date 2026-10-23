"""RT-P1-EXEC-OWNERSHIP: the worker verbs are checked against the exec owner.

``worker_park``, ``worker_complete`` and ``worker_alarm`` carry
``(workflow_id, run_id, attempt)`` like a ``set_state`` move with ``run``: the
first call on an unowned card records the owner, a different run or a lower
attempt is refused as ``exec_stale``, and a card that already has an owner
refuses bodies that omit all three fields — so one run cannot act on another
run's card.  Bodies without the fields keep the historical behaviour on
unowned cards.  Every rule gets a test that fails if the rule is removed, per
verb.  Driven through the HTTP door like the Rust worker
(rust/worker/src/keeper.rs); portable: runs on the default backend (``World``
follows ``PG_KEEPER_BACKEND``).
"""
import pytest

from door_util import Doors
from keeper_util import World, seed, to_claimed


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


def park(card, **extra):
    body = {"card": card, "tier": "free", "exit_class": "check_fail",
            "check_tail": "boom"}
    body.update(extra)
    return body


def complete(card, **extra):
    body = {"card": card, "merged_sha": "deadbeef"}
    body.update(extra)
    return body


def alarm(card, **extra):
    body = {"card": card, "reason": "stuck on check"}
    body.update(extra)
    return body


def state(w, key):
    return w.conn.execute("SELECT state FROM items WHERE key=?", (key,)).fetchone()[0]


def owner(w, key):
    row = w.conn.execute("SELECT exec_workflow_id, exec_run_id, exec_attempt"
                         " FROM items WHERE key=?", (key,)).fetchone()
    return (row[0], row[1], row[2])


def alarms(w, key):
    return w.conn.execute(
        "SELECT detail FROM projector_alarms WHERE key=?", (key,)).fetchall()


RUN1 = {"workflow_id": "wf-1", "run_id": "run-1", "attempt": 1}
RUN1_A2 = {"workflow_id": "wf-1", "run_id": "run-1", "attempt": 2}
RUN2 = {"workflow_id": "wf-2", "run_id": "run-2", "attempt": 1}


# ---------------------------------------------------------------------- park
def test_park_with_fields_records_the_exec_owner(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/park", park(key, **RUN1)) == (204, "")
    assert owner(w, key) == ("wf-1", "run-1", 1)
    assert state(w, key) == "parked"


def test_a_claimed_card_parked_by_its_holder_records_the_owner(doors, w):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    assert doors.http("workerA", "POST", "/v1/park", park(key, **RUN1)) == (204, "")
    assert state(w, key) == "parked"
    assert owner(w, key) == ("wf-1", "run-1", 1)
    row = w.conn.execute(
        "SELECT claimed_by, delegate, lease_until FROM items WHERE key=?",
        (key,)).fetchone()
    assert tuple(row) == (None, None, None)


def test_a_claimed_owned_card_refuses_park_by_a_stale_run(doors, w):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    assert doors.http("workerA", "POST", "/v1/complete",
                      complete(key, **RUN1)) == (204, "")
    status, body = doors.http("workerA", "POST", "/v1/park", park(key, **RUN2))
    assert (status, body["error"]) == (409, "exec_stale")
    assert state(w, key) == "claimed"
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_park_with_a_different_run_is_refused_and_changes_nothing(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/park", park(key, **RUN1)) == (204, "")
    was = state(w, key)
    status, body = doors.http("workerA", "POST", "/v1/park", park(key, **RUN2))
    assert (status, body["error"]) == (409, "exec_stale")
    assert state(w, key) == was
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_park_with_a_lower_attempt_is_refused(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/park",
                      park(key, **RUN1_A2)) == (204, "")
    status, body = doors.http("workerA", "POST", "/v1/park", park(key, **RUN1))
    assert (status, body["error"]) == (409, "exec_stale")
    assert owner(w, key) == ("wf-1", "run-1", 2)


def test_park_without_fields_on_an_owned_card_is_refused(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/park", park(key, **RUN1)) == (204, "")
    status, body = doors.http("workerA", "POST", "/v1/park", park(key))
    assert (status, body["error"]) == (409, "exec_stale")
    assert "has an exec owner" in body["msg"]
    assert state(w, key) == "parked"
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_park_without_fields_on_an_unowned_card_works_as_before(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/park", park(key)) == (204, "")
    assert state(w, key) == "parked"
    assert owner(w, key) == (None, None, None)


# ------------------------------------------------------------------ complete
def test_complete_with_fields_records_the_exec_owner(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/complete",
                      complete(key, **RUN1)) == (204, "")
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_complete_with_a_different_run_is_refused_and_changes_nothing(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/complete",
                      complete(key, **RUN1)) == (204, "")
    was = state(w, key)
    status, body = doors.http("workerA", "POST", "/v1/complete",
                              complete(key, **RUN2))
    assert (status, body["error"]) == (409, "exec_stale")
    assert state(w, key) == was
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_complete_with_a_lower_attempt_is_refused(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/complete",
                      complete(key, **RUN1_A2)) == (204, "")
    status, body = doors.http("workerA", "POST", "/v1/complete",
                              complete(key, **RUN1))
    assert (status, body["error"]) == (409, "exec_stale")
    assert owner(w, key) == ("wf-1", "run-1", 2)


def test_complete_without_fields_on_an_owned_card_is_refused(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/complete",
                      complete(key, **RUN1)) == (204, "")
    status, body = doors.http("workerA", "POST", "/v1/complete", complete(key))
    assert (status, body["error"]) == (409, "exec_stale")
    assert "has an exec owner" in body["msg"]
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_complete_without_fields_on_an_unowned_card_works_as_before(doors, w):
    key, = seed(w, [("A", [])])
    was = state(w, key)
    assert doors.http("workerA", "POST", "/v1/complete",
                      complete(key)) == (204, "")
    assert state(w, key) == was
    assert owner(w, key) == (None, None, None)


# --------------------------------------------------------------------- alarm
def test_alarm_with_fields_records_the_exec_owner(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/alarms",
                      alarm(key, **RUN1)) == (204, "")
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_alarm_with_a_different_run_is_refused_and_raises_nothing(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/alarms",
                      alarm(key, **RUN1)) == (204, "")
    status, body = doors.http("workerA", "POST", "/v1/alarms",
                              alarm(key, **RUN2))
    assert (status, body["error"]) == (409, "exec_stale")
    assert [r[0] for r in alarms(w, key)] == ["stuck on check"]   # no new row
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_alarm_with_a_lower_attempt_is_refused(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/alarms",
                      alarm(key, **RUN1_A2)) == (204, "")
    status, body = doors.http("workerA", "POST", "/v1/alarms",
                              alarm(key, **RUN1))
    assert (status, body["error"]) == (409, "exec_stale")
    assert owner(w, key) == ("wf-1", "run-1", 2)


def test_alarm_without_fields_on_an_owned_card_is_refused(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/alarms",
                      alarm(key, **RUN1)) == (204, "")
    status, body = doors.http("workerA", "POST", "/v1/alarms", alarm(key))
    assert (status, body["error"]) == (409, "exec_stale")
    assert "has an exec owner" in body["msg"]
    assert [r[0] for r in alarms(w, key)] == ["stuck on check"]   # no new row
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_alarm_without_fields_on_an_unowned_card_works_as_before(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/alarms", alarm(key)) == (204, "")
    assert [r[0] for r in alarms(w, key)] == ["stuck on check"]
    assert owner(w, key) == (None, None, None)
