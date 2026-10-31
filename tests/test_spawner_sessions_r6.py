"""tests/test_spawner_sessions_r6.py — SESSION-SPAWN-MCP r7 slice 1+2 tests.

Pin the r6 seat findings: name locks keyed case-insensitively (record paths
collide on case-insensitive filesystems), audit lines on _ActionFailed and
OSError/SubprocessError outcomes carry the started session's id, and
_audit_argv is per-thread so concurrent actions cannot cross-audit argv.
Slice 2: names that case-fold onto an existing record or onto Windows
reserved device names are refused at launch, and _claude_head accepts an
upper-case .EXE on win32 while still refusing .cmd shims.
Slice 3: stop and mark_ready take the name lock too (an id-only stop locks
the resolved record name), and launched_ts is stamped only on a resolved
launch.
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


def _running_record(site):
    """A hand-written running record for alpha with session id UUID1 - the
    state a stop must serialize against, with no prior launch calls."""
    sess = site.state / "sessions"
    sess.mkdir(parents=True)
    (sess / "alpha.json").write_text(json.dumps({
        "name": "alpha", "state": "running", "session_id": UUID1,
        "ts": 1000, "stopped_ts": None}) + "\n")
    return sess


def test_stop_blocked_by_name_lock(site):
    # A stop racing a relaunch read the pre-stop record, confirmed its own
    # stop and overwrote the record _start had just written (r6 seat): stop
    # takes the name lock and _stop_inner re-reads the record under it.
    run = FakeRun()
    s = make(site, run)
    _running_record(site)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",  # r7 adapt
                        "kind": "background", "state": "running",
                        "startedAt": 1_000_000}]
    lock = s._name_lock("alpha")
    lock.acquire()
    out = {}

    def worker():
        out["res"] = s.stop(name="alpha")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        t.join(0.3)
        assert t.is_alive()  # stop waits for the name lock
        assert run.calls == []  # no claude stop ran while the lock is held
    finally:
        lock.release()
    t.join(5)
    assert not t.is_alive() and out["res"]["ok"] is True
    assert [a[2] for a, _ in run.calls if a[:2] == ["claude", "stop"]] == [UUID1]


def test_stop_by_id_blocked_by_record_name_lock(site):
    # An id-only stop locks the RESOLVED record name, not the id string -
    # otherwise a launch on the name races straight through (r6 seat).
    run = FakeRun()
    s = make(site, run)
    _running_record(site)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",  # r7 adapt
                        "kind": "background", "state": "running",
                        "startedAt": 1_000_000}]
    lock = s._name_lock("alpha")
    lock.acquire()
    out = {}

    def worker():
        out["res"] = s.stop(session_id=UUID1)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        t.join(0.3)
        assert t.is_alive() and run.calls == []  # blocked before any call
    finally:
        lock.release()
    t.join(5)
    assert not t.is_alive() and out["res"]["ok"] is True
    assert out["res"]["name"] == "alpha"
    assert [a[2] for a, _ in run.calls if a[:2] == ["claude", "stop"]] == [UUID1]


def test_mark_ready_blocked_by_name_lock(site):
    # mark_ready writes the ready marker and must serialize with launch and
    # relaunch on the same name too (r6 seat).
    run = FakeRun()
    s = make(site, run)
    sess = _running_record(site)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    marker = sess / "alpha.ready.json"
    lock = s._name_lock("alpha")
    lock.acquire()
    out = {}

    def worker():
        out["res"] = s.mark_ready("alpha", str(hf))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        t.join(0.3)
        assert t.is_alive()  # mark_ready waits for the name lock
        assert not marker.exists()  # no marker written while the lock is held
    finally:
        lock.release()
    t.join(5)
    assert not t.is_alive() and out["res"]["ok"] is True
    assert marker.exists()


def test_stop_absent_row_marks_stopped_without_claude_stop(site):
    # Sonnet r7: a running record whose session already ended has no row to
    # stop - polling must recover the name instead of refusing forever. r8
    # (qwen MED): an absent row no longer means "already gone" - `claude stop
    # <id>` runs once; rc 0 confirms, and the name is still recovered.
    run = FakeRun()
    s = make(site, run)
    _running_record(site)
    res = s.stop(name="alpha")
    assert res["ok"] is True and res["session_id"] == UUID1
    assert [a for a, _ in run.calls if a[:2] == ["claude", "stop"]] == \
        [["claude", "stop", UUID1]]  # attempted once on the unlisted id
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "stopped" and rec["session_id"] == UUID1
    assert rec["stopped_ts"] is not None


def test_stop_finished_row_marks_stopped_without_claude_stop(site):
    # Sonnet r7: a finished row is just as gone as no row at all - the record
    # is marked stopped without ever calling claude stop.
    run = FakeRun(agents_rows=[
        {"id": "r", "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "finished", "startedAt": 1_000_000}])
    s = make(site, run)
    _running_record(site)
    res = s.stop(name="alpha")
    assert res["ok"] is True and res["session_id"] == UUID1
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "stopped" and rec["session_id"] == UUID1
    assert rec["stopped_ts"] is not None


def test_stop_null_id_skips_finished_rows_in_resolution(site):
    # qwen r7: the null-id by-name resolution must skip FINISHED_STATES rows
    # like _resolve_id does - a finished row is not this record's live
    # session and its id must never be attributed to the record or stopped.
    run = FakeRun(stdout="done\n")  # launch resolves no id
    s = make(site, run, resolve_budget=0.0)
    res = go(site, s)
    assert res["session_id"] is None
    run.agents_rows = [
        {"id": "f", "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "finished", "startedAt": 1_000_000}]  # finished, in window
    res = s.stop(name="alpha")
    assert res["ok"] is True and res["session_id"] is None
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "stopped" and rec["session_id"] is None
    assert rec["stopped_ts"] is not None


def test_stop_live_row_still_calls_claude_stop_then_confirms(site):
    # The existing behaviour stays: a live row is stopped for real, and the
    # strict poll after the stop confirms the id is gone (D-3193). The one
    # pre-stop poll proves the row was checked before claude ran.
    run = FakeRun()
    s = make(site, run)
    _running_record(site)
    run.agents_rows = [
        {"id": "r", "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "running", "startedAt": 1_000_000}]
    res = s.stop(name="alpha")
    assert res["ok"] is True and res["session_id"] == UUID1
    assert [a for a, _ in run.calls if a[:2] == ["claude", "stop"]] == \
        [["claude", "stop", UUID1]]  # exact argv, never rm/kill
    assert [a[:2] for a, _ in run.calls] == \
        [["claude", "agents"], ["claude", "stop"], ["claude", "agents"]]
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "stopped" and rec["session_id"] == UUID1


def test_unresolved_id_leaves_launched_ts_unset(site):
    # _start stamped launched_ts even when the id stayed unresolved (r6
    # qwen LOW): the stamp marks the last SUCCESSFUL launch only - unset on
    # a fresh unknown-id record, unchanged when the record carries one.
    ticks = iter([1000.0 + 10.0 * i for i in range(100)])
    run = FakeRun(stdout="done\n")  # no id anywhere
    s = make(site, run, resolve_budget=0.0, now=lambda: next(ticks))
    res = go(site, s)
    assert res["ok"] is True and res["session_id"] is None
    rec_path = site.state / "sessions" / "alpha.json"
    rec = json.loads(rec_path.read_text())
    assert rec["state"] == "unknown" and rec.get("launched_ts") is None
    rec["launched_ts"] = 1500.0  # as if an earlier launch had resolved
    rec_path.write_text(json.dumps(rec) + "\n")
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    res = s.relaunch("alpha")  # still no resolvable id
    assert res["ok"] is True and res["session_id"] is None
    rec = json.loads(rec_path.read_text())
    assert rec["state"] == "unknown"
    assert rec["launched_ts"] == 1500.0  # unchanged from before


def test_audit_argv_cleared_after_action(site):
    s = make(site)

    def fn():
        s._audit_argv = ["claude", "--bg"]
        raise OSError("boom")

    s._action("launch", "alpha", fn)
    assert make(site)._audit_argv is None
