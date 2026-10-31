"""tests/test_spawner_sessions_r5.py — SESSION-SPAWN-MCP r5 seat findings.

Fail-closed fixes: a malformed `claude agents --json` row refuses the action
instead of reading as absence; a finished/stopped row of the launched name
never becomes the new session's id; _ActionFailed carries the session id.
"""

from __future__ import annotations

import json
import os

import pytest

from plangraph.routing_errors import ParameterRefused
from plangraph.spawner.sessions_agents import _ActionFailed
from sessions_support import UUID1, UUID2, FakeRun, make, go, write_config, _ready, _stopped
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


def test_name_shape_accepts_fleet_names_refuses_marker_collisions(site):
    """Spec ^[A-Za-z0-9._-]{1,64}$ plus the no-leading-dash guard: fleet names
    like build-L1 and a.b_c pass and dry-launch; a leading dash or dot, or a
    .ready suffix (would collide with <name>.ready.json markers, any case),
    is refused."""
    run = FakeRun()
    s = make(site, run)
    for ok in ("build-L1", "a.b_c"):
        assert go(site, s, name=ok, dry_run=True)["argv"]
    for bad in ("-x", ".x", "x.ready", "X.READY"):
        with pytest.raises(ParameterRefused, match="name"):
            go(site, s, name=bad, dry_run=True)
    assert not run.calls  # every refusal came before any run


def test_config_style_text_leading_dash_refused(site):
    """A config style_text starting with "-" would read as a CLI flag in the
    built argv: it is refused at style_text assembly (seat finding)."""
    cfg2 = site.tmp_path / "dash.json"
    write_config(cfg2, json.dumps({**json.loads(site.cfg.read_text()), "style_text": "-x"}))
    s = make(site, FakeRun(), config_path=cfg2)
    with pytest.raises(ParameterRefused, match="style_text"):
        go(site, s, name="alpha", dry_run=True)


def test_relaunch_handoff_must_be_strictly_newer_than_launch(site):
    """Freshness compares against the LAST SUCCESSFUL LAUNCH (seat finding):
    a handoff whose mtime merely equals rec["launched_ts"] is stale and the
    relaunch is refused; a strictly newer one relaunches."""
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    rec_path, _marker = _ready(site, run, s)
    rec = json.loads(rec_path.read_text())
    rec["launched_ts"] = 2_000_000.0  # the launch time to beat (later than ts)
    rec_path.write_text(json.dumps(rec))
    hf = site.tmp_path / "retry.md"
    hf.write_text("carry on again")
    os.utime(hf, (rec["launched_ts"], rec["launched_ts"]))  # equal to the launch
    with pytest.raises(ParameterRefused, match="newer"):
        s.relaunch("alpha", handoff=str(hf))
    os.utime(hf, (rec["launched_ts"] + 1, rec["launched_ts"] + 1))  # strictly newer
    res = s.relaunch("alpha", handoff=str(hf))
    assert res["ok"] is True and res["name"] == "alpha"


def test_stop_of_stopped_record_with_live_id_stops_again(site):
    """Seat HIGH: stop never reports ok behind a live id. A stopped record
    whose id is listed running is stopped again - `claude stop <id>` runs and
    only a confirmed stop reports ok."""
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",  # r7 adapt
                        "kind": "background", "state": "running",
                        "startedAt": 1_000_000}]
    s.stop(name="alpha")
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",
                        "kind": "background", "state": "running", "startedAt": 1_000_000}]
    res = s.stop(name="alpha")  # the stopped record's id is still live
    assert res["ok"] is True and res["session_id"] == UUID1
    stops = [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert stops == [["claude", "stop", UUID1[:8]], ["claude", "stop", UUID1[:8]]]


def test_relaunch_resolved_old_id_is_never_the_new_id(site):
    """R-13: a null-id record's relaunch stops the by-name resolved id first
    (here UUID2); with "done" in the launch stdout the new id is read from
    the rows AFTER that stop - the finished UUID2 row can never be it, so
    result and record carry UUID1, never UUID2."""
    class SwapRun(FakeRun):
        def _finish_rows(self, sid):
            # the stop ends UUID2; the relaunch's own row appeared meanwhile
            self.agents_rows[:] = [
                {"id": "a", "sessionId": UUID2, "name": "alpha", "kind": "background",
                 "state": "finished", "startedAt": 1_005_000},  # newest, but replaced
                {"id": "b", "sessionId": UUID1, "name": "alpha", "kind": "background",
                 "state": "running", "startedAt": 1_000_000},
            ]

    run = SwapRun(stdout="done\n")
    s = make(site, run, resolve_budget=0.0)
    rec_path, _marker = _ready(site, run, s)
    run.agents_rows = [{"id": "x", "sessionId": UUID2, "name": "alpha",
                        "kind": "background", "state": "running", "startedAt": 1_005_000}]
    res = s.relaunch("alpha")
    assert res["ok"] is True
    assert res["session_id"] == UUID1  # never the replaced UUID2
    assert json.loads(rec_path.read_text())["session_id"] == UUID1
    assert [a[2] for a, _ in run.calls if a[:2] == ["claude", "stop"]] == [UUID2[:8]]


def test_failed_relaunch_retry_with_same_handoff_ok(site):
    """Freshness points at the last SUCCESSFUL launch (seat): with an
    advancing clock and a handoff written after the first launch, a failed
    relaunch must not advance launched_ts - the retry relaunches with the
    same handoff instead of refusing it as stale."""
    class Clock:
        def __init__(self):
            self.t = 2_000_000.0

        def __call__(self):
            self.t += 10.0
            return self.t

    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run, now=Clock())
    rec_path, _marker = _stopped(site, run, s)
    launched_ts = json.loads(rec_path.read_text())["launched_ts"]
    hf = site.tmp_path / "handoff.md"
    os.utime(hf, (launched_ts + 1, launched_ts + 1))  # written after the launch
    run.launch_rc = 1
    assert s.relaunch("alpha")["ok"] is False  # the relaunch fails outright
    run.launch_rc = 0
    res = s.relaunch("alpha")  # the retry uses the same (marker) handoff
    assert res["ok"] is True and res["session_id"] == UUID1


def test_record_write_failure_after_start_reports_started_id(site, monkeypatch):
    """The started id survives a failing record write (round 4): the
    _ActionFailed carries session_id=sid, so the result is the id the
    session was started with - never None."""
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    real = s._write_record

    def dead(name, rec):
        if rec.get("state") == "running":  # only the post-start write fails
            raise OSError("no space left on device")
        return real(name, rec)

    monkeypatch.setattr(s, "_write_record", dead)
    res = go(site, s)
    assert res["ok"] is False
    assert res["session_id"] == UUID1  # the started id reaches the result
    assert "record write failed" in res["error"]
