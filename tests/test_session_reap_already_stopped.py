"""tests/test_session_reap_already_stopped.py - skip sessions the reaper already stopped.

`claude stop` leaves state.json untouched (state stays "done", updatedAt unchanged), so without the
stop log the next run would stop the same sessions again, forever.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from plangraph.session_reap.__main__ import main as cli_main, reap
from plangraph.session_reap.log import log_stop, stopped_updated_at

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
UPDATED = NOW - timedelta(hours=3)


class _Runner:
    def __init__(self, rc=0):
        self.calls = []
        self.rc = rc

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return type("P", (), {"returncode": self.rc, "stdout": "stopped", "stderr": ""})()


def _job(jobs, short_id="j1", updated=UPDATED):
    d = jobs / short_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({
        "state": "done", "tempo": "idle", "name": "worker-task",
        "updatedAt": updated.isoformat(), "daemonShort": short_id,
        "output": {"result": "report"},
    }), encoding="utf-8")


def _lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _row(rows, short_id="j1"):
    return next(r for r in rows if r["short_id"] == short_id)


def _prior(log, updated_at, rc=0, short_id="j1"):
    log_stop(short_id=short_id, name="worker-task", age_min=180.0, state="done",
             reason="done_with_result", rc=rc, log_path=log, updated_at=updated_at)


def test_log_line_carries_updated_at(tmp_path):
    jobs, log = tmp_path / "jobs", tmp_path / "log.jsonl"
    _job(jobs)
    reap(jobs_dir=jobs, apply=True, runner=_Runner(), log_path=log, now=NOW)
    (entry,) = _lines(log)
    assert datetime.fromisoformat(entry["updated_at"]) == UPDATED
    assert datetime.fromisoformat(entry["updated_at"]).utcoffset() is not None


def test_repeated_apply_stops_once(tmp_path):
    jobs, log = tmp_path / "jobs", tmp_path / "log.jsonl"
    _job(jobs)
    runner = _Runner()
    first = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=log, now=NOW)
    assert (_row(first)["action"], _row(first)["reason"]) == ("stop", "done_with_result")
    assert len(runner.calls) == 1 and len(_lines(log)) == 1

    later = NOW + timedelta(minutes=5)
    second = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=log, now=later)
    assert (_row(second)["action"], _row(second)["reason"]) == ("keep", "already_stopped")
    assert "rc" not in _row(second)
    assert len(runner.calls) == 1
    assert len(_lines(log)) == 1


def test_resumed_and_finished_again_is_stoppable_again(tmp_path):
    jobs, log = tmp_path / "jobs", tmp_path / "log.jsonl"
    _job(jobs)
    runner = _Runner()
    reap(jobs_dir=jobs, apply=True, runner=runner, log_path=log, now=NOW)

    newer = NOW + timedelta(minutes=30)
    _job(jobs, updated=NOW + timedelta(minutes=10))
    rows = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=log, now=newer)
    assert (_row(rows)["action"], _row(rows)["reason"]) == ("stop", "done_with_result")
    assert len(runner.calls) == 2
    assert len(_lines(log)) == 2


def test_failed_stop_is_retried(tmp_path):
    jobs, log = tmp_path / "jobs", tmp_path / "log.jsonl"
    _job(jobs)
    _prior(log, UPDATED, rc=1)
    runner = _Runner()
    rows = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=log, now=NOW)
    assert _row(rows)["action"] == "stop"
    assert len(runner.calls) == 1


def test_failed_run_then_success_then_skip(tmp_path):
    jobs, log = tmp_path / "jobs", tmp_path / "log.jsonl"
    _job(jobs)
    assert reap(jobs_dir=jobs, apply=True, runner=_Runner(rc=1), log_path=log, now=NOW)
    ok = _Runner()
    reap(jobs_dir=jobs, apply=True, runner=ok, log_path=log, now=NOW)
    assert len(ok.calls) == 1
    rows = reap(jobs_dir=jobs, apply=True, runner=ok, log_path=log, now=NOW)
    assert _row(rows)["reason"] == "already_stopped"
    assert len(ok.calls) == 1


def test_malformed_log_lines_are_skipped(tmp_path):
    jobs, log = tmp_path / "jobs", tmp_path / "log.jsonl"
    _job(jobs)
    _prior(log, UPDATED)
    with open(log, "a", encoding="utf-8") as f:
        f.write("{not json\n[1, 2]\n\n" + json.dumps({"short_id": "j1", "rc": 0}) + "\n"
                + json.dumps({"short_id": "j1", "rc": 0, "updated_at": "garbage"}) + "\n"
                + json.dumps({"short_id": 7, "rc": 0, "updated_at": UPDATED.isoformat()}) + "\n")
    runner = _Runner()
    rows = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=log, now=NOW)
    assert _row(rows)["reason"] == "already_stopped"
    assert runner.calls == []


def test_only_malformed_log_is_not_fatal(tmp_path):
    jobs, log = tmp_path / "jobs", tmp_path / "log.jsonl"
    _job(jobs)
    log.write_bytes(b"\xff\xfe garbage\n{broken\n")
    runner = _Runner()
    rows = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=log, now=NOW)
    assert _row(rows)["action"] == "stop"
    assert len(runner.calls) == 1


def test_log_is_a_directory_is_not_fatal(tmp_path):
    jobs, log = tmp_path / "jobs", tmp_path / "logdir"
    _job(jobs)
    log.mkdir()
    rows = reap(jobs_dir=jobs, apply=False, runner=_Runner(), log_path=log, now=NOW)
    assert _row(rows)["action"] == "stop"


def test_old_line_without_updated_at_does_not_match(tmp_path):
    jobs, log = tmp_path / "jobs", tmp_path / "log.jsonl"
    _job(jobs)
    old = {"ts": "2026-11-01T10:32:00+00:00", "host": "h", "short_id": "j1", "name": "worker-task",
           "age_min": 180.0, "state": "done", "reason": "done_with_result", "action": "stop", "rc": 0}
    log.write_text(json.dumps(old) + "\n", encoding="utf-8")
    runner = _Runner()
    rows = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=log, now=NOW)
    assert _row(rows)["action"] == "stop"
    assert len(runner.calls) == 1


def test_log_for_another_id_does_not_match(tmp_path):
    jobs, log = tmp_path / "jobs", tmp_path / "log.jsonl"
    _job(jobs)
    _prior(log, UPDATED, short_id="other")
    rows = reap(jobs_dir=jobs, apply=False, now=NOW, log_path=log)
    assert _row(rows)["action"] == "stop"


def test_dry_run_shows_already_stopped(tmp_path, capsys):
    jobs, log = tmp_path / "jobs", tmp_path / "log.jsonl"
    _job(jobs)
    _prior(log, UPDATED)
    runner = _Runner()
    rows = reap(jobs_dir=jobs, apply=False, runner=runner, log_path=log, now=NOW)
    assert (_row(rows)["action"], _row(rows)["reason"]) == ("keep", "already_stopped")
    assert runner.calls == []

    assert cli_main(["--jobs-dir", str(jobs), "--json"], log_path=log, now=NOW) == 0
    (row,) = json.loads(capsys.readouterr().out)["sessions"]
    assert (row["action"], row["reason"]) == ("keep", "already_stopped")
    assert len(_lines(log)) == 1


def test_updated_at_offset_spelling_does_not_matter(tmp_path):
    jobs, log = tmp_path / "jobs", tmp_path / "log.jsonl"
    _job(jobs)
    _prior(log, UPDATED.astimezone(timezone(timedelta(hours=2))).isoformat())
    rows = reap(jobs_dir=jobs, apply=False, log_path=log, now=NOW)
    assert _row(rows)["reason"] == "already_stopped"


def test_non_string_naive_and_non_stop_lines_never_match(tmp_path):
    jobs, log = tmp_path / "jobs", tmp_path / "log.jsonl"
    _job(jobs)
    good = UPDATED.isoformat()
    lines = [{"action": "stop", "updated_at": v} for v in (123, [1], {"a": 1}, UPDATED.replace(tzinfo=None).isoformat())]
    lines += [{"action": "keep", "updated_at": good}, {"updated_at": good}]
    log.write_bytes("".join(json.dumps({"short_id": "j1", "rc": 0, **x}) + "\n" for x in lines).encode())
    assert stopped_updated_at(log) == {}
    runner = _Runner()
    rows = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=log, now=NOW)
    assert _row(rows)["action"] == "stop"
    assert len(runner.calls) == 1
