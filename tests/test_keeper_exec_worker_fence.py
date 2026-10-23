"""Worker audit/no-op calls must check ownership without acquiring it."""
import pytest

from keeper_util import World, seed, seed_exec_owner
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


RUN = {"workflow_id": "wf", "run_id": "run", "attempt": 2}


def owner(w, key):
    row = w.conn.execute(
        "SELECT exec_workflow_id, exec_run_id, exec_attempt FROM items WHERE key=?",
        (key,)).fetchone()
    return tuple(row)


def open_alarms(w, key):
    """How many open ``worker_alarm`` rows *key* carries (a duplicate fact)."""
    return w.conn.execute(
        "SELECT COUNT(*) FROM projector_alarms WHERE kind='worker_alarm'"
        " AND key=? AND cleared_at IS NULL", (key,)).fetchone()[0]


@pytest.mark.parametrize("extra", [RUN, {}, {**RUN, "run_id": "stale"},
                                   {**RUN, "attempt": 1}])
def test_attempt_checks_existing_owner(w, extra):
    key, = seed(w, [("A", [])])
    # Only a worker may RECORD an owner (V2-1); the router move below then
    # only checks the triple it no longer records.
    seed_exec_owner(w, key, ("wf", "run", 2))
    w.store.set_state(w.p("router"), key, "running", run=("wf", "run", 2))
    # Rust worker report now carries ExecId on worker_attempt, not just lifecycle verbs.
    if extra == RUN:
        result = w.store.worker_attempt(w.p("workerA"),
                                       {"card": key, "n": 1, "exit_class": "check_fail", **extra})
        assert result["n"] == 1
    else:
        with pytest.raises(Refused) as exc:
            w.store.worker_attempt(w.p("workerA"),
                                   {"card": key, "n": 1, "exit_class": "check_fail", **extra})
        assert exc.value.code == "exec_stale"
    assert owner(w, key) == ("wf", "run", 2)


def test_audit_attempt_checks_but_does_not_advance_owner(w):
    key, = seed(w, [("A", [])])
    # Only a worker may RECORD an owner (V2-1); the router move below then
    # only checks the triple it no longer records.
    seed_exec_owner(w, key, ("wf", "run", 2))
    w.store.set_state(w.p("router"), key, "running", run=("wf", "run", 2))
    w.store.worker_attempt(w.p("workerA"),
                           {"card": key, "n": 1, "exit_class": "check_fail",
                            **RUN, "attempt": 3})
    assert owner(w, key) == ("wf", "run", 2)
    with pytest.raises(Refused) as exc:
        w.store.worker_attempt(w.p("workerA"),
                               {"card": key, "n": 1, "exit_class": "check_fail",
                                **RUN, "attempt": 1})
    assert exc.value.code == "exec_stale"
    assert owner(w, key) == ("wf", "run", 2)


@pytest.mark.parametrize("extra", [{}, RUN])
def test_unowned_attempt_never_records_owner(w, extra):
    key, = seed(w, [("A", [])])
    result = w.store.worker_attempt(w.p("workerA"),
                                    {"card": key, "n": 1, "exit_class": "check_fail", **extra})
    assert result["n"] == 1
    assert owner(w, key) == (None, None, None)


def test_noop_complete_does_not_acquire_owner(w):
    key, = seed(w, [("A", [])])
    with w.store._write():
        w.conn.execute("UPDATE items SET state='done' WHERE key=?", (key,))
    w.store.worker_complete(w.p("workerA"),
                            {"card": key, "merged_sha": "deadbeef", **RUN})
    assert owner(w, key) == (None, None, None)


@pytest.mark.parametrize("verb,body", [
    ("worker_park", {"tier": "free", "exit_class": "check_fail"}),
    ("worker_alarm", {"reason": "stuck"}),
])
def test_existing_unowned_fact_does_not_acquire_owner(w, verb, body):
    key, = seed(w, [("A", [])])
    call = getattr(w.store, verb)
    call(w.p("workerA"), {"card": key, **body})
    call(w.p("workerA"), {"card": key, **body, **RUN})
    assert owner(w, key) == (None, None, None)


def test_open_alarm_on_an_owned_card_still_advances_the_attempt(w):
    """An alarm records on an OWNED card even when an open alarm already exists.

    ``_worker_alarm`` used to gate on ``existing is None``, so a run-carrying
    alarm whose (duplicate) row already existed only VALIDATED: the recorded
    attempt never advanced, and the older attempt of the same run then passed
    the fence.  The alarm fact is still de-duplicated (one open row); only the
    fence moves.  On an UNOWNED card an open alarm stays a fact that acquires no
    owner -- ``test_existing_unowned_fact_does_not_acquire_owner`` pins that
    side and is unchanged.
    """
    key, = seed(w, [("A", [])])
    alarm = {"card": key, "reason": "stuck"}
    # 1. A first alarm of run R at attempt 1 opens the fact and records R.
    w.store.worker_alarm(w.p("workerA"),
                         {**alarm, "workflow_id": "wf", "run_id": "run",
                          "attempt": 1})
    assert owner(w, key) == ("wf", "run", 1)
    assert open_alarms(w, key) == 1
    # 2. The same run at attempt 2 while that alarm is still open: the row is a
    #    duplicate fact, the recorded attempt is not -- it advances to 2.
    w.store.worker_alarm(w.p("workerA"),
                         {**alarm, "workflow_id": "wf", "run_id": "run",
                          "attempt": 2})
    assert owner(w, key) == ("wf", "run", 2)
    assert open_alarms(w, key) == 1
    # 3. ... so the older attempt of the same run can no longer touch the card.
    with pytest.raises(Refused) as exc:
        w.store.worker_park(w.p("workerA"),
                            {"card": key, "tier": "free",
                             "exit_class": "check_fail",
                             "workflow_id": "wf", "run_id": "run", "attempt": 1})
    assert exc.value.code == "exec_stale"
    assert owner(w, key) == ("wf", "run", 2)


