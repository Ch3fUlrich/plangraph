"""tests/test_spawner_sessions_r5.py — SESSION-SPAWN-MCP r5 seat findings.

Fail-closed fixes: a malformed `claude agents --json` row refuses the action
instead of reading as absence; a finished/stopped row of the launched name
never becomes the new session's id; _ActionFailed carries the session id.
"""

from __future__ import annotations

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
