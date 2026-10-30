"""tests/test_spawner_sessions_r5.py — SESSION-SPAWN-MCP r5 seat findings.

Fail-closed fixes: a malformed `claude agents --json` row refuses the action
instead of reading as absence; a finished/stopped row of the launched name
never becomes the new session's id; _ActionFailed carries the session id.
"""

from __future__ import annotations

import json

from plangraph.spawner.sessions_agents import _ActionFailed
from sessions_support import UUID1, FakeRun, make, go, _stopped
from sessions_support import site  # noqa: F401


def test_launch_never_resolves_a_finished_row(site):
    """A finished row of the same name must not become the new session id:
    with no UUID in stdout and only that row listed, resolution is None and
    the launch result carries the unresolved-id warning."""
    row = {"id": "f", "sessionId": UUID1, "name": "alpha", "kind": "background",
           "state": "finished", "startedAt": 1_000_000}
    run = FakeRun(stdout="done\n", agents_rows=[row])
    s = make(site, run)
    res = go(site, s)
    assert res["session_id"] is None
    assert res["warning"] == "session id could not be resolved"


def test_strict_poll_malformed_row_refuses_relaunch(site):
    """`claude agents --json` answering with a non-object row must fail
    closed: the relaunch of a stopped record with an id is refused with ok
    False and nothing is launched."""
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    _stopped(site, run, s)
    launches = len(run.launch_calls())
    run.agents_rows = [1]  # malformed: one row that is not an object
    res = s.relaunch("alpha")
    assert res["ok"] is False
    assert len(run.launch_calls()) == launches  # fail closed: no new launch


def test_action_failed_carries_session_id():
    exc = _ActionFailed("x", session_id="s")
    assert exc.session_id == "s"
    assert str(exc) == "x"
    assert _ActionFailed("y").session_id is None


def test_dry_run_failure_writes_no_audit(site):
    """A dry run must write NOTHING, also on failure (seat): with a stopped
    record's old id to poll, the dry launch runs the poll - here it raises
    OSError - and the failure must still leave no audit.log at all."""
    sessions = site.state / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "alpha.json").write_text(json.dumps(
        {"name": "alpha", "session_id": UUID1, "state": "stopped", "stopped_ts": 900.0}))

    class BoomRun:
        def __call__(self, argv, **kw):
            raise OSError("runner exploded")

    s = make(site, BoomRun())
    res = go(site, s, dry_run=True)
    assert res["ok"] is False
    assert not (site.state / "sessions" / "audit.log").exists()


def test_failed_launch_outcome_carries_error_text(site):
    """A failing (non-dry) launch must tell the caller WHY it failed (seat):
    the outcome dict carries an "error" key with the same reason text the
    audit line gets - here the claude exit code."""
    run = FakeRun(stdout="boom\n", launch_rc=1)
    s = make(site, run)
    res = go(site, s)
    assert res["ok"] is False and res["name"] == "alpha"
    assert "exited 1" in res["error"]
    assert res["session_id"] is None


def test_find_record_never_crosses_case_folding_names(site, monkeypatch):
    """On a case-insensitive file system "Alpha.json" resolves to alpha.json:
    the record read must still be returned only under its own name, so
    _find_record("Alpha") must not find the record named "alpha"."""
    sessions = site.state / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "alpha.json").write_text(json.dumps({"name": "alpha"}))
    s = make(site)
    # simulate case-insensitive path resolution (macOS/Windows)
    monkeypatch.setattr(s, "_record_path", lambda n: sessions / f"{n.lower()}.json")
    assert s._find_record("Alpha", None) is None
    assert s._find_record("alpha", None) == {"name": "alpha"}  # same name still found


def test_name_lock_is_stable_per_name(site):
    s = make(site)
    assert s._name_lock("a") is s._name_lock("a")
    assert s._name_lock("a") is not s._name_lock("b")
