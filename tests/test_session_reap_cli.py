"""tests/test_session_reap_cli.py - CLI and integration tests for session reaper."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

from plangraph.session_reap.__main__ import main as cli_main
from plangraph.session_reap.act import stop_session
from plangraph.session_reap.log import log_stop
from plangraph.session_reap.mark import mark_session, main as mark_main
from plangraph.session_reap.policy import ReapConfig, decide
from plangraph.session_reap.scan import SessionRecord


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
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["short_id"] == "job_cli_1"
    assert data[0]["action"] == "keep"


def test_cli_custom_config(tmp_path, capsys):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    job1 = jobs_dir / "custom1"
    job1.mkdir()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    (job1 / "state.json").write_text(json.dumps({
        "state": "done",
        "tempo": "idle",
        "name": "special-custom-prefix-job",
        "updatedAt": (now - timedelta(minutes=5)).isoformat(),
        "daemonShort": "custom1",
        "output": {"result": "report"},
    }), encoding="utf-8")

    # Config with custom allowlist and grace
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "done_grace_min": 1,
        "idle_min": 10,
        "allowlist": ["special-custom-*"],
    }), encoding="utf-8")

    rc = cli_main(["--jobs-dir", str(jobs_dir), "--config", str(cfg_file), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["action"] == "keep"
    assert data[0]["reason"] == "allowlisted"


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
