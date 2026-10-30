"""tests/test_spawner_sessions_r6.py — SESSION-SPAWN-MCP r7 slice 1 tests.

Pin the r6 seat findings: name locks keyed case-insensitively (record paths
collide on case-insensitive filesystems), audit lines on _ActionFailed and
OSError/SubprocessError outcomes carry the started session's id, and
_audit_argv is per-thread so concurrent actions cannot cross-audit argv.
"""

from __future__ import annotations

import json
import threading

from plangraph.spawner.sessions_agents import _ActionFailed
from sessions_support import UUID1, make
from sessions_support import site  # noqa: F401


def test_name_lock_shared_across_case(site):
    s = make(site)
    assert s._name_lock("Alpha") is s._name_lock("alpha")


def test_action_failed_audit_carries_session_id(site):
    s = make(site)

    def fn():
        raise _ActionFailed("x", session_id=UUID1)

    out = s._action("launch", "alpha", fn)
    assert out["session_id"] == UUID1  # id of the started session is not lost
    lines = [json.loads(l) for l in
             (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["action"] == "launch" and lines[0]["ok"] is False
    assert lines[0]["session_id"] == UUID1


def test_action_failed_oserror_audit_carries_session_id(site):
    s = make(site)

    def fn():
        exc = OSError("disk gone")
        exc.session_id = UUID1
        raise exc

    out = s._action("launch", "alpha", fn)
    assert out["session_id"] == UUID1
    lines = [json.loads(l) for l in
             (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["ok"] is False and lines[0]["session_id"] == UUID1


def test_audit_argv_is_per_thread(site):
    s = make(site)
    assert s._audit_argv is None
    seen = {}

    def worker():
        s._audit_argv = ["w"]
        seen["worker"] = s._audit_argv

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["worker"] == ["w"]
    assert s._audit_argv is None  # main thread never saw the worker's argv

    s._audit_argv = ["m"]
    seen2 = {}

    def worker2():
        seen2["worker"] = s._audit_argv

    t2 = threading.Thread(target=worker2)
    t2.start()
    t2.join()
    assert seen2["worker"] is None  # worker never saw the main thread's argv
    assert s._audit_argv == ["m"]
