import pytest

from keeper_util import World, seed, seed_exec_owner
from plangraph.keeper.errors import Refused


def test_unassigned_worker_cannot_plant_owner(tmp_path):
    w = World(tmp_path)
    try:
        key, = seed(w, [("A", [])])
        w.conn.execute("UPDATE items SET delegate=NULL WHERE key=?", (key,))
        w.conn.commit()
        with pytest.raises(Refused) as exc:
            w.store.worker_alarm(w.p("workerB"), {"card": key, "reason": "stuck",
                "workflow_id": "wf", "run_id": "foreign", "attempt": 1})
        assert exc.value.code == "forbidden"
        assert w.store._item(key)["exec_run_id"] is None
        w.conn.execute("UPDATE items SET delegate='workerA' WHERE key=?", (key,))
        w.conn.commit()
        w.store.worker_alarm(w.p("workerA"), {"card": key, "reason": "stuck",
            "workflow_id": "wf", "run_id": "real", "attempt": 1})
        assert w.store._item(key)["exec_run_id"] == "real"
    finally:
        w.close()


@pytest.mark.parametrize("state", ["waiting", "needs_input"])
def test_projector_resume(tmp_path, state):
    w = World(tmp_path)
    try:
        key, = seed(w, [("A", [])])
        w.store.set_state(w.p("router"), key, state)
        seed_exec_owner(w, key, ("wf", "run", 1))
        w.store.set_state(w.p("system"), key, "running")
        assert w.store._item(key)["exec_run_id"] == "run"
    finally:
        w.close()


def test_new_run_is_not_silently_ignored(tmp_path):
    w = World(tmp_path)
    try:
        key, = seed(w, [("A", [])])
        with pytest.raises(Refused) as exc:
            w.store.set_state(w.p("router"), key, "running", new_run=True)
        assert exc.value.code == "validation"
    finally:
        w.close()


def test_router_requeue(tmp_path):
    w = World(tmp_path)
    try:
        key, = seed(w, [("A", [])])
        w.store.worker_park(w.p("workerA"), {"card": key, "tier": "free",
            "exit_class": "check_fail", "workflow_id": "wf", "run_id": "run",
            "attempt": 1})
        with pytest.raises(Refused) as exc:
            w.store.set_state(w.p("l1main"), key, "open", hand_back=True)
        assert exc.value.code == "forbidden"
        w.store.requeue(w.p("router"), key)
        assert w.store._item(key)["state"] == "open"
        assert w.store._item(key)["exec_run_id"] is None
        with pytest.raises(Refused) as exc:
            w.store.worker_alarm(w.p("workerA"), {"card": key, "reason": "stuck",
                "workflow_id": "wf", "run_id": "run", "attempt": 1})
        assert exc.value.code == "exec_stale"
        w.store.claim(w.p("l1main"), key)
    finally:
        w.close()


def test_owner_changes_are_chained_and_retirement_is_bounded(tmp_path):
    import json
    from plangraph.keeper import events

    w = World(tmp_path)
    try:
        key, = seed(w, [("A", [])])
        for n in range(18):
            with w.store._write():
                w.store._exec_check(w.conn, key, "wf", f"run-{n}", 1,
                                    w.p("workerA"), now=w.clock())
                w.store._exec_check(w.conn, key, "wf", f"run-{n}", 2,
                                    w.p("workerA"), now=w.clock())
                w.store._exec_clear(w.conn, key)
        row = w.conn.execute("SELECT exec_retired_runs FROM items WHERE key=?",
                             (key,)).fetchone()
        retired = json.loads(row[0])
        assert len(retired) == 16
        assert retired[0]["run_id"] == "run-2"
        assert all(entry["attempt"] == 2 for entry in retired)
        count = w.conn.execute("SELECT COUNT(*) FROM events WHERE type=?",
                               ("exec_owner_changed",)).fetchone()[0]
        assert count == 54
        assert events.verify_chain(w.conn) == []
    finally:
        w.close()


def test_requeue_is_on_router_transport_surface():
    from plangraph.keeper import api_routes, mcp

    assert "requeue" in api_routes.WRITE_VERBS
    assert any(tool["name"] == "requeue" for tool in mcp.TOOLS)
