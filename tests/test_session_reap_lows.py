"""tests/test_session_reap_lows.py - fail-closed fixes for the SESSION-AUTO-CLOSE seat LOWs (D-3485)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plangraph.session_reap.act import failure_tag, stop_session
from plangraph.session_reap.mark import main as mark_main, mark_session
from plangraph.session_reap.policy import decide
from plangraph.session_reap.scan import parse_ttl_marker

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _dict_rec(**over):
    base = {
        "name": "worker-task", "state": "done", "updated_at": NOW - timedelta(hours=3), "has_result": True,
    }
    base.update(over)
    return base


# ---- act: an unverifiable stop is a failure, never a clean success ----

class _NoRc:
    stdout = "stopped"
    stderr = ""


@pytest.mark.parametrize("result", [None, _NoRc(), "ok", [], 0, 0.0])
def test_runner_result_without_returncode_is_a_runner_error(result):
    rc, msg = stop_session("x", apply=True, runner=lambda argv, **kw: result)
    assert rc != 0
    assert failure_tag(rc, msg) == "runner_error"


@pytest.mark.parametrize("rc_value", [None, "0", "abc", True, float("nan"), 0.5, [0]])
def test_non_numeric_returncode_is_a_runner_error(rc_value):
    proc = type("P", (), {"returncode": rc_value, "stdout": "", "stderr": ""})()
    rc, msg = stop_session("x", apply=True, runner=lambda argv, **kw: proc)
    assert rc != 0
    assert failure_tag(rc, msg) == "runner_error"


def test_integer_returncode_is_still_honoured():
    for value, expected in ((0, 0), (127, 127)):
        proc = type("P", (), {"returncode": value, "stdout": "", "stderr": ""})()
        assert stop_session("x", apply=True, runner=lambda argv, **kw: proc)[0] == expected


# ---- mark: an explicit --dir must be a session job dir too ----

def test_mark_session_refuses_explicit_dir_without_state_json(tmp_path):
    outside = tmp_path / "elsewhere"
    with pytest.raises(ValueError, match="state.json"):
        mark_session(ttl_min=5, job_dir=outside)
    assert not outside.exists()
    outside.mkdir()
    with pytest.raises(ValueError, match="state.json"):
        mark_session(ttl_min=5, job_dir=outside)
    assert not (outside / "ttl.json").exists()


def test_mark_cli_refuses_explicit_dir_without_state_json(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        mark_main(["--ttl-min", "5", "--dir", str(tmp_path)])
    assert exc.value.code != 0
    assert "state.json" in capsys.readouterr().err
    assert not (tmp_path / "ttl.json").exists()


def test_mark_accepts_explicit_dir_with_state_json(tmp_path):
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    assert mark_session(ttl_min=7, job_dir=tmp_path) == tmp_path / "ttl.json"


# ---- policy: allow_stop is strict for untrusted dict records ----

@pytest.mark.parametrize("value", [0, "false", 0.0, "no", "", [], 2, "true", 1, 1.0])
def test_non_bool_allow_stop_keeps(value):
    assert decide(_dict_rec(allow_stop=value), NOW)[0] == "keep"


def test_false_allow_stop_keeps_as_no_stop():
    assert decide(_dict_rec(allow_stop=False), NOW) == ("keep", "ttl_no_stop")


@pytest.mark.parametrize("value", [True, None])
def test_true_or_missing_allow_stop_can_stop(value):
    assert decide(_dict_rec(allow_stop=value), NOW) == ("stop", "done_with_result")


# ---- policy: non-numeric thresholds keep instead of crashing ----

@pytest.mark.parametrize("bad", ["abc", "5", None, True, [1], {}, float("nan"), float("inf")])
@pytest.mark.parametrize("key,has_result", [("done_grace_min", True), ("idle_min", False)])
def test_non_numeric_config_threshold_keeps(key, has_result, bad):
    rec = _dict_rec(has_result=has_result)
    action, reason = decide(rec, NOW, {key: bad})
    assert action == "keep"
    assert reason == "config_invalid"


@pytest.mark.parametrize("bad", ["abc", "5", True, [1], float("nan"), float("inf")])
def test_non_numeric_record_ttl_min_keeps(bad):
    assert decide(_dict_rec(ttl_min=bad), NOW) == ("keep", "ttl_invalid")


def test_config_threshold_of_zero_is_still_floored():
    fresh = _dict_rec(updated_at=NOW - timedelta(seconds=30))
    assert decide(fresh, NOW, {"done_grace_min": 0}) == ("keep", "within_grace")
    old = _dict_rec(updated_at=NOW - timedelta(minutes=2))
    assert decide(old, NOW, {"done_grace_min": 0}) == ("stop", "done_with_result")


# ---- marker ttl_min must be positive; garbled marker keeps ----

@pytest.mark.parametrize("ttl", [0, -1, -0.5, 0.0])
def test_non_positive_marker_ttl_min_is_invalid(ttl):
    allow_stop, ttl_min, error = parse_ttl_marker({"ttl_min": ttl})
    assert error and ttl_min is None and allow_stop is False


@pytest.mark.parametrize("ttl", [0, -5, -0.5])
def test_non_positive_record_ttl_min_keeps_as_invalid(ttl):
    old = _dict_rec(has_result=False, updated_at=NOW - timedelta(minutes=2), ttl_min=ttl)
    assert decide(old, NOW) == ("keep", "ttl_invalid")
    assert decide(_dict_rec(ttl={"ttl_min": ttl}), NOW) == ("keep", "ttl_invalid")


def test_positive_marker_ttl_min_is_unchanged():
    assert parse_ttl_marker({"ttl_min": 45, "allow_stop": True}) == (True, 45, None)
    assert parse_ttl_marker({"ttl_min": 0.5}) == (None, 0.5, None)
