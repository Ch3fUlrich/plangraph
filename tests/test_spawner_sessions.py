"""tests/test_spawner_sessions.py — SESSION-SPAWN-MCP launch records, dry-run and windows argv."""

from __future__ import annotations

import json
import subprocess
import pytest
from plangraph.routing_errors import ParameterRefused
from sessions_support import UUID1, UUID2, PROMPT, DETACHED, FakeRun, make, go
from sessions_support import site  # noqa: F401


def test_argv_windows_bg_and_wt(site, monkeypatch):
    exe = str(site.tmp_path / "claude.exe")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run, platform="win32", which=lambda _: exe)
    go(site, s)
    argv, kw = run.calls[0]
    assert argv[:2] == [exe, "--bg"]  # the resolved .exe path is argv[0]
    assert kw["creationflags"] == DETACHED
    assert kw.get("shell") in (None, False)


def test_handoff_prefixes_prompt(site):
    hf = site.tmp_path / "handoff.md"
    hf.write_text("state at stop")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s, handoff=str(hf))
    assert run.calls[0][0][-1] == f"Restart from handoff {hf.resolve()}.\n\n" + PROMPT


def test_dry_run_writes_nothing(site):
    run = FakeRun()
    s = make(site, run)
    res = go(site, s, prompt="abcdefg", dry_run=True)
    assert set(res) == {"argv", "cwd", "env_keys"}
    assert res["argv"][-1] == "<7 chars>"
    assert all(isinstance(a, str) for a in res["argv"])
    assert "FLEET_SESSION_NAME" in res["env_keys"]
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in res["env_keys"]
    assert res["cwd"] == str(site.proj.resolve())
    assert run.calls == []
    sessions_dir = site.state / "sessions"
    with pytest.raises(ParameterRefused, match="model"):
        go(site, s, dry_run=True, model="gpt-4")  # a refusing dry run is still dry
    assert not sessions_dir.exists() or not any(sessions_dir.iterdir())


def test_windows_which_resolution(site):
    exe = str(site.tmp_path / "claude.exe")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run, platform="win32", which=lambda _: exe)
    go(site, s)
    assert run.calls[0][0][0] == exe  # the full .exe path is argv[0]
    run2 = FakeRun(stdout="session " + UUID1)
    s2 = make(site, run2, platform="win32", which=lambda _: exe)
    go(site, s2, name="beta", prompt="do beta")
    # a .cmd/.bat shim would let cmd.exe parse the prompt: refused
    s3 = make(site, FakeRun(), platform="win32", which=lambda _: str(site.tmp_path / "claude.cmd"))
    with pytest.raises(ParameterRefused, match="exe"):
        go(site, s3, name="gamma", prompt="do gamma")
    s4 = make(site, FakeRun(), platform="win32", which=lambda _: None)
    with pytest.raises(ParameterRefused, match="claude"):
        go(site, s4, name="delta", prompt="do delta")
    # non-win32 never consults which
    calls = []
    s5 = make(site, FakeRun(stdout="session " + UUID1), which=lambda n: calls.append(n) or "/x/claude")
    go(site, s5, name="eps", prompt="do eps")
    assert calls == []


def test_exit_failure_reason_carries_claude_output(site):
    run = FakeRun(stdout="Workspace not trusted. Run `claude` in /x once and accept the trust prompt",
                  launch_rc=1)
    s = make(site, run)
    res = go(site, s)
    assert res["ok"] is False and res["session_id"] is None
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["ok"] is False
    assert line["reason"].startswith("claude exited 1: ")  # rc first, then the output
    assert "Workspace not trusted" in line["reason"]  # why claude refused, in the audit


def test_launch_run_oserror_writes_null_id_record(site):
    class OsErrRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"] and self.broken:
                raise OSError("exec format error")
            return super().__call__(argv, **kw)

    run = OsErrRun(stdout="session " + UUID1)
    run.broken = True
    s = make(site, run)
    res = go(site, s)
    assert res["ok"] is False and res["name"] == "alpha" and res["session_id"] is None
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] is None and rec["stopped_ts"] is None  # list shows it
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert lines[-1]["action"] == "launch" and lines[-1]["ok"] is False
    assert [r for r in s.list()["sessions"] if r["name"] == "alpha"][0]["id"] is None
    run.broken = False
    run.stdout = "session " + UUID2
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    retry = s.relaunch("alpha", handoff=str(hf))  # relaunch replaces the orphan record
    assert retry["ok"] is True and retry["session_id"] == UUID2
    rec2 = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec2["session_id"] == UUID2 and rec2["stopped_ts"] is None


def test_launch_timeout_writes_null_id_record(site):
    class TimeoutRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"]:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
            return super().__call__(argv, **kw)

    s = make(site, TimeoutRun())
    with pytest.raises(subprocess.TimeoutExpired):
        go(site, s)
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] is None and rec["stopped_ts"] is None  # the process may exist
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert lines[-1]["action"] == "launch" and lines[-1]["ok"] is False


