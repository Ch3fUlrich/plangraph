"""tests/test_session_reap.py - tests for plangraph:SESSION-AUTO-CLOSE (D-2442)."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
import pytest

from plangraph.session_reap.scan import scan, SessionRecord
from plangraph.session_reap.policy import decide
from plangraph.session_reap.mark import main as mark_main
from plangraph.session_reap.__main__ import reap


@pytest.fixture
def frozen_now():
    return datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_done_with_result_stops_after_grace(frozen_now):
    # Grace default is 2 minutes.
    # 1 minute ago -> keep
    rec_fresh = SessionRecord(
        short_id="job1",
        name="worker-task",
        state="done",
        tempo="idle",
        updated_at=frozen_now - timedelta(minutes=1),
        has_result=True,
    )
    action, reason = decide(rec_fresh, frozen_now)
    assert action == "keep"
    assert reason == "within_grace"

    # 2 minutes ago -> stop
    rec_grace = SessionRecord(
        short_id="job1",
        name="worker-task",
        state="done",
        tempo="idle",
        updated_at=frozen_now - timedelta(minutes=2),
        has_result=True,
    )
    action, reason = decide(rec_grace, frozen_now)
    assert action == "stop"
    assert reason == "done_with_result"


def test_done_without_result_idle_30_min_stops_and_29_min_keeps(frozen_now):
    # Idle timeout default is 30 minutes without result.
    # 29 minutes -> keep
    rec_29 = SessionRecord(
        short_id="job29",
        name="worker-task",
        state="done",
        tempo="idle",
        updated_at=frozen_now - timedelta(minutes=29),
        has_result=False,
    )
    action, reason = decide(rec_29, frozen_now)
    assert action == "keep"
    assert reason == "within_idle"

    # 30 minutes -> stop
    rec_30 = SessionRecord(
        short_id="job30",
        name="worker-task",
        state="done",
        tempo="idle",
        updated_at=frozen_now - timedelta(minutes=30),
        has_result=False,
    )
    action, reason = decide(rec_30, frozen_now)
    assert action == "stop"
    assert reason == "done_idle"


def test_working_idle_blocked_stopped_all_keep(frozen_now):
    # working with tempo idle (e.g. waiting on own background task) -> keep
    rec_working_idle = SessionRecord(
        short_id="w1",
        name="worker-task",
        state="working",
        tempo="idle",
        updated_at=frozen_now - timedelta(hours=5),
        has_result=False,
    )
    assert decide(rec_working_idle, frozen_now) == ("keep", "state_working")

    # blocked (waiting for human) -> keep
    rec_blocked = SessionRecord(
        short_id="b1",
        name="worker-task",
        state="blocked",
        tempo="blocked",
        updated_at=frozen_now - timedelta(hours=5),
        has_result=False,
    )
    assert decide(rec_blocked, frozen_now) == ("keep", "state_blocked")

    # stopped -> keep
    rec_stopped = SessionRecord(
        short_id="s1",
        name="worker-task",
        state="stopped",
        tempo="idle",
        updated_at=frozen_now - timedelta(hours=5),
        has_result=False,
    )
    assert decide(rec_stopped, frozen_now) == ("keep", "state_stopped")


def test_allowlisted_sessions_never_stopped(frozen_now):
    # Names matching *-L1-*, routing*, *judge*, *planner*, plangraph-oversight*
    allowlisted_names = [
        "task-L1-run",
        "routing-dispatcher",
        "code-judge-agent",
        "auto-planner-v2",
        "plangraph-oversight-seat",
    ]
    for name in allowlisted_names:
        rec = SessionRecord(
            short_id="allow1",
            name=name,
            state="done",
            tempo="idle",
            updated_at=frozen_now - timedelta(days=5),
            has_result=True,
        )
        action, reason = decide(rec, frozen_now)
        assert action == "keep", f"failed for {name}"
        assert reason == "allowlisted"


def test_ttl_json_allow_stop_false_keeps(frozen_now):
    # ttl.json with allow_stop: false -> always keep
    rec = SessionRecord(
        short_id="ttl_keep",
        name="regular-session",
        state="done",
        tempo="idle",
        updated_at=frozen_now - timedelta(hours=10),
        has_result=False,
        ttl={"ttl_min": 15, "allow_stop": False},
        allow_stop=False,
        ttl_min=15,
    )
    assert decide(rec, frozen_now) == ("keep", "ttl_no_stop")


def test_ttl_min_overrides_idle_min(frozen_now):
    # ttl_min = 10 overrides default 30 min idle threshold for sessions without result
    rec_9 = SessionRecord(
        short_id="ttl_override",
        name="regular-session",
        state="done",
        tempo="idle",
        updated_at=frozen_now - timedelta(minutes=9),
        has_result=False,
        ttl={"ttl_min": 10, "allow_stop": True},
        allow_stop=True,
        ttl_min=10,
    )
    action, reason = decide(rec_9, frozen_now)
    assert action == "keep"

    rec_10 = SessionRecord(
        short_id="ttl_override",
        name="regular-session",
        state="done",
        tempo="idle",
        updated_at=frozen_now - timedelta(minutes=10),
        has_result=False,
        ttl={"ttl_min": 10, "allow_stop": True},
        allow_stop=True,
        ttl_min=10,
    )
    action, reason = decide(rec_10, frozen_now)
    assert action == "stop"


def test_scan_skips_and_counts_unreadable_state_json(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    # Valid job 1
    job1 = jobs_dir / "job001"
    job1.mkdir()
    (job1 / "state.json").write_text(json.dumps({
        "state": "done",
        "tempo": "idle",
        "name": "job-one",
        "updatedAt": "2026-11-01T11:00:00Z",
        "daemonShort": "job001",
        "output": {"result": "success"},
    }), encoding="utf-8")

    # Corrupt / partial JSON job 2
    job2 = jobs_dir / "job002"
    job2.mkdir()
    (job2 / "state.json").write_text("{corrupt json ...", encoding="utf-8")

    # Empty file job 3
    job3 = jobs_dir / "job003"
    job3.mkdir()
    (job3 / "state.json").write_text("", encoding="utf-8")

    # Valid job 4 with ttl.json next to state.json
    job4 = jobs_dir / "job004"
    job4.mkdir()
    (job4 / "state.json").write_text(json.dumps({
        "state": "working",
        "tempo": "active",
        "name": "job-four",
        "updatedAt": "2026-11-01T11:30:00Z",
        "daemonShort": "job004",
        "output": None,
    }), encoding="utf-8")
    (job4 / "ttl.json").write_text(json.dumps({
        "ttl_min": 45,
        "allow_stop": False,
    }), encoding="utf-8")

    res = scan(jobs_dir)
    assert len(res.records) == 2
    assert res.unreadable == 2

    # Verify job4 loaded ttl.json
    j4 = [r for r in res.records if r.short_id == "job004"][0]
    assert j4.allow_stop is False
    assert j4.ttl_min == 45


def test_dry_run_never_calls_runner(tmp_path, frozen_now):
    calls = []

    def fake_runner(argv, **kw):
        calls.append(argv)
        return type("Proc", (), {"returncode": 0, "stdout": "stopped", "stderr": ""})()

    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    job1 = jobs_dir / "job001"
    job1.mkdir()
    (job1 / "state.json").write_text(json.dumps({
        "state": "done",
        "tempo": "idle",
        "name": "task-one",
        "updatedAt": (frozen_now - timedelta(hours=1)).isoformat(),
        "daemonShort": "job001",
        "output": {"result": "done"},
    }), encoding="utf-8")

    # Run without apply (dry run)
    reap(jobs_dir=jobs_dir, apply=False, runner=fake_runner, now=frozen_now)
    assert len(calls) == 0


def test_apply_calls_claude_stop_exactly_once_and_logs(tmp_path, frozen_now):
    calls = []

    def fake_runner(argv, **kw):
        calls.append(argv)
        return type("Proc", (), {"returncode": 0, "stdout": f"stopped {argv[2]}", "stderr": ""})()

    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    # Job that should be stopped (done with result > grace)
    j1 = jobs_dir / "j1"
    j1.mkdir()
    (j1 / "state.json").write_text(json.dumps({
        "state": "done",
        "tempo": "idle",
        "name": "done-task",
        "updatedAt": (frozen_now - timedelta(minutes=10)).isoformat(),
        "daemonShort": "j1",
        "output": {"result": "all done"},
    }), encoding="utf-8")

    # Job that should NOT be stopped (working)
    j2 = jobs_dir / "j2"
    j2.mkdir()
    (j2 / "state.json").write_text(json.dumps({
        "state": "working",
        "tempo": "active",
        "name": "working-task",
        "updatedAt": (frozen_now - timedelta(minutes=10)).isoformat(),
        "daemonShort": "j2",
        "output": None,
    }), encoding="utf-8")

    log_file = tmp_path / "session-reap.jsonl"

    reap(jobs_dir=jobs_dir, apply=True, runner=fake_runner, log_path=log_file, now=frozen_now)

    # Fake runner must be called exactly once: claude stop j1
    assert len(calls) == 1
    assert calls[0] == ["claude", "stop", "j1"]

    # Verify log line
    assert log_file.is_file()
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    entry = lines[0]

    # Required fields and nothing else
    expected_fields = {"ts", "host", "short_id", "name", "age_min", "state", "reason", "action", "rc", "updated_at"}
    assert set(entry.keys()) == expected_fields
    assert entry["short_id"] == "j1"
    assert entry["name"] == "done-task"
    assert entry["state"] == "done"
    assert entry["action"] == "stop"
    assert entry["rc"] == 0
    assert entry["age_min"] >= 10.0


def test_runner_failure_logged_with_rc_no_crash(tmp_path, frozen_now):
    def failing_runner(argv, **kw):
        return type("Proc", (), {"returncode": 127, "stdout": "", "stderr": "claude not found"})()

    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    j1 = jobs_dir / "fail1"
    j1.mkdir()
    (j1 / "state.json").write_text(json.dumps({
        "state": "done",
        "tempo": "idle",
        "name": "failing-stop-task",
        "updatedAt": (frozen_now - timedelta(minutes=10)).isoformat(),
        "daemonShort": "fail1",
        "output": {"result": "report"},
    }), encoding="utf-8")

    log_file = tmp_path / "session-reap.jsonl"

    # Should not crash
    reap(jobs_dir=jobs_dir, apply=True, runner=failing_runner, log_path=log_file, now=frozen_now)

    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["short_id"] == "fail1"
    assert lines[0]["rc"] == 127


def test_mark_session_writes_ttl_json(tmp_path, monkeypatch):
    job_dir = tmp_path / "test_job_123"
    job_dir.mkdir()
    (job_dir / "state.json").write_text("{}", encoding="utf-8")

    # CLAUDE_JOB_DIR set to a child of the job dir (the job dir holds state.json)
    monkeypatch.setenv("CLAUDE_JOB_DIR", str(job_dir / "task_scratch"))

    # Test marking with ttl_min 20 and default allow_stop True
    mark_main(["--ttl-min", "20"])
    ttl_path = job_dir / "ttl.json"
    assert ttl_path.is_file()
    data = json.loads(ttl_path.read_text(encoding="utf-8"))
    assert data == {"ttl_min": 20, "allow_stop": True}

    # Test marking with --no-stop
    mark_main(["--ttl-min", "40", "--no-stop"])
    data2 = json.loads(ttl_path.read_text(encoding="utf-8"))
    assert data2 == {"ttl_min": 40, "allow_stop": False}


def test_mark_refuses_when_claude_job_dir_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)
    with pytest.raises(SystemExit) as exc:
        mark_main(["--ttl-min", "30"])
    assert exc.value.code != 0
