"""tests/test_spawner_sessions_r6.py — SESSION-SPAWN-MCP r7 slice 1+2 tests.

Pin the r6 seat findings: name locks keyed case-insensitively (record paths
collide on case-insensitive filesystems), audit lines on _ActionFailed and
OSError/SubprocessError outcomes carry the started session's id, and
_audit_argv is per-thread so concurrent actions cannot cross-audit argv.
Slice 2: names that case-fold onto an existing record or onto Windows
reserved device names are refused at launch, and _claude_head accepts an
upper-case .EXE on win32 while still refusing .cmd shims.
"""

from __future__ import annotations

import json
import threading

import pytest

from plangraph.routing_errors import ParameterRefused
from plangraph.spawner.sessions_agents import _ActionFailed
from sessions_support import UUID1, FakeRun, go, make
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


def test_launch_refuses_name_clashing_by_case(site):
    # A launch as "Alpha" must never reach the existing-record gate that
    # _find_record("Alpha") misses on a case-insensitive filesystem: the
    # write would overwrite the live record alpha.json (r6 seat finding).
    run = FakeRun()
    s = make(site, run)
    sess = site.state / "sessions"
    sess.mkdir(parents=True)
    rec_path = sess / "alpha.json"
    rec_path.write_text(json.dumps({"name": "alpha", "state": "running",
                                    "session_id": UUID1, "ts": 1000,
                                    "stopped_ts": None}) + "\n")
    before = rec_path.read_text()
    with pytest.raises(ParameterRefused, match="clashes"):
        go(site, s, name="Alpha")
    assert rec_path.read_text() == before  # live record unchanged
    assert run.calls == []  # no claude invocation ever ran


def test_check_name_refuses_windows_device_names(site):
    s = make(site)
    for name in ("con", "NUL", "aux.x", "com1", "lpt9"):
        with pytest.raises(ParameterRefused, match="reserved device name"):
            s._check_name(name)
    assert s._check_name("console") == "console"  # not a device name
    assert s._check_name("com10") == "com10"  # ports are com1..com9 only


def test_claude_head_win32_exe_case_insensitive(site):
    # CLAUDE.EXE is the real executable: refusing it broke Windows (r6 seat).
    s = make(site, platform="win32", which=lambda _cmd: "C:/x/CLAUDE.EXE")
    assert s._claude_head() == ["C:/x/CLAUDE.EXE"]
    shim = make(site, platform="win32", which=lambda _cmd: "C:/x/claude.cmd")
    with pytest.raises(ParameterRefused, match="shim"):
        shim._claude_head()