def test_successful_launch_unlinks_stale_ready_marker(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    marker = site.state / "sessions" / "alpha.ready.json"
    assert marker.is_file()
    s.stop(name="alpha")
    run.stdout = "session " + UUID2
    res = go(site, s)  # a fresh successful launch over the stopped record
    assert res["ok"] is True
    assert not marker.is_file()  # the old session's ready marker is stale and gone


def test_launch_record_write_failure_reports_started_id(site, monkeypatch):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    real = s._write_record

    def dead(name, rec):
        # Only the post-start write fails: the write-ahead "launching" record
        # must land before the process starts (D-3193) - this test's subject
        # is the write AFTER the start, which must report the started id.
        if rec.get("state") == "running":
            raise OSError("no space left on device")
        return real(name, rec)

    monkeypatch.setattr(s, "_write_record", dead)
    res = go(site, s)
    # adapted (r5 slice 4, seat): the record-write failure _ActionFailed now
    # carries session_id=sid, so the result reports the started id, not None.
    assert res["ok"] is False and res["session_id"] == UUID1
    assert len(run.launch_calls()) == 1  # Claude may be running: the id must survive
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "launch" and line["ok"] is False
    assert f"session started (id {UUID1}) but record write failed" in line["reason"]


def test_launch_marker_unlink_failure_reports_started_id(site, monkeypatch):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)

    class Boom:
        def unlink(self, missing_ok=False):
            raise OSError("unlink refused")

    monkeypatch.setattr(s, "_marker_path", lambda name: Boom())
    res = go(site, s)
    assert res["ok"] is False  # the session started: its id must reach the audit line
    assert len(run.launch_calls()) == 1
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "launch" and line["ok"] is False
    assert f"session started (id {UUID1}) but record write failed" in line["reason"]


def test_launch_parameter_refused_after_start_writes_orphan_record(site):
    exe = str(site.tmp_path / "claude.exe")
    seen = {"n": 0}

    def which(cmd):
        seen["n"] += 1
        return exe if seen["n"] == 1 else None  # resolution dies after argv is built

    run = FakeRun(stdout="backgrounded, id pending")  # no UUID: _resolve_id polls
    s = make(site, run, platform="win32", which=which)
    with pytest.raises(ParameterRefused, match="claude not found"):
        go(site, s)
    assert any(a[0] == exe and a[1:2] == ["--bg"] for a, _ in run.calls)  # it started
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] is None and rec["stopped_ts"] is None  # the orphan is recorded
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "launch" and line["ok"] is False


def test_record_state_launching_when_run_called(site):
    class ProbeRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"]:
                rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
                assert rec["state"] == "launching"  # written before the run
                assert rec["session_id"] is None
                assert rec["launched_ms"] == 1_000_000.0  # taken before the start
                assert rec["argv"][-1] == f"<{len(PROMPT)} chars>"  # redacted argv
            return super().__call__(argv, **kw)

    s = make(site, ProbeRun(stdout="session " + UUID1), now=lambda: 1000.0)
    go(site, s)
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "running" and rec["session_id"] == UUID1


def test_timeout_record_state_unknown(site):
    class TimeoutRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"]:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
            return super().__call__(argv, **kw)

    s = make(site, TimeoutRun())
    with pytest.raises(subprocess.TimeoutExpired):
        go(site, s)
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "unknown" and rec["stopped_ts"] is None  # the process may exist


def test_dry_run_writes_no_launch_record(site):
    run = FakeRun()
    s = make(site, run)
    go(site, s, dry_run=True)
    assert run.calls == []  # nothing ran
    assert not (site.state / "sessions" / "alpha.json").exists()  # no write-ahead record
    assert not (site.state / "sessions" / "audit.log").exists()


def test_legacy_record_without_state_is_read(site):
    sessions = site.state / "sessions"
    sessions.mkdir(parents=True)
    legacy = {"name": "alpha", "session_id": None, "stopped_ts": None}
    (sessions / "alpha.json").write_text(json.dumps(legacy))
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    listed = [r for r in s.list()["sessions"] if r["name"] == "alpha"][0]
    assert listed["state"] == "running"  # no state field, stopped_ts unset
    with pytest.raises(ParameterRefused, match="has a running record"):
        go(site, s)  # a legacy live record refuses a plain launch
    assert run.launch_calls() == []
    legacy["stopped_ts"] = 500.0
    (sessions / "alpha.json").write_text(json.dumps(legacy))
    listed = [r for r in s.list()["sessions"] if r["name"] == "alpha"][0]
    assert listed["state"] == "stopped"  # no state field, stopped_ts set
    res = go(site, s)  # a legacy stopped record is replaced by a plain launch
    assert res["ok"] is True
    rec = json.loads((sessions / "alpha.json").read_text())
    assert rec["state"] == "running"  # the new record carries the state field
