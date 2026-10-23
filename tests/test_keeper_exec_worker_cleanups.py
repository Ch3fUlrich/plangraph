"""Worker null-state symmetry and read-only attempt ownership checks."""
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


@pytest.mark.parametrize("verb,body", [
    ("worker_park", {"exit_class": "check_fail"}),
    ("worker_complete", {"merged_sha": "deadbeef"}),
])
@pytest.mark.parametrize("owned", [False, True])
def test_null_expected_state_is_omitted(w, verb, body, owned):
    key, = seed(w, [("A", [])])
    from keeper_util import seed_exec_owner
    seed_exec_owner(w, key, ("wf", "run", 1))
    if owned:
        # Only a worker may RECORD an owner now (V2-1): seed it, the router
        # run-carrying move below only checks the triple it no longer plants.
        seed_exec_owner(w, key, ("wf", "run", 2))
        w.store.set_state(w.p("router"), key, "running", run=("wf", "run", 2))
    result = getattr(w.store, verb)(
        w.p("workerA"), {"card": key, **body, **RUN, "expected_state": None})
    assert result["card"] == key


@pytest.mark.parametrize("extra", [RUN, {**RUN, "attempt": 3}, {},
                                    {**RUN, "run_id": "stale"},
                                    {**RUN, "workflow_id": "other"},
                                    {**RUN, "attempt": 1}])
@pytest.mark.parametrize("owned", [False, True])
def test_attempt_owner_check_is_read_only(w, monkeypatch, extra, owned):
    key, = seed(w, [("A", [])])
    if owned:
        # Only a worker may RECORD an owner now (V2-1): seed it, the router
        # run-carrying move below only checks the triple it no longer plants.
        seed_exec_owner(w, key, ("wf", "run", 2))
        w.store.set_state(w.p("router"), key, "running", run=("wf", "run", 2))

    def snapshot():
        return tuple(w.conn.execute(
            "SELECT exec_workflow_id, exec_run_id, exec_attempt, updated_at"
            " FROM items WHERE key=?", (key,)).fetchone())

    before = snapshot()

    def no_write():
        pytest.fail("worker_attempt must not open a write transaction")

    monkeypatch.setattr(w.store, "_write", no_write)
    body = {"card": key, "n": 1, "exit_class": "check_fail", **extra}
    stale = owned and (not extra or extra["run_id"] != "run"
                       or extra["workflow_id"] != "wf" or extra["attempt"] < 2)
    if stale:
        with pytest.raises(Refused) as exc:
            w.store.worker_attempt(w.p("workerA"), body)
        assert exc.value.code == "exec_stale"
    else:
        assert w.store.worker_attempt(w.p("workerA"), body)["n"] == 1
    assert snapshot() == before
