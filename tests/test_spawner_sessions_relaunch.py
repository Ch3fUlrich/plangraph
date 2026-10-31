"""tests/test_spawner_sessions_relaunch.py — SESSION-SPAWN-MCP relaunch tests."""

from __future__ import annotations

import json
import subprocess
import pytest
from plangraph.routing_errors import ParameterRefused
from sessions_support import UUID1, UUID2, UUID3, PROMPT, FakeRun, make, go, _ready, _assert_relaunch_failure_kept, _stopped
from sessions_support import site  # noqa: F401


def test_relaunch_keeps_name_and_uses_handoff(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha")
    assert res["name"] == "alpha" and res["session_id"] == UUID2
    launches = run.launch_calls()
    assert len(launches) == 2
    assert launches[1][-1] == f"Restart from handoff {hf.resolve()}.\n\n" + PROMPT
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["name"] == "alpha" and rec["session_id"] == UUID2
    assert rec["handoff"] == str(hf.resolve()) and rec["children"] == []
    assert not (site.state / "sessions" / "alpha.ready.json").exists()
    actions = [json.loads(l)["action"] for l in
               (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert actions == ["launch", "mark_ready", "relaunch"]
    res2 = s.relaunch("alpha", handoff=str(hf))  # a handoff argument works without a marker
    assert res2["name"] == "alpha" and len(run.launch_calls()) == 3


def test_relaunch_without_marker_or_handoff_refused(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    with pytest.raises(ParameterRefused, match="ready marker or handoff required"):
        s.relaunch("alpha")  # neither a ready marker nor a handoff argument
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]  # nothing was stopped


def test_relaunch_stop_failure_launches_nothing(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",  # r7 adapt
                        "kind": "background", "state": "running",
                        "startedAt": 1_000_000}]
    run.stop_rc = 1
    with pytest.raises(ParameterRefused, match="stop failed"):
        s.relaunch("alpha")
    assert len(run.launch_calls()) == 1  # only the original launch


def test_relaunch_marker_handoff_not_string_refuses(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    marker = site.state / "sessions" / "alpha.ready.json"
    marker.write_text(json.dumps({"handoff": 1234, "card": None, "ts": 1000.0}))
    with pytest.raises(ParameterRefused, match="handoff"):
        s.relaunch("alpha")
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]


def test_relaunch_validation_failure_stops_nothing(site, tmp_path_factory):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    stray = tmp_path_factory.mktemp("stray3")
    bad = stray / "handoff.md"
    bad.write_text("state")
    with pytest.raises(ParameterRefused, match="handoff"):
        s.relaunch("alpha", handoff=str(bad))  # handoff outside the roots
    # a stored spec that no longer validates: nothing is stopped either
    (site.state / "sessions" / "alpha.prompt.txt").unlink()
    hf = site.tmp_path / "handoff.md"
    hf.write_text("state")
    s.mark_ready("alpha", str(hf))
    with pytest.raises(ParameterRefused, match="prompt_file"):
        s.relaunch("alpha")
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]  # nothing was stopped


def test_relaunch_launch_failure_after_stop_keeps_record(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    run.launch_rc = 1  # the relaunch's claude exits 1 after the old one is stopped
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha")
    assert res["ok"] is False and res["session_id"] is None
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    # _start's outcome record stands (D-3193): stopped after the failed start,
    # session_id null - the pre-relaunch record is no longer restored
    assert rec["state"] == "stopped" and rec["stopped_ts"] is not None
    assert rec["session_id"] is None
    assert (site.state / "sessions" / "alpha.ready.json").exists()  # a retry can relaunch
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert lines[-1]["action"] == "relaunch" and lines[-1]["ok"] is False
    run.launch_rc = 0
    retry = s.relaunch("alpha")  # marker survived: a plain retry works
    assert retry["ok"] is True and retry["session_id"] == UUID2


@pytest.mark.parametrize("state", ["running", "idle", None])
def test_relaunch_already_stopped_live_row_is_stopped_first(site, state):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",  # r7 adapt
                        "kind": "background", "state": "running",
                        "startedAt": 1_000_000}]
    _stopped(site, run, s)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",
                        "kind": "background", "state": state, "startedAt": 1_000_000}]
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha")
    assert res["ok"] is True and res["session_id"] == UUID2
    kinds = [a[:2] for a, _ in run.calls]
    # the old session is looked up in claude agents and stopped BEFORE the
    # relaunch; each stop is pre-checked and confirmed by agents polls (D-3193)
    assert kinds == [["claude", "--bg"], ["claude", "agents"], ["claude", "stop"],
                     ["claude", "agents"], ["claude", "agents"], ["claude", "agents"],
                     ["claude", "stop"], ["claude", "agents"], ["claude", "--bg"]]
    assert [a for a, _ in run.calls if a[:2] == ["claude", "stop"]] == \
        [["claude", "stop", UUID1[:8]], ["claude", "stop", UUID1[:8]]]  # short job id, never rm/kill
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] == UUID2 and rec["stopped_ts"] is None


