"""tests/test_session_reap_safety.py - fail-closed safety properties of the session reaper (D-2442).

Every case here is a way the reaper could stop a session it should have kept.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from plangraph.session_reap import __main__ as reap_cli
from plangraph.session_reap.__main__ import ConfigError, load_config, main as cli_main, reap
from plangraph.session_reap.act import stop_session
from plangraph.session_reap.policy import DEFAULT_ALLOWLIST, ReapConfig, decide
from plangraph.session_reap.scan import SessionRecord, read_session, scan

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _state(updated=None, **over):
    data = {
        "state": "done",
        "tempo": "idle",
        "name": "worker-task",
        "updatedAt": (updated or NOW - timedelta(hours=3)).isoformat(),
        "output": {"result": "report"},
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not ...}


def _job(jobs, short_id, **over):
    d = jobs / short_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(_state(daemonShort=short_id, **over)), encoding="utf-8")
    return d


def _rec(**over):
    base = dict(
        short_id="r1", name="worker-task", state="done", tempo="idle",
        updated_at=NOW - timedelta(hours=3), has_result=True,
    )
    base.update(over)
    return SessionRecord(**base)


class _Runner:
    def __init__(self, rc=0):
        self.calls = []
        self.rc = rc

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return type("P", (), {"returncode": self.rc, "stdout": "", "stderr": ""})()


def _lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---- allowlist: case-insensitive on every platform, defaults always apply ----

@pytest.mark.parametrize("name", [
    "work-l1-x", "Task-L1-Run", "ROUTING-dispatch", "Routing", "Judge-A", "my-JUDGE",
    "Auto-PLANNER", "PlanGraph-Oversight-1", "PLANGRAPH-OVERSIGHT",
])
def test_allowlist_matches_case_insensitively(name):
    action, reason = decide(_rec(name=name), NOW)
    assert (action, reason) == ("keep", "allowlisted")


def test_config_allowlist_extends_defaults_never_replaces_them():
    cfg = ReapConfig(allowlist=["Custom-*"])
    assert decide(_rec(name="custom-thing"), NOW, cfg) == ("keep", "allowlisted")
    for pattern_name in ("x-L1-y", "routing-a", "a-judge-b", "a-planner-b", "plangraph-oversight-z"):
        assert decide(_rec(name=pattern_name), NOW, cfg) == ("keep", "allowlisted")
    assert decide(_rec(name="custom-thing"), NOW, {"allowlist": ["zzz*"]})[0] == "stop"
    assert decide(_rec(name="x-L1-y"), NOW, {"allowlist": ["zzz*"]}) == ("keep", "allowlisted")
    assert DEFAULT_ALLOWLIST  # the defaults themselves stay non-empty


@pytest.mark.parametrize("name", ["", "   ", None])
def test_empty_name_is_unknown_and_kept(name):
    assert decide(_rec(name=name), NOW) == ("keep", "unknown_name")


def test_scan_leaves_garbled_name_empty_instead_of_using_dir_name(tmp_path):
    jobs = tmp_path / "jobs"
    for sid, over in (("a1", {"name": ...}), ("a2", {"name": ""}), ("a3", {"name": 42}), ("a4", {"name": None})):
        _job(jobs, sid, **over)
    res = scan(jobs)
    assert [r.name for r in res.records] == ["", "", "", ""]
    assert all(decide(r, NOW)[1] == "unknown_name" for r in res.records)


# ---- ttl.json: fail closed ----

def test_corrupt_ttl_json_keeps_the_session(tmp_path):
    jobs = tmp_path / "jobs"
    job = _job(jobs, "t1")
    (job / "ttl.json").write_text("{not json", encoding="utf-8")
    rec = scan(jobs).records[0]
    assert rec.allow_stop is False
    assert rec.ttl_error
    assert decide(rec, NOW) == ("keep", "ttl_invalid")


def test_ttl_json_that_is_not_a_file_keeps_the_session(tmp_path):
    jobs = tmp_path / "jobs"
    job = _job(jobs, "t2")
    (job / "ttl.json").mkdir()
    rec = scan(jobs).records[0]
    assert decide(rec, NOW) == ("keep", "ttl_invalid")


@pytest.mark.parametrize("body", [
    {"allow_stop": "false"}, {"allow_stop": "no"}, {"allow_stop": "0"}, {"allow_stop": None},
    {"allow_stop": 2}, {"allow_stop": []}, {"allow_stop": 1.0},
    {"ttl_min": "10"}, {"ttl_min": None}, {"ttl_min": True}, {"ttl_min": [5]},
    {"ttl_min": float("nan")}, {"ttl_min": float("inf")},
])
def test_garbled_ttl_values_keep_the_session(tmp_path, body):
    jobs = tmp_path / "jobs"
    job = _job(jobs, "g1")
    (job / "ttl.json").write_text(json.dumps(body), encoding="utf-8")
    rec = scan(jobs).records[0]
    assert decide(rec, NOW) == ("keep", "ttl_invalid")


def test_ttl_json_top_level_not_object_keeps(tmp_path):
    jobs = tmp_path / "jobs"
    job = _job(jobs, "g2")
    (job / "ttl.json").write_text("[1, 2]", encoding="utf-8")
    assert decide(scan(jobs).records[0], NOW) == ("keep", "ttl_invalid")


@pytest.mark.parametrize("val,expected", [(True, True), (False, False), (1, True), (0, False)])
def test_allow_stop_accepts_literal_bool_and_zero_one(tmp_path, val, expected):
    jobs = tmp_path / "jobs"
    job = _job(jobs, "ok1")
    (job / "ttl.json").write_text(json.dumps({"allow_stop": val}), encoding="utf-8")
    rec = scan(jobs).records[0]
    assert rec.allow_stop is expected
    assert rec.ttl_error is None


def test_ttl_dict_on_a_plain_record_is_parsed_strictly():
    assert decide({"name": "w", "state": "done", "updated_at": NOW - timedelta(days=1),
                   "has_result": True, "ttl": {"allow_stop": "false"}}, NOW) == ("keep", "ttl_invalid")


# ---- ttl_min bounds and scope ----

@pytest.mark.parametrize("ttl", [0, -5, -0.5])
def test_non_positive_ttl_min_is_invalid_not_instant_reap(ttl):
    rec = _rec(has_result=False, updated_at=NOW - timedelta(seconds=30), ttl_min=ttl)
    assert decide(rec, NOW) == ("keep", "ttl_invalid")
    old = _rec(has_result=False, updated_at=NOW - timedelta(minutes=2), ttl_min=ttl)
    assert decide(old, NOW) == ("keep", "ttl_invalid")


def test_ttl_min_applies_to_sessions_with_a_result_too():
    rec = _rec(has_result=True, updated_at=NOW - timedelta(minutes=10), ttl_min=45)
    assert decide(rec, NOW) == ("keep", "within_ttl")
    rec = _rec(has_result=True, updated_at=NOW - timedelta(minutes=45), ttl_min=45)
    assert decide(rec, NOW) == ("stop", "done_with_result")


# ---- timestamps ----

def test_missing_updated_at_is_not_replaced_by_created_at(tmp_path):
    jobs = tmp_path / "jobs"
    _job(jobs, "m1", updatedAt=..., createdAt=(NOW - timedelta(hours=9)).isoformat())
    rec = scan(jobs).records[0]
    assert rec.updated_at is None
    assert decide(rec, NOW) == ("keep", "missing_timestamp")


@pytest.mark.parametrize("raw", ["2026-11-01T08:00:00", "not a date", 12345, ""])
def test_naive_or_unparseable_updated_at_is_kept(tmp_path, raw):
    jobs = tmp_path / "jobs"
    _job(jobs, "n1", updatedAt=raw)
    res = scan(jobs)
    assert res.unreadable == 0
    assert decide(res.records[0], NOW) == ("keep", "missing_timestamp")


def test_naive_datetime_on_a_record_is_kept():
    rec = _rec(updated_at=datetime(2026, 11, 1, 8, 0, 0))
    assert decide(rec, NOW) == ("keep", "missing_timestamp")


def test_record_has_no_partial_iteration():
    with pytest.raises(TypeError):
        tuple(_rec())
    with pytest.raises(TypeError):
        len(_rec())


def test_read_session_returns_none_for_corrupt_state(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "state.json").write_text("{", encoding="utf-8")
    assert read_session(d) is None
    assert read_session(tmp_path / "nowhere") is None


# ---- re-read before each stop ----

def _scan_then(monkeypatch, mutate):
    real = reap_cli.scan

    def wrapped(jobs_dir):
        res = real(jobs_dir)
        mutate()
        return res

    monkeypatch.setattr(reap_cli, "scan", wrapped)


def test_session_resumed_after_scan_is_not_stopped(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    job = _job(jobs, "res1")
    runner, log = _Runner(), tmp_path / "log.jsonl"
    _scan_then(monkeypatch, lambda: (job / "state.json").write_text(
        json.dumps(_state(daemonShort="res1", state="working", tempo="active")), encoding="utf-8"))
    rows = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=log, now=NOW)
    assert runner.calls == []
    assert not log.exists()
    assert (rows[0]["action"], rows[0]["reason"]) == ("keep", "changed_since_scan")


def test_session_touched_but_still_done_is_not_stopped(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    job = _job(jobs, "res2")
    runner = _Runner()
    _scan_then(monkeypatch, lambda: (job / "state.json").write_text(
        json.dumps(_state(daemonShort="res2", updated=NOW - timedelta(seconds=5))), encoding="utf-8"))
    rows = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=tmp_path / "l", now=NOW)
    assert runner.calls == []
    assert rows[0]["reason"] == "changed_since_scan"


def test_marker_added_after_scan_is_honoured(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    job = _job(jobs, "res3")
    runner = _Runner()
    _scan_then(monkeypatch, lambda: (job / "ttl.json").write_text(
        json.dumps({"allow_stop": False}), encoding="utf-8"))
    rows = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=tmp_path / "l", now=NOW)
    assert runner.calls == []
    assert rows[0]["action"] == "keep"


def test_state_removed_after_scan_is_not_stopped(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    job = _job(jobs, "res4")
    runner = _Runner()
    _scan_then(monkeypatch, lambda: (job / "state.json").unlink())
    rows = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=tmp_path / "l", now=NOW)
    assert runner.calls == []
    assert rows[0]["reason"] == "changed_since_scan"


def test_unchanged_session_is_still_stopped(tmp_path):
    jobs = tmp_path / "jobs"
    _job(jobs, "go1")
    runner = _Runner()
    rows = reap(jobs_dir=jobs, apply=True, runner=runner, log_path=tmp_path / "l", now=NOW)
    assert runner.calls == [["claude", "stop", "go1"]]
    assert rows[0]["action"] == "stop"


# ---- act / audit ----

def test_stop_session_reports_timeout_distinctly():
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)

    rc, msg = stop_session("x", apply=True, runner=boom)
    assert rc == 1
    assert msg == "timeout"


def test_timeout_is_visible_in_the_audit_line_without_crash(tmp_path):
    jobs = tmp_path / "jobs"
    _job(jobs, "to1")
    log = tmp_path / "log.jsonl"

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)

    reap(jobs_dir=jobs, apply=True, runner=boom, log_path=log, now=NOW)
    (entry,) = _lines(log)
    assert entry["rc"] == 1
    assert "timeout" in entry["reason"]
    assert set(entry) == {"ts", "host", "short_id", "name", "age_min", "state", "reason", "action", "rc", "updated_at"}


def test_nonzero_rc_and_runner_error_are_tagged(tmp_path):
    jobs = tmp_path / "jobs"
    _job(jobs, "rc1")
    log = tmp_path / "log.jsonl"
    reap(jobs_dir=jobs, apply=True, runner=_Runner(rc=127), log_path=log, now=NOW)
    reap(jobs_dir=jobs, apply=True, runner=lambda a, **k: (_ for _ in ()).throw(OSError("x")),
         log_path=log, now=NOW)
    first, second = _lines(log)
    assert first["rc"] == 127 and "stop_failed" in first["reason"]
    assert second["rc"] == 1 and "runner_error" in second["reason"]


# ---- config: validated, loud, merged ----

@pytest.mark.parametrize("body", [
    "{broken", "[]", '{"idle_min": null}', '{"idle_min": "abc"}', '{"idle_min": -1}',
    '{"idle_min": 0}', '{"done_grace_min": true}', '{"done_grace_min": "2"}',
    '{"allowlist": "x*"}', '{"allowlist": [1]}', '{"allowlist": [""]}', '{"idle_minutes": 5}',
])
def test_bad_config_aborts_the_run(tmp_path, capsys, body):
    jobs = tmp_path / "jobs"
    _job(jobs, "cfg1")
    cfg = tmp_path / "c.json"
    cfg.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(cfg)
    runner = _Runner()
    rc = cli_main(["--jobs-dir", str(jobs), "--config", str(cfg), "--apply"], runner=runner, now=NOW,
                  log_path=tmp_path / "l")
    assert rc == 2
    assert runner.calls == []
    assert "config" in capsys.readouterr().err.lower()


def test_explicit_missing_config_aborts(tmp_path, capsys):
    rc = cli_main(["--jobs-dir", str(tmp_path), "--config", str(tmp_path / "absent.json")], now=NOW)
    assert rc == 2
    assert "config" in capsys.readouterr().err.lower()


def test_valid_config_loads(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"done_grace_min": 5, "idle_min": 12.5, "allowlist": ["a*"]}), encoding="utf-8")
    c = load_config(cfg)
    assert (c.done_grace_min, c.idle_min, list(c.allowlist)) == (5, 12.5, ["a*"])
    assert load_config(None) == ReapConfig()


# ---- report shows skipped sessions ----

def test_json_report_includes_unreadable_count(tmp_path, capsys):
    jobs = tmp_path / "jobs"
    _job(jobs, "ok1")
    bad = jobs / "bad1"
    bad.mkdir()
    (bad / "state.json").write_text("{", encoding="utf-8")
    assert cli_main(["--jobs-dir", str(jobs), "--json"], now=NOW) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["unreadable"] == 1
    assert [r["short_id"] for r in data["sessions"]] == ["ok1"]


def test_table_report_mentions_unreadable_count(tmp_path, capsys):
    jobs = tmp_path / "jobs"
    bad = jobs / "bad1"
    bad.mkdir(parents=True)
    (bad / "state.json").write_text("", encoding="utf-8")
    assert cli_main(["--jobs-dir", str(jobs)], now=NOW) == 0
    assert "1 unreadable" in capsys.readouterr().out
