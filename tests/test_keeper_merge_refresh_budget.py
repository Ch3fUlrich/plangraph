"""The keeper merge-index refresh: a scan budget, a terminal failed state and resume."""
import pytest
from test_keeper_merge_close_auto import env, set_states  # noqa: F401  (the fixture and the item states)


def _await_scan(doors, path):
    import threading
    for thread in threading.enumerate():
        if thread.name == "keeper-merge-index":
            thread.join(10)
    return doors.http("system", "POST", path, {"refresh": "status"})


def test_refresh_deadline_refuses_close_and_allows_restart(env, tmp_path, monkeypatch):
    from door_util import Doors
    from plangraph.keeper import api_routes

    set_states(env)
    env.repo.land("c", "Plan-Item: demo:C", 84)
    doors = Doors(env, tmp_path)
    path = "/v1/close_merged_auto"
    try:
        monkeypatch.setattr(api_routes.MergeRefresh, "SCAN_BUDGET_SECONDS", 0)
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "failed"})
        refused = (409, {"error": "merge_index_failed", "msg": "merge index refresh failed; start a refresh"})
        assert doors.http("system", "POST", path, {"require_seats": False}) == refused
        assert doors.http("system", "POST", path, {"apply": False}) == refused
        status, body = doors.http("system", "POST", path, {"repos_only": True})
        assert status == 200 and "repos" in body
        assert env.store.show(env.p("router"), "demo:C")["state"] == "in_review"
        monkeypatch.setattr(api_routes.MergeRefresh, "SCAN_BUDGET_SECONDS", 180)
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "ready"})
        status, body = doors.http("system", "POST", path, {"require_seats": False})
        assert status == 200 and [c["key"] for c in body["closed"]] == ["demo:C"]
    finally:
        doors.close()


def test_resume_reuses_a_fresh_scan_that_another_caller_consumed(env, tmp_path, monkeypatch):
    from door_util import Doors
    from plangraph.keeper import api_routes

    set_states(env)
    env.repo.land("c", "Plan-Item: demo:C", 84)
    tick = [100.0]
    monkeypatch.setattr(api_routes, "monotonic", lambda: tick[0], raising=False)
    doors = Doors(env, tmp_path)
    path = "/v1/close_merged_auto"
    try:
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "ready"})
        assert doors.http("system", "POST", path, {"apply": False})[0] == 200      # a preview consumes it
        assert doors.http("system", "POST", path, {"refresh": "resume"}) == (200, {"scan": "ready"})
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "ready"})   # ordinary start still replaces it
        tick[0] += 300
        assert doors.http("system", "POST", path, {"refresh": "resume"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "ready"})
        assert doors.http("system", "POST", path, {"refresh": "bogus"})[0] == 400
    finally:
        doors.close()


def _hold_lock_past_budget(lock, seconds):
    """Take the publication mutex on another thread until the scan expires."""
    import threading
    import time

    held = threading.Event()

    def hold():
        with lock:
            held.set()
            time.sleep(seconds)

    threading.Thread(target=hold, daemon=True).start()
    assert held.wait(5)


def test_a_scan_that_waits_for_the_lock_past_its_budget_is_not_published(env, tmp_path, monkeypatch):
    from door_util import Doors
    from plangraph.keeper import api_routes

    set_states(env)
    doors = Doors(env, tmp_path)
    path = "/v1/close_merged_auto"
    try:
        monkeypatch.setattr(api_routes.MergeRefresh, "SCAN_BUDGET_SECONDS", 0.4)
        monkeypatch.setattr(type(env.store), "warm_merge_scan",
                            lambda store, candidates: _hold_lock_past_budget(doors.server.merge_refresh._lock, 0.8))
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "failed"})
        assert doors.http("system", "POST", path, {})[1]["error"] == "merge_index_failed"
    finally:
        doors.close()


