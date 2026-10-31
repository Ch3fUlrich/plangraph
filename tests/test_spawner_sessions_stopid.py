"""tests/test_spawner_sessions_stopid.py — SESSION-SPAWN-MCP stop-id tests (D-3343).

`claude stop` only accepts the 8-char short job id: with the full sessionId it
exits 1 with "No job matching '<id>'". `_stop_record` passes the matching
`claude agents` row's own "id" only when that id is exactly the 8-char prefix
of the sessionId; any other row id - a truncated "8"/"87", a too-long form or
another job's id - is not a guessable stop target and refuses "unresolved job
id" instead of guessing, and sid[:8] is used only when the row is None or has
no "id" at all. Every other check stays keyed on the full sessionId. The rows
below are copied verbatim from `claude agents --json --all`, where the terminal
states are "done" and "failed" (live: "working", "blocked"), so those two must
count as finished alongside "finished" and "stopped".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from plangraph.routing_errors import ParameterRefused

from sessions_support import FakeRun, make, go
from sessions_support import site  # noqa: F401

ROW_WORKING = {"id": "87dcc5e9", "cwd": "/work/plangraph", "kind": "background",
               "startedAt": 1793445596000, "sessionId": "87dcc5e9-fae1-42a9-838e-9b834de20647",
               "name": "pg-session-smoke-b", "state": "working"}
ROW_DONE = {"id": "abb0102e", "cwd": "/work/plangraph", "kind": "background",
            "startedAt": 1793445560144, "sessionId": "abb0102e-8df4-4311-ba1d-78ecb7bee5d3",
            "name": "pg-session-smoke", "state": "done"}
ROW_FAILED = {"id": "c34d06f5", "cwd": "/work/plangraph", "kind": "background",
              "startedAt": 1793445496758, "sessionId": "c34d06f5-90f2-4865-8847-a84a8b010219",
              "name": "pg-session-smoke", "state": "failed"}


def _rec(site, name):
    return json.loads((site.state / "sessions" / f"{name}.json").read_text())


def _stops(run):
    """Every `claude stop` argv this run saw (D-3343): the tail is asserted to
    be the 8-char short job id here, so every stop test checks argv[-1]'s
    length even when it only asserts that nothing was stopped."""
    stops = [a for a, _ in run.calls
             if Path(a[0]).name in ("claude", "claude.exe") and a[1:2] == ["stop"]]
    assert all(len(a[-1]) == 8 for a in stops)  # argv[-1] is the short job id
    return stops


def test_stop_working_row_stops_by_short_job_id(site):
    # The live row's own "id" IS the short job id `claude stop` accepts; the
    # full sessionId is kept for every record and confirmation check.
    row = dict(ROW_WORKING)
    run = FakeRun(stdout="session " + row["sessionId"], agents_rows=[row])
    s = make(site, run)
    res = go(site, s, name=row["name"])
    assert res["session_id"] == row["sessionId"]
    res = s.stop(name=row["name"])
    assert res["ok"] is True and res["session_id"] == row["sessionId"]
    stops = _stops(run)
    assert [a[-2:] for a in stops] == [["stop", "87dcc5e9"]]  # short job id, never the UUID
    rec = _rec(site, row["name"])
    assert rec["state"] == "stopped" and rec["session_id"] == row["sessionId"]
    assert rec["stopped_ts"] is not None


def test_stop_done_row_records_stopped_without_claude_stop(site):
    # "done" is terminal in live `claude agents` output: the row is already
    # over and the record must be marked stopped without calling claude stop.
    row = dict(ROW_DONE)
    run = FakeRun(stdout="session " + row["sessionId"], agents_rows=[row])
    s = make(site, run)
    res = go(site, s, name=row["name"])
    assert res["session_id"] == row["sessionId"]
    res = s.stop(name=row["name"])
    assert res["ok"] is True and res["session_id"] == row["sessionId"]
    assert _stops(run) == []
    rec = _rec(site, row["name"])
    assert rec["state"] == "stopped" and rec["session_id"] == row["sessionId"]
    assert rec["stopped_ts"] is not None


def test_stop_failed_row_records_stopped_without_claude_stop(site):
    # "failed" is terminal too: same recovery as "done", no claude stop.
    row = dict(ROW_FAILED)
    run = FakeRun(stdout="session " + row["sessionId"], agents_rows=[row])
    s = make(site, run)
    res = go(site, s, name=row["name"])
    assert res["session_id"] == row["sessionId"]
    res = s.stop(name=row["name"])
    assert res["ok"] is True and res["session_id"] == row["sessionId"]
    assert _stops(run) == []
    rec = _rec(site, row["name"])
    assert rec["state"] == "stopped" and rec["session_id"] == row["sessionId"]
    assert rec["stopped_ts"] is not None


@pytest.mark.parametrize("bad_id", ["87", "87dcc5e9-fae1", "deadbeef"])
def test_stop_row_id_not_short_job_id_refuses_without_claude_stop(site, bad_id):
    # The row's own "id" is the stop token only when it IS the 8-char prefix
    # of the sessionId (D-3343): a truncated "87", a too-long "87dcc5e9-fae1"
    # and another job's "deadbeef" are not guessable stop targets - the stop
    # refuses "unresolved job id" before any argv is built, `claude stop` is
    # never called, and the record keeps the live session.
    row = dict(ROW_WORKING, id=bad_id)
    run = FakeRun(stdout="session " + row["sessionId"], agents_rows=[row])
    s = make(site, run)
    res = go(site, s, name=row["name"])
    assert res["session_id"] == row["sessionId"]
    with pytest.raises(ParameterRefused, match="unresolved job id"):
        s.stop(name=row["name"])
    assert _stops(run) == []  # refused first: no claude stop call at all
    rec = _rec(site, row["name"])
    assert rec["state"] == "running" and rec["session_id"] == row["sessionId"]  # unchanged
    assert rec["stopped_ts"] is None


def test_stop_blocked_row_is_live_and_stops_by_short_job_id(site):
    # "blocked" is LIVE (only "done"/"failed" are terminal): the row is really
    # stopped, and the argv ends ["stop", <8 chars>] - the short job id.
    row = dict(ROW_WORKING, state="blocked")
    run = FakeRun(stdout="session " + row["sessionId"], agents_rows=[row])
    s = make(site, run)
    res = go(site, s, name=row["name"])
    assert res["session_id"] == row["sessionId"]
    res = s.stop(name=row["name"])
    assert res["ok"] is True and res["session_id"] == row["sessionId"]
    stops = _stops(run)
    assert [a[-2:] for a in stops] == [["stop", "87dcc5e9"]]  # ends ["stop", "<8 chars>"]
    rec = _rec(site, row["name"])
    assert rec["state"] == "stopped" and rec["session_id"] == row["sessionId"]
    assert rec["stopped_ts"] is not None


def test_relaunch_over_terminal_old_rows_stops_nothing(site):
    # A relaunch whose old rows are all terminal ("done"/"failed") proceeds
    # without ever calling `claude stop`, and never adopts a terminal row as
    # the new session's id even when the launcher claims one of their short
    # ids (D-3343).
    row = dict(ROW_DONE)
    run = FakeRun(stdout="session " + row["sessionId"], agents_rows=[row])
    s = make(site, run)
    res = go(site, s, name=row["name"])
    assert res["session_id"] == row["sessionId"]
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    run.agents_rows = [dict(ROW_DONE), dict(ROW_FAILED)]  # old rows in "done" and "failed"
    run.stdout = "backgrounded · " + ROW_FAILED["id"] + " · " + row["name"]
    res = s.relaunch(row["name"], handoff=str(hf))
    assert res["ok"] is True and res["session_id"] is None and "warning" in res
    assert len(run.launch_calls()) == 2  # the relaunch still launched
    assert _stops(run) == []  # terminal rows are never stopped
