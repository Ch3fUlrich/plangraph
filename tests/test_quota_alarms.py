"""Hermetic quota alarm checks over fixture snapshot series."""
import json
import sqlite3
import subprocess
from pathlib import Path
from datetime import UTC, datetime, timedelta

from plangraph.keeper import quota_alarms
from plangraph.keeper.quota_alarms import evaluate, run

T0 = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
RESET = "2026-11-19T00:00:00.000Z"


def test_timer_requires_read_only_snapshot_source():
    unit = (Path(__file__).resolve().parents[1] / "deploy/systemd/plangraph-quota-alarms.service").read_text()
    assert "EnvironmentFile=%h/.config/plangraph/quota-alarms.env" in unit
    assert 'ExecStartPre=/usr/bin/test -n "${QUOTA_SNAPSHOT_DB}"' in unit
    assert '--db "${QUOTA_SNAPSHOT_DB}"' in unit


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
    assert evaluate([row(0, 5)], now(240)) == []
    assert evaluate([row(0, 5, reset="2026-11-13T09:00:00.000Z")], now(0)) == []


def test_hourly_cadence_with_jitter_is_not_stale():
    w = evaluate([row(0, 40)], now(100))  # 100 min since the hourly snapshot
    assert len(w) == 1 and w[0]["used"] == 60


def test_stale_snapshot_raises_visible_notice_once_per_day(tmp_path):
    sent = []
    state = tmp_path / "s.json"
    rows = [row(0, 40)]
    lines = run(rows, state, sent.append, now=now(240))  # writer stopped after 3 h
    assert len(lines) == 1 and lines[0].startswith("quota-alarm-blind") and sent == lines
    assert "codex/weekly last=" in lines[0]
    assert run(rows, state, sent.append, now=now(300)) == []  # same episode, no repeat
    assert len(run(rows, state, sent.append, now=now(240 + 25 * 60))) == 1  # daily reminder


def test_partially_stale_windows_get_stale_notice_and_fresh_ones_still_alarm(tmp_path):
    rows = [row(0, 40, window="daily"), row(300, 20)]
    lines = run(rows, tmp_path / "s.json", lambda line: None, now=now(300))
    assert sorted(l.split()[0] for l in lines) == ["quota-alarm", "quota-alarm", "quota-alarm-stale"]
    assert any("codex/daily last=" in l for l in lines)


def test_empty_table_is_blind_notice(tmp_path):
    lines = run([], tmp_path / "s.json", lambda line: None, now=now(0))
    assert len(lines) == 1 and lines[0].startswith("quota-alarm-blind")


def test_null_reset_windows_rearm_after_refill(tmp_path):
    sent = []
    state = tmp_path / "s.json"
    first = [row(0, 60, reset=None), row(30, 5, reset=None)]
    assert len(run(first, state, sent.append, now=now(30))) == 3
    assert run(first, state, sent.append, now=now(30)) == []
    second = first + [row(60, 100, reset=None), row(90, 40, reset=None)]  # refill, new window
    lines = run(second, state, sent.append, now=now(90))
    assert len(lines) == 1 and ">=50%" in lines[0] and len(sent) == 4
    # the refill row slides out of the lookback: still the same window, no repeat
    assert run([row(90, 40, reset=None), row(120, 35, reset=None)], state, sent.append,
               now=now(120)) == []
    third = [row(90, 40, reset=None), row(150, 100, reset=None), row(180, 20, reset=None)]
    assert len(run(third, state, sent.append, now=now(180))) == 2  # 50 and 75 again
    run(third, state, sent.append, now=now(195))
    keys = json.loads(state.read_text())["sent"]
    assert len(keys) == 2 and all(k.endswith(("|50", "|75")) for k in keys)  # old runs pruned


def test_null_reset_burn_rate_ignores_previous_window():
    rows = [row(0, 60, reset=None), row(30, 5, reset=None),
            row(60, 100, reset=None), row(90, 40, reset=None)]
    w = evaluate(rows, now(90))[0]
    assert w["rate"] == 120.0 and w["rolled"] is True


def test_rows_out_of_order_use_the_newest_snapshot():
    w = evaluate([row(60, 40), row(0, 80)], now(60))
    assert w[0]["used"] == 60 and w[0]["rate"] == 40.0


def test_state_prunes_windows_whose_reset_passed(tmp_path):
    state = tmp_path / "s.json"
    run([row(0, 5)], state, lambda line: None, now=now(0))
    assert len(json.loads(state.read_text())["sent"]) == 3
    new = "2026-11-26T00:00:00.000Z"
    later = T0 + timedelta(days=7, hours=1)
    rows = [row(7 * 24 * 60 + 60, 5, reset=new)]
    run(rows, state, lambda line: None, now=later.timestamp())
    keys = json.loads(state.read_text())["sent"]
    assert len(keys) == 3 and all(new in k for k in keys)