def test_a_failed_scan_leaves_the_live_landing_cache_alone(env, tmp_path, monkeypatch):
    from door_util import Doors
    from plangraph.keeper import api_routes

    set_states(env)
    doors = Doors(env, tmp_path)
    path = "/v1/close_merged_auto"
    try:
        live = doors.server.store.__dict__.setdefault("_landings", {})
        named = doors.server.store._named_landings
        landing = ("demo:C", "a" * 40)

        real = type(env.store).warm_merge_scan

        def warm(store, candidates):
            real(store, candidates)
            store._landings[("demo", "a" * 40)] = ({"pr": 1}, 0)
            store._named_landings[landing] = "b" * 40

        monkeypatch.setattr(type(env.store), "warm_merge_scan", warm)
        monkeypatch.setattr(api_routes.MergeRefresh, "SCAN_BUDGET_SECONDS", 0)
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "failed"})
        assert ("demo", "a" * 40) not in live
        assert landing not in named
        monkeypatch.setattr(api_routes.MergeRefresh, "SCAN_BUDGET_SECONDS", 180)
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "ready"})
        assert ("demo", "a" * 40) not in doors.server.store.__dict__["_landings"]
        assert landing not in doors.server.store._named_landings
        assert doors.http("system", "POST", path, {"apply": False})[0] == 200
        assert ("demo", "a" * 40) in doors.server.store.__dict__["_landings"]
        assert doors.server.store._named_landings[landing] == "b" * 40
    finally:
        doors.close()


def test_refresh_candidate_read_and_warm_leave_request_lock_free(env, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from threading import Event
    from door_util import Doors

    doors = Doors(env, tmp_path)
    server, path = doors.server, "/v1/close_merged_auto"
    entered, permit, closed = Event(), Event(), Event()
    warming, warmed = Event(), Event()
    read, warm = server.read_store, type(env.store).warm_merge_scan

    @contextmanager
    def read_view(**kwargs):
        with read(**kwargs) as view:
            entered.set()
            assert permit.wait(3)
            yield view
        closed.set()

    def scan(store, candidates):
        assert closed.is_set() and store.conn is None
        warming.set()
        assert warmed.wait(3)
        return warm(store, candidates)

    monkeypatch.setattr(server, "read_store", read_view)
    monkeypatch.setattr(type(env.store), "warm_merge_scan", scan)
    try:
        assert doors.http("system", "POST", path, {"refresh": "start"})[0] == 200
        assert entered.wait(2)
        assert doors.http("system", "GET", "/v1/whoami")[0] == 200
        permit.set()
        assert warming.wait(2)
        assert doors.http("system", "GET", "/v1/whoami")[0] == 200
        warmed.set()
        assert _await_scan(doors, path) == (200, {"scan": "ready"})
    finally:
        permit.set()
        warmed.set()
        doors.close()


def test_the_named_pr_lookup_takes_what_is_left_of_the_shared_budget(tmp_path, monkeypatch):
    import subprocess

    from plangraph.keeper import context_sources, timing
    from plangraph.keeper.errors import Refused
    from plangraph.keeper.evidence_git import GitAdapter
    from test_keeper_close_merged_scan import _git, merged_repo

    repo = tmp_path / "repo"
    merged_repo(repo, [])
    _git(repo, "remote", "add", "origin", "https://github.com/acme/demo.git")
    adapter = GitAdapter({"demo": {"path": str(repo), "ref": "main"}})
    seen = []
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if cmd[0] != "gh":
            return real_run(cmd, **kw)
        seen.append((cmd[0], kw["timeout"]))
        raise OSError("no gh in the test")

    monkeypatch.setattr(context_sources.subprocess, "run", fake_run)
    with timing.io_budget(1):
        assert context_sources.named_merge(adapter, "demo", "#7") is None
    assert [timeout for cmd, timeout in seen if cmd == "gh"] and all(
        timeout <= 1 for cmd, timeout in seen if cmd == "gh")
    calls = len(seen)
    with timing.io_budget(0):   # nothing is left: no gh call starts
        try:
            context_sources.named_merge(adapter, "demo", "#7")
        except Refused:
            pass
    assert len(seen) == calls
    assert context_sources.named_merge(adapter, "demo", "#7") is None and seen[-1] == ("gh", 20)
