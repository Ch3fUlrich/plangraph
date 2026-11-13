"""Hermetic quota alarm checks over fixture snapshot series."""
from datetime import UTC, datetime, timedelta

from plangraph.keeper.quota_alarms import evaluate, run

T0 = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
RESET = "2026-11-19T00:00:00.000Z"


def row(minutes, remaining, reset=RESET, provider="codex", window="weekly"):
    return {"provider": provider, "connection_id": "abcdef123456", "window_key": window,
            "remaining_percentage": remaining, "is_exhausted": 0, "next_reset_at": reset,
            "created_at": (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}


def now(minutes):
    return (T0 + timedelta(minutes=minutes)).timestamp()


def test_burn_rate_and_projection():
    w = evaluate([row(0, 80), row(120, 60), row(240, 40)], now(240))[0]
    assert w["used"] == 60 and w["rate"] == 10.0
    assert w["empty"] == T0 + timedelta(hours=4 + 4)


def test_burn_rate_none_with_single_snapshot():
    w = evaluate([row(0, 30)], now(0))[0]
    assert w["rate"] is None and w["empty"] is None


def test_crossing_sends_one_line_per_threshold_and_dedups(tmp_path):
    sent = []
    state = tmp_path / "s.json"
    rows = [row(0, 60), row(60, 40)]
    lines = run(rows, state, sent.append, now=now(60))
    assert len(lines) == 1 and ">=50%" in lines[0] and "burn6h=20.0%/h" in lines[0]
    assert run(rows, state, sent.append, now=now(60)) == []
    rows.append(row(120, 5))  # jumps over 75 and 90 at once
    lines = run(rows, state, sent.append, now=now(120))
    assert [">=75%" in lines[0], ">=90%" in lines[1]] == [True, True] and len(sent) == 3


def test_window_reset_rearms_thresholds(tmp_path):
    sent = []
    state = tmp_path / "s.json"
    run([row(0, 20)], state, sent.append, now=now(0))
    new = "2026-11-26T00:00:00.000Z"
    rows = [row(0, 20), row(30, 5, reset=new)]
    lines = run(rows, state, sent.append, now=now(30))
    assert len(lines) == 3 and len(sent) == 5
    # burn rate ignores snapshots of the previous window
    assert evaluate(rows, now(30))[0]["rate"] is None


def test_dry_run_sends_and_saves_nothing(tmp_path):
    sent = []
    state = tmp_path / "s.json"
    assert len(run([row(0, 5)], state, sent.append, now=now(0), dry_run=True)) == 3
    assert not sent and not state.exists()


def test_stale_and_expired_windows_are_skipped():
    assert evaluate([row(0, 5)], now(180)) == []
    assert evaluate([row(0, 5, reset="2026-11-13T09:00:00.000Z")], now(0)) == []
