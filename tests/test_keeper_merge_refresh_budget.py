"""The keeper merge-index refresh: a scan budget, a terminal failed state and resume."""
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
