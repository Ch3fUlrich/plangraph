"""Ask CAS checks the observed state before recording any exec ownership."""
import pytest

from door_util import Doors
from keeper_util import World, seed
from plangraph.keeper.errors import Refused


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
@pytest.mark.parametrize("expected, accepted", [("running", False),
                                                ("waiting", True), (None, True)])
def test_delayed_ask_compares_projected_state(w, tmp_path, http, expected, accepted):
    key = waiting(w)
    before = snapshot(w, key)
    extra = {} if expected is None else {"expected_state": expected}
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
