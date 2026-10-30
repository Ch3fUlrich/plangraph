"""tests/test_spawner_sessions_r7.py — SESSION-SPAWN-MCP r8 fix slice 1 tests.

Pin the r8 seat findings (r7 seats qwen MED + Sonnet LOW): _stop_record must
attempt `claude stop <id>` once when the strict poll does not list a recorded
id - a live-but-unlisted id was written `stopped` with nothing stopped, so a
relaunch could start a second same-name session beside the orphaned one. Its
rc still gates the relaunch: rc 1 refuses unless the id is unlisted before
AND after (the session had already ended). _list_inner matches a null-id
record to an agents row with the _stop_record filter (name + background +
state not in FINISHED_STATES), never a finished row by name alone.
"""

from __future__ import annotations

import json

import pytest

from plangraph.routing_errors import ParameterRefused
from sessions_support import UUID1, UUID2, FakeRun, go, make
from sessions_support import site  # noqa: F401


def _running_record(site):
    """A hand-written running record for alpha with session id UUID1 - the
    live-but-unlisted state the stop under test must not lose."""
    sess = site.state / "sessions"
    sess.mkdir(parents=True)
    (sess / "alpha.json").write_text(json.dumps({
        "name": "alpha", "state": "running", "session_id": UUID1,
        "ts": 1000, "stopped_ts": None}) + "\n")
    return sess


def test_stop_unlisted_id_attempts_claude_stop(site):
    # qwen MED: an unlisted id is NOT assumed gone - a live-but-unlisted
    # session must still get `claude stop <id>` or relaunch orphans it.
    run = FakeRun()
    s = make(site, run)
    _running_record(site)
    res = s.stop(name="alpha")
    assert res["ok"] is True and res["session_id"] == UUID1
    assert [a for a, _ in run.calls if a[:2] == ["claude", "stop"]] == \
        [["claude", "stop", UUID1]]  # exact argv, never rm/kill
    assert [a[:2] for a, _ in run.calls] == \
        [["claude", "agents"], ["claude", "stop"], ["claude", "agents"]]
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "stopped" and rec["session_id"] == UUID1
    assert rec["stopped_ts"] is not None


def test_stop_unlisted_id_stop_rc1_still_recovers(site):
    # qwen MED: `claude stop` exiting 1 for an id listed neither before nor
    # after means the session had already ended - recover the name, do not
    # refuse the stop (and with it launch and relaunch) forever.
    run = FakeRun(stop_rc=1)
    s = make(site, run)
    _running_record(site)
    res = s.stop(name="alpha")
    assert res["ok"] is True and res["session_id"] == UUID1
    assert [a for a, _ in run.calls if a[:2] == ["claude", "stop"]] == \
        [["claude", "stop", UUID1]]  # attempted exactly once
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "stopped" and rec["session_id"] == UUID1
    assert rec["stopped_ts"] is not None


def test_stop_rc1_on_listed_id_refuses_and_keeps_record(site):
    # qwen MED: the rc still gates - a listed id whose `claude stop` exits 1
    # is refused with the unchanged text and the record is left untouched.
    run = FakeRun(stop_rc=1, agents_rows=[
        {"id": "r", "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "running", "startedAt": 1_000_000}])
    s = make(site, run)
    sess = _running_record(site)
    before = (sess / "alpha.json").read_text()
    with pytest.raises(ParameterRefused, match="stop failed"):
        s.stop(name="alpha")
    assert [a for a, _ in run.calls if a[:2] == ["claude", "stop"]] == \
        [["claude", "stop", UUID1]]
    assert (sess / "alpha.json").read_text() == before  # record unchanged


def test_relaunch_stops_unlisted_old_id_before_bg(site):
    # qwen MED: relaunch is stop-then-launch - an unlisted old id must be
    # passed to `claude stop` BEFORE the new --bg, never abandoned in place.
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)  # running record with id UUID1
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    run.agents_rows = []  # the old id is unlisted at relaunch time
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha")
    assert res["ok"] is True and res["name"] == "alpha"
    assert res["session_id"] == UUID2  # the name is kept for the new session
    stops = [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert stops == [["claude", "stop", UUID1]]  # old id stopped exactly once
    kinds = [a[:2] for a, _ in run.calls]
    bg = [i for i, k in enumerate(kinds) if k == ["claude", "--bg"]]
    assert len(bg) == 2 and kinds.index(["claude", "stop"]) < bg[1]


def test_list_null_id_never_matches_finished_row(site):
    # Sonnet LOW: _list_inner matched a null-id record to an agents row by
    # name only - a finished row must never lend its state to the record.
    sess = site.state / "sessions"
    sess.mkdir(parents=True)
    (sess / "alpha.json").write_text(json.dumps({
        "name": "alpha", "state": "running", "session_id": None,
        "ts": 1000, "stopped_ts": None}) + "\n")
    run = FakeRun(agents_rows=[
        {"id": "r", "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "finished", "startedAt": 1_000_000}])
    s = make(site, run)
    row = s.list()["sessions"][0]
    assert row["id"] is None
    assert row["state"] == "running"  # from the record, not the finished row
    run.agents_rows[0]["state"] = "working"  # a live background row still matches
    row = s.list()["sessions"][0]
    assert row["state"] == "working"