def test_relaunch_already_stopped_stop_failure_launches_nothing(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    rec_path, marker_path = _stopped(site, run, s)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",
                        "kind": "background", "state": "running", "startedAt": 1_000_000}]
    run.stop_rc = 1
    with pytest.raises(ParameterRefused, match="stop failed"):
        s.relaunch("alpha")
    assert len(run.launch_calls()) == 1  # nothing was relaunched
    rec = json.loads(rec_path.read_text())
    assert rec["session_id"] == UUID1 and rec["stopped_ts"] is not None
    assert marker_path.exists()  # a retry can relaunch


def test_relaunch_already_stopped_unlisted_id_proceeds_without_stop(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",  # r7 adapt
                        "kind": "background", "state": "running",
                        "startedAt": 1_000_000}]
    _stopped(site, run, s)
    run.agents_rows = [{"id": "r", "sessionId": UUID3, "name": "alpha",
                        "kind": "background", "state": "running", "startedAt": 1_000_000}]
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha")
    assert res["ok"] is True and res["session_id"] == UUID2
    stops = [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert stops == [["claude", "stop", UUID1[:8]]]  # only the earlier user stop
    assert any(a[:3] == ["claude", "agents", "--json"] for a, _ in run.calls)  # id was looked up
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] == UUID2 and rec["stopped_ts"] is None


@pytest.mark.parametrize("state", ["finished", "stopped"])
def test_relaunch_already_stopped_finished_row_proceeds_without_stop(site, state):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",  # r7 adapt
                        "kind": "background", "state": "running",
                        "startedAt": 1_000_000}]
    _stopped(site, run, s)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",
                        "kind": "background", "state": state, "startedAt": 1_000_000}]
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha")
    assert res["ok"] is True and res["session_id"] == UUID2
    stops = [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert stops == [["claude", "stop", UUID1[:8]]]  # already over: no second stop
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] == UUID2 and rec["stopped_ts"] is None


def test_relaunch_timeout_keeps_record_and_marker(site):
    class TimeoutRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"] and self.broken:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            return super().__call__(argv, **kw)

    run = TimeoutRun(stdout="session " + UUID1)
    run.broken = False
    s = make(site, run)
    rec_path, marker_path = _ready(site, run, s)
    run.stdout = "session " + UUID2
    run.broken = True
    with pytest.raises(subprocess.TimeoutExpired):
        s.relaunch("alpha")
    _assert_relaunch_failure_kept(rec_path, marker_path, site, "unknown")


def test_relaunch_oserror_keeps_record_and_marker(site):
    class OsErrRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"] and self.broken:
                raise OSError("exec format error")
            return super().__call__(argv, **kw)

    run = OsErrRun(stdout="session " + UUID1)
    run.broken = False
    s = make(site, run)
    rec_path, marker_path = _ready(site, run, s)
    run.stdout = "session " + UUID2
    run.broken = True
    res = s.relaunch("alpha")
    assert res["ok"] is False
    _assert_relaunch_failure_kept(rec_path, marker_path, site, "unknown")


def test_relaunch_record_write_failure_keeps_record_and_marker(site, monkeypatch):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    rec_path, marker_path = _ready(site, run, s)
    run.stdout = "session " + UUID2
    real = s._write_record

    def flaky(name, rec):
        if rec.get("session_id") == UUID2:  # only the NEW record's write fails
            raise OSError("no space left on device")
        return real(name, rec)

    monkeypatch.setattr(s, "_write_record", flaky)
    res = s.relaunch("alpha")
    assert res["ok"] is False
    _assert_relaunch_failure_kept(rec_path, marker_path, site, "launching")


