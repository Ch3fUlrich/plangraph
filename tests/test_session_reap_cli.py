"""tests/test_session_reap_cli.py - CLI and integration tests for session reaper."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

from plangraph.session_reap.__main__ import main as cli_main
from plangraph.session_reap.act import stop_session
from plangraph.session_reap.log import log_stop
from plangraph.session_reap.mark import mark_session


def test_cli_json_and_table_output(tmp_path, monkeypatch, capsys):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    job1 = jobs_dir / "job_cli_1"
    job1.mkdir()
    (job1 / "state.json").write_text(json.dumps({
        "state": "working",
        "tempo": "active",
        "name": "cli-test-session",
        "updatedAt": "2026-11-01T10:00:00Z",
        "daemonShort": "job_cli_1",
        "output": None,
    }), encoding="utf-8")

    # Test table output (default)
    rc_table = cli_main(["--jobs-dir", str(jobs_dir)])
    assert rc_table == 0
    captured_table = capsys.readouterr().out
    assert "SHORT_ID" in captured_table
    assert "job_cli_1" in captured_table
    assert "cli-test-session" in captured_table

    # Test json output
    rc_json = cli_main(["--jobs-dir", str(jobs_dir), "--json"])
    assert rc_json == 0
    captured_json = capsys.readouterr().out
    data = json.loads(captured_json)
    assert data["unreadable"] == 0
    sessions = data["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["short_id"] == "job_cli_1"
    assert (sessions[0]["action"], sessions[0]["reason"]) == ("keep", "state_working")


def _write_job(jobs_dir, short_id, name, updated, *, result, state="done"):
    job = jobs_dir / short_id
    job.mkdir(parents=True)
    (job / "state.json").write_text(json.dumps({
        "state": state,
        "tempo": "idle",
        "name": name,
        "updatedAt": updated.isoformat(),
        "daemonShort": short_id,
        "output": {"result": "report"} if result else None,
    }), encoding="utf-8")


def test_cli_custom_config(tmp_path, capsys):
    jobs_dir = tmp_path / "jobs"
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    five_ago = now - timedelta(minutes=5)
    _write_job(jobs_dir, "extra", "special-custom-prefix-job", five_ago, result=True)
    _write_job(jobs_dir, "dflt", "task-L1-run", five_ago, result=True)
    _write_job(jobs_dir, "res", "plain-with-result", five_ago, result=True)
    _write_job(jobs_dir, "idle", "plain-no-result", five_ago, result=False)

    def run(cfg_body):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(cfg_body), encoding="utf-8")
        rc = cli_main(["--jobs-dir", str(jobs_dir), "--config", str(cfg_file), "--json"], now=now)
        assert rc == 0
        rows = json.loads(capsys.readouterr().out)["sessions"]
        return {r["short_id"]: (r["action"], r["reason"]) for r in rows}

    # Short grace/idle: both timed sessions are old enough. The custom allowlist adds a
    # pattern and the built-in *-L1-* protection is still in force.
    got = run({"done_grace_min": 1, "idle_min": 4, "allowlist": ["special-custom-*"]})
    assert got == {
        "extra": ("keep", "allowlisted"),
        "dflt": ("keep", "allowlisted"),
        "res": ("stop", "done_with_result"),
        "idle": ("stop", "done_idle"),
    }

    # Long grace/idle: the same sessions are now inside their windows.
    got = run({"done_grace_min": 10, "idle_min": 10})
    assert got["res"] == ("keep", "within_grace")
    assert got["idle"] == ("keep", "within_idle")
    assert got["extra"] == ("keep", "within_grace")  # no longer allowlisted: judged by time
    assert got["dflt"] == ("keep", "allowlisted")

def test_stop_session_runner_invocations(monkeypatch):
    called = []

    def mock_runner(argv, **kw):
        called.append((argv, kw))
        assert "shell" not in kw or kw["shell"] is False
        assert kw.get("timeout") == 30.0
        return type("Res", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    # Dry run must not call runner
    rc, msg = stop_session("job_dry", apply=False, runner=mock_runner)
    assert rc == 0
    assert len(called) == 0

    # Apply must call runner with ['claude', 'stop', 'job_live']
    rc, msg = stop_session("job_live", apply=True, runner=mock_runner)
    assert rc == 0
    assert len(called) == 1
    assert called[0][0] == ["claude", "stop", "job_live"]


def test_mark_session_with_explicit_dir(tmp_path):
    job_dir = tmp_path / "explicit_dir"
    job_dir.mkdir()

    ttl_file = mark_session(ttl_min=60, allow_stop=False, job_dir=job_dir)
    assert ttl_file == job_dir / "ttl.json"
    assert json.loads(ttl_file.read_text(encoding="utf-8")) == {
        "ttl_min": 60,
        "allow_stop": False,
    }


def test_log_stop_writes_to_nested_path(tmp_path):
    log_dir = tmp_path / "nested" / "logs"
    log_file = log_dir / "reap.jsonl"

    entry = log_stop(
        short_id="s123",
        name="test-run",
        age_min=15.234,
        state="done",
        reason="done_idle",
        action="stop",
        rc=0,
        log_path=log_file,
    )
    assert log_file.is_file()
    assert entry["age_min"] == 15.2
    assert entry["short_id"] == "s123"