def test_refused_park_does_not_acquire_owner(w):
    key, = seed(w, [("A", [])])
    with w.store._write():
        w.conn.execute("UPDATE items SET state='done' WHERE key=?", (key,))
    with pytest.raises(Refused):
        w.store.worker_park(w.p("workerA"),
                            {"card": key, "exit_class": "check_fail", **RUN})
    assert owner(w, key) == (None, None, None)


def test_refused_complete_does_not_acquire_owner(w, monkeypatch):
    """V1-A, the complete mirror of the park test above: gate + record and the
    completion write share ONE transaction, so a completion whose guarded write
    is refused (race guard) rolls the owner/attempt record back with it -- a
    failed completion never leaves a recorded owner.
    """
    key, = seed(w, [("A", [])])
    before = dict(w.store._item(key))

    def lost_race(self, cursor, item_key, expected_state, expected_epoch):
        raise Refused("epoch_stale", f"{item_key}: completion write lost the race")

    monkeypatch.setattr(type(w.store), "_race_guard", lost_race)
    with pytest.raises(Refused) as exc:
        w.store.worker_complete(w.p("workerA"),
                                {"card": key, "merged_sha": "deadbeef", **RUN})
    assert exc.value.code == "epoch_stale"
    assert dict(w.store._item(key)) == before
    assert owner(w, key) == (None, None, None)


@pytest.mark.parametrize("extra", [{}, {**RUN, "run_id": "stale"},
                                   {**RUN, "attempt": 1}])
def test_noop_complete_still_checks_recorded_owner(w, extra):
    key, = seed(w, [("A", [])])
    # Only a worker may RECORD an owner (V2-1); the router move below then
    # only checks the triple it no longer records.
    seed_exec_owner(w, key, ("wf", "run", 2))
    w.store.set_state(w.p("router"), key, "running", run=("wf", "run", 2))
    with w.store._write():
        w.conn.execute("UPDATE items SET state='done' WHERE key=?", (key,))
    with pytest.raises(Refused) as exc:
        w.store.worker_complete(w.p("workerA"),
                                {"card": key, "merged_sha": "deadbeef", **extra})
    assert exc.value.code == "exec_stale"
    assert owner(w, key) == ("wf", "run", 2)


# ---------------------------------------- a lost response retries as a no-op
def park_body(key, **extra):
    body = {"card": key, "tier": "free", "exit_class": "check_fail",
            "check_tail": "boom"}
    body.update(extra)
    return body


def test_a_duplicate_park_with_a_stale_live_set_is_a_success_noop(w):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "running")
    # The live-set the worker observed before its first park succeeded.
    body = park_body(key, expected_state="running", **RUN)
    assert w.store.worker_park(w.p("workerA"), body) == {
        "card": key, "state": "parked", "changed": True}
    before = dict(w.store._item(key))
    # The retry names the recorded owner: a duplicate is a success no-op, decided
    # before expected_state -- not exec_stale_state on all five retries.
    assert w.store.worker_park(w.p("workerA"), body) == {
        "card": key, "state": "parked", "changed": False}
    assert dict(w.store._item(key)) == before
    assert owner(w, key) == ("wf", "run", 2)


def test_a_parked_card_retried_by_another_run_is_exec_stale(w):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "running")
    body = park_body(key, expected_state="running", **RUN)
    w.store.worker_park(w.p("workerA"), body)
    before = dict(w.store._item(key))
    # The stale-run check decides first: a different run is exec_stale even
    # though the card is parked and the live set is stale too.
    with pytest.raises(Refused) as exc:
        w.store.worker_park(w.p("workerA"), dict(body, run_id="another-run"))
    assert exc.value.code == "exec_stale"
    assert dict(w.store._item(key)) == before
    assert owner(w, key) == ("wf", "run", 2)


def test_a_first_park_with_a_stale_live_set_is_exec_stale_state(w):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "running")
    before = dict(w.store._item(key))
    # Not a duplicate: the card is not parked yet, so expected_state still fences.
    with pytest.raises(Refused) as exc:
        w.store.worker_park(w.p("workerA"),
                            park_body(key, expected_state="open", **RUN))
    assert exc.value.code == "exec_stale_state"
    assert dict(w.store._item(key)) == before
    assert owner(w, key) == (None, None, None)


def test_a_duplicate_complete_with_a_stale_live_set_is_a_success_noop(w):
    key, = seed(w, [("A", [])])
    seed_exec_owner(w, key, ("wf", "run", 2))
    w.store.set_state(w.p("router"), key, "waiting", run=("wf", "run", 2))
    body = {"card": key, "merged_sha": "deadbeef",
            "expected_state": "waiting", **RUN}
    w.store.worker_complete(w.p("workerA"), body)
    with w.store._write():
        w.conn.execute("UPDATE items SET state='done' WHERE key=?", (key,))
    before = dict(w.store._item(key))
    # The same order for complete: a duplicate of the recorded owner is a
    # success no-op before expected_state, everything else is checked as before.
    w.store.worker_complete(w.p("workerA"), body)
    assert dict(w.store._item(key)) == before
    assert owner(w, key) == ("wf", "run", 2)