def test_relaunch_already_stopped_poll_failure_refuses(site):
    class BrokenPoll(FakeRun):
        def __call__(self, argv, **kw):
            if self._is_agents(argv) and self.poll_broken:
                raise OSError("agents unavailable")
            return super().__call__(argv, **kw)

    run = BrokenPoll(stdout="session " + UUID1)
    run.poll_broken = False
    s = make(site, run)
    rec_path, marker_path = _stopped(site, run, s)
    run.poll_broken = True
    res = s.relaunch("alpha")
    assert res["ok"] is False  # fail closed: the old id could not be checked
    assert len(run.launch_calls()) == 1  # nothing was relaunched
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "relaunch" and line["ok"] is False
    assert "agents poll failed" in line["reason"]
    rec = json.loads(rec_path.read_text())
    assert rec["session_id"] == UUID1 and rec["stopped_ts"] is not None
    assert marker_path.exists()  # a retry can relaunch once polling works


def test_relaunch_already_stopped_poll_garbage_refuses(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    rec_path, marker_path = _stopped(site, run, s)
    run.agents_rows = {"not": "a list"}  # garbage where rows should be
    res = s.relaunch("alpha")
    assert res["ok"] is False  # garbage is no answer: the relaunch is refused
    assert len(run.launch_calls()) == 1  # nothing was relaunched
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "relaunch" and line["ok"] is False
    assert "agents poll failed" in line["reason"]
    rec = json.loads(rec_path.read_text())
    assert rec["session_id"] == UUID1 and rec["stopped_ts"] is not None
    assert marker_path.exists()


def test_relaunch_record_write_failure_reports_new_id(site, monkeypatch):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    rec_path, marker_path = _ready(site, run, s)
    run.stdout = "session " + UUID2
    real = s._write_record

    def flaky(name, rec):
        if rec.get("session_id") == UUID2:  # only the NEW record's write fails
            raise OSError("no space left on device")
        return real(name, rec)

    monkeypatch.setattr(s, "_write_record", flaky)
    res = s.relaunch("alpha")
    assert res["ok"] is False
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "relaunch" and line["ok"] is False
    assert f"session started (id {UUID2}) but record write failed" in line["reason"]
    _assert_relaunch_failure_kept(rec_path, marker_path, site, "launching")


def test_relaunch_unknown_record_null_id_stops_by_name_then_launches(site):
    run = FakeRun(stdout="done\n")
    s = make(site, run, resolve_budget=0.0)
    res = go(site, s)
    assert res["ok"] is True and res["session_id"] is None and "warning" in res
    rec_path = site.state / "sessions" / "alpha.json"
    assert json.loads(rec_path.read_text())["state"] == "unknown"
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    run.agents_rows = [{"id": "x", "sessionId": UUID2, "name": "alpha",
                        "kind": "background", "startedAt": 1_000_000}]
    run.stdout = "session " + UUID1
    res = s.relaunch("alpha")
    assert res["ok"] is True and res["session_id"] == UUID1
    stops = [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert stops == [["claude", "stop", UUID2[:8]]]  # null id resolved by name, then stopped
    assert len(run.launch_calls()) == 2
    rec = json.loads(rec_path.read_text())
    assert rec["state"] == "running" and rec["session_id"] == UUID1 and rec["stopped_ts"] is None
    assert not (site.state / "sessions" / "alpha.ready.json").exists()


def test_relaunch_launch_failure_record_state_follows_start(site):
    class FailRun(FakeRun):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.fail_mode = None

        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"] and self.fail_mode == "timeout":
                raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
            return super().__call__(argv, **kw)

    run = FailRun(stdout="session " + UUID1)
    s = make(site, run)
    rec_path, marker_path = _ready(site, run, s)
    run.stdout = "session " + UUID2

    run.fail_mode = "timeout"  # the process may have started: "unknown" per _start
    with pytest.raises(subprocess.TimeoutExpired):
        s.relaunch("alpha")
    rec = json.loads(rec_path.read_text())
    assert rec["state"] == "unknown" and rec["stopped_ts"] is None
    assert marker_path.exists()  # marker kept: a retry can relaunch

    run.fail_mode = None
    run.launch_rc = 1  # claude exits non-zero: "stopped" per _start
    res = s.relaunch("alpha")
    assert res["ok"] is False and res["session_id"] is None
    rec = json.loads(rec_path.read_text())
    assert rec["state"] == "stopped" and rec["stopped_ts"] is not None
    assert marker_path.exists()