def make_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE quota_snapshots (id INTEGER PRIMARY KEY, provider TEXT, "
                "connection_id TEXT, window_key TEXT, remaining_percentage REAL, "
                "is_exhausted INTEGER, next_reset_at TEXT, created_at TEXT, raw_data TEXT)")
    for r in rows:
        con.execute("INSERT INTO quota_snapshots (provider,connection_id,window_key,"
                    "remaining_percentage,is_exhausted,next_reset_at,created_at,raw_data) "
                    "VALUES (?,?,?,?,?,?,?,'SECRET')",
                    (r["provider"], r["connection_id"], r["window_key"],
                     r["remaining_percentage"], 0, r["next_reset_at"], r["created_at"]))
    con.commit()
    con.close()


def test_direct_db_read_needs_no_docker_and_no_raw_data(tmp_path, monkeypatch, capsys):
    def no_docker(*a, **k):
        raise AssertionError("docker exec must not run when --db is given")
    monkeypatch.setattr(subprocess, "run", no_docker)
    stamp = lambda m: (datetime.now(UTC) - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    reset = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    old = {**row(0, 90), "created_at": stamp(60 * 24 * 3), "next_reset_at": reset}
    new = {**row(0, 5), "created_at": stamp(10), "next_reset_at": reset}
    db = tmp_path / "storage.sqlite"
    make_db(db, [old, new])
    rows = quota_alarms.fetch_rows_db(stamp(60 * 6), str(db))
    assert [r["remaining_percentage"] for r in rows] == [5] and "raw_data" not in rows[0]
    assert quota_alarms.main(["--db", str(db), "--state", str(tmp_path / "st.json"), "--dry-run"]) == 0
    assert capsys.readouterr().out.count("quota-alarm codex/weekly") == 3
    missing = tmp_path / "none.sqlite"
    try:
        quota_alarms.fetch_rows_db(stamp(60), str(missing))
        raise AssertionError("expected OperationalError")
    except sqlite3.OperationalError:
        assert not missing.exists()  # read-only open never creates the file
    assert "raw_data" not in quota_alarms.SQL and json.dumps(quota_alarms.SQL) in quota_alarms.JS


def test_dry_run_takes_the_same_lock(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(quota_alarms.fcntl, "flock", lambda fh, op: calls.append(op))
    monkeypatch.setattr(quota_alarms, "fetch_rows", lambda since: [])
    quota_alarms.main(["--state", str(tmp_path / "st.json"), "--dry-run"])
    assert calls == [quota_alarms.fcntl.LOCK_EX]


def test_db_timestamp_formats_share_the_same_cutoff(tmp_path):
    rows = [row(0, 90), row(30, 60), row(60, 40)]
    rows[1]['created_at'] = rows[1]['created_at'].replace('T', ' ')
    db = tmp_path / 'snapshots.sqlite'
    make_db(db, rows)
    found = quota_alarms.fetch_rows_db(row(15, 0)['created_at'], db)
    assert [r['remaining_percentage'] for r in found] == [60, 40]


def test_read_only_db_uri_escapes_special_path_characters(tmp_path):
    db = tmp_path / 'snapshot ? # %.sqlite'
    make_db(db, [row(0, 40)])
    assert quota_alarms.fetch_rows_db(row(0, 0)['created_at'], db)[0]['remaining_percentage'] == 40


def test_poll_recovers_refill_outside_burn_lookback(tmp_path, monkeypatch):
    from plangraph.fleet_bus import client
    from types import SimpleNamespace
    sent = []
    monkeypatch.setattr(client, 'build_client', lambda: SimpleNamespace(send=lambda routing, line: sent.append(line)))
    state = tmp_path / 'state.json'
    db = tmp_path / 'snapshots.sqlite'
    make_db(db, [row(0, 5, reset=None)])
    args = ['--state', str(state), '--db', str(db)]
    monkeypatch.setattr(quota_alarms.time, 'time', lambda: now(0))
    quota_alarms.main(args)
    assert len(sent) == 3
    db.unlink()
    make_db(db, [row(0, 5, reset=None), row(60, 100, reset=None), row(480, 5, reset=None)])
    monkeypatch.setattr(quota_alarms.time, 'time', lambda: now(480))
    quota_alarms.main(args)
    assert len(sent) == 6
    quota_alarms.main(args)
    assert len(sent) == 6


def test_null_reset_refill_compares_with_previous_poll(tmp_path):
    state = tmp_path / 'state.json'
    sent = []
    run([row(0, 5, reset=None)], state, sent.append, now=now(0))
    lines = run([row(480, 40, reset=None)], state, sent.append, now=now(480))
    assert len(lines) == 1 and '>=50%' in lines[0]
    assert run([row(490, 35, reset=None)], state, sent.append, now=now(490)) == []


def test_contribution_notes_use_existing_section():
    text = (Path(__file__).resolve().parents[1] / 'CONTRIBUTING.md').read_text()
    assert '# In-flight language exceptions' not in text
    assert 'Package R Python' not in text
