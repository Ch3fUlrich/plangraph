"""Ask CAS checks the observed state before recording any exec ownership."""
import pytest

from door_util import Doors
from keeper_util import World, seed, to_claimed
from plangraph.keeper.errors import Refused

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


def snapshot(w, key):
    return tuple(w.conn.execute(
        "SELECT state, exec_workflow_id, exec_run_id, exec_attempt FROM items"
        " WHERE key=?", (key,)).fetchone())


def waiting(w):
    key, = seed(w, [("A", [])])
    for state in ("running", "waiting"):
        w.store.set_state(w.p("system"), key, state,
                          run=("wf-1", "run-1", 1))
    assert snapshot(w, key) == ("waiting", "wf-1", "run-1", 1)
    return key


@pytest.mark.parametrize("http", [False, True])
# The live set carries waiting now, so it matches the projected waiting state;
# a set that still excludes waiting (the pre-waiting pin) does not.
@pytest.mark.parametrize("expected, accepted", [("running", False),
                                                ("waiting", True), (None, True),
                                                (LIVE_SET, True),
                                                (["open", "ready", "running", "needs_input"], False),
                                                (["running", "waiting"], True)])
def test_delayed_ask_compares_projected_state(w, tmp_path, http, expected, accepted):
    key = waiting(w)
    before = snapshot(w, key)
    extra = {"expected_state": expected}
    if http:
        live = Doors(w, tmp_path)
        try:
            status, body = live.http("workerA", "POST", "/v1/ask_question", {
                "key": key, "text": "continue?", "workflow_id": "wf-1",
                "run_id": "run-1", "attempt": 1, **extra})
        finally:
            live.close()
        if accepted:
            assert (status, body["state"]) == (200, "open")
        else:
            assert status >= 400
            assert body["error"] == "exec_stale_state"
    else:
        def ask():
            return w.store.ask_question(
                w.p("workerA"), key, "continue?", exec_workflow_id="wf-1",
                exec_run_id="run-1", exec_attempt=1, **extra)
        if accepted:
            assert ask()["state"] == "open"
        else:
            with pytest.raises(Refused) as err:
                ask()
            assert err.value.code == "exec_stale_state"
    assert snapshot(w, key) == before
    assert w.conn.execute("SELECT COUNT(*) FROM questions WHERE key=?",
                          (key,)).fetchone()[0] == int(accepted)


def test_stale_state_is_checked_before_first_owner_record(w, monkeypatch):
    key, = seed(w, [("A", [])])
    before = snapshot(w, key)

    def no_owner_check(*args, **kwargs):
        pytest.fail("exec ownership checked before stale observed state")

    monkeypatch.setattr(type(w.store), "_exec_check", no_owner_check)
    with pytest.raises(Refused) as err:
        w.store.ask_question(w.p("workerA"), key, "late?",
                             exec_workflow_id="wf-1", exec_run_id="run-1",
                             exec_attempt=1, expected_state="running")
    assert err.value.code == "exec_stale_state"
    assert snapshot(w, key) == before
    assert w.conn.execute("SELECT COUNT(*) FROM questions WHERE key=?",
                          (key,)).fetchone()[0] == 0


@pytest.mark.parametrize("expected", [True, [], ["running", False],
                                      ["running", "bogus"], [1], {}])
def test_ask_rejects_invalid_any_of_before_writes(w, expected):
    key = waiting(w)
    before = snapshot(w, key)
    with pytest.raises(Refused) as err:
        w.store.ask_question(w.p("workerA"), key, "continue?",
                             exec_workflow_id="wf-1", exec_run_id="run-1",
                             exec_attempt=1, expected_state=expected)
    assert err.value.code == "validation"
    assert snapshot(w, key) == before
    assert w.conn.execute("SELECT COUNT(*) FROM questions WHERE key=?",
                          (key,)).fetchone()[0] == 0


@pytest.mark.parametrize("outside", ["claimed", "parked", "failed", "done"])
def test_live_set_ask_refuses_a_card_outside_the_live_states(w, outside):
    key, = seed(w, [("A", [])])
    if outside == "claimed":
        to_claimed(w, key, "workerA")
    elif outside == "done":                  # done has its own verb, not set_state
        w.conn.execute("UPDATE items SET state=? WHERE key=?", (outside, key))
        w.conn.commit()
    else:
        w.store.set_state(w.p("system"), key, outside)
    assert snapshot(w, key)[0] == outside
    before = snapshot(w, key)
    with pytest.raises(Refused) as err:
        w.store.ask_question(w.p("workerA"), key, "continue?",
                             exec_workflow_id="wf-1", exec_run_id="run-1",
                             exec_attempt=1, expected_state=LIVE_SET)
    assert err.value.code == "exec_stale_state"
    assert snapshot(w, key) == before
    assert w.conn.execute("SELECT COUNT(*) FROM questions WHERE key=?",
                          (key,)).fetchone()[0] == 0
