"""tests/test_spawner_sessions_stopid.py — SESSION-SPAWN-MCP stop-id tests (D-3343).

`claude stop` only accepts the 8-char short job id: with the full sessionId it
exits 1 with "No job matching '<id>'". `_stop_record` must pass the matching
`claude agents` row's "id" when that id is a prefix of the sessionId, else
sid[:8] - every other check stays keyed on the full sessionId. The rows below
are copied verbatim from `claude agents --json --all`, where the terminal
states are "done" and "failed" (live: "working", "blocked"), so those two must
count as finished alongside "finished" and "stopped".
"""

from __future__ import annotations

import json

from sessions_support import FakeRun, make, go
from sessions_support import site  # noqa: F401

ROW_WORKING = {"id": "87dcc5e9", "cwd": "/home/s/code/plangraph", "kind": "background",
               "startedAt": 1793445596000, "sessionId": "87dcc5e9-fae1-42a9-838e-9b834de20647",
               "name": "pg-session-smoke-b", "state": "working"}
ROW_DONE = {"id": "abb0102e", "cwd": "/home/s/code/plangraph", "kind": "background",
            "startedAt": 1793445560144, "sessionId": "abb0102e-8df4-4311-ba1d-78ecb7bee5d3",
            "name": "pg-session-smoke", "state": "done"}
ROW_FAILED = {"id": "c34d06f5", "cwd": "/home/s/code/plangraph", "kind": "background",
              "startedAt": 1793445496758, "sessionId": "c34d06f5-90f2-4865-8847-a84a8b010219",
              "name": "pg-session-smoke", "state": "failed"}


def _rec(site, name):
    return json.loads((site.state / "sessions" / f"{name}.json").read_text())


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
    stops = [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
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
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
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
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    rec = _rec(site, row["name"])
    assert rec["state"] == "stopped" and rec["session_id"] == row["sessionId"]
    assert rec["stopped_ts"] is not None


def test_launch_resolution_skips_failed_and_done_rows(site):
    # A terminal row is never the new session's id (D-3343): with only
    # "failed"/"done" rows listed - even one whose short "id" matches the
    # launcher's own "backgrounded" token - the id stays unresolved instead
    # of adopting a session that is already over.
    run = FakeRun(stdout="backgrounded (c34d06f5)",
                  agents_rows=[dict(ROW_DONE), dict(ROW_FAILED)])
    s = make(site, run)
    res = go(site, s, name=ROW_FAILED["name"])
    assert res["session_id"] is None and "warning" in res
    rec = _rec(site, ROW_FAILED["name"])
    assert rec["state"] == "unknown" and rec["session_id"] is None
