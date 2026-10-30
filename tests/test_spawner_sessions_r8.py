"""tests/test_spawner_sessions_r8.py — SESSION-SPAWN-MCP r9 slice 1.

A `claude agents --json` poll that exits non-zero must fail closed (never read
as "no live session"), and odd usage values in a transcript must never break
list."""

from __future__ import annotations

import json
import re

from sessions_support import UUID1, FakeRun, go, make
from sessions_support import site  # noqa: F401


def _running_record(site):
    """A hand-written running record for alpha with session id UUID1 - the
    record the failing poll must not mark stopped."""
    sess = site.state / "sessions"
    sess.mkdir(parents=True)
    (sess / "alpha.json").write_text(json.dumps({
        "name": "alpha", "state": "running", "session_id": UUID1,
        "ts": 1000, "stopped_ts": None}) + "\n")
    return sess


def test_relaunch_agents_poll_failure_refuses_and_starts_nothing(site):
    # qwen HIGH: `claude agents --json` exiting non-zero with empty stdout hit
    # json.loads("[]") and returned [], which relaunch read as "no live
    # session" - starting a second bypassPermissions session under the same
    # --remote-control name. A poll that fails refuses the action (fail
    # closed): no second --bg, and the record is left untouched.
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)  # running record with id UUID1
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    before = (site.state / "sessions" / "alpha.json").read_text()
    run.agents_rc = 1
    run.agents_rows = []
    res = s.relaunch("alpha")
    assert res["ok"] is False and "agents poll failed" in res["error"]
    assert len(run.launch_calls()) == 1  # no second --bg call
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert (site.state / "sessions" / "alpha.json").read_text() == before  # record unchanged


def test_stop_agents_poll_failure_leaves_record_running(site):
    # qwen HIGH: the same non-zero-exit poll returning [] let stop mark a
    # still-running record stopped. The strict poll gates the stop: when it
    # fails, the stop is refused and the record is never marked stopped.
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    before = (site.state / "sessions" / "alpha.json").read_text()
    run.agents_rc = 1
    run.agents_rows = []
    res = s.stop(name="alpha")
    assert res["ok"] is False and "agents poll failed" in res["error"]
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "running" and rec["stopped_ts"] is None
    assert (site.state / "sessions" / "alpha.json").read_text() == before  # record unchanged


def test_list_agents_poll_failure_degrades_to_record_state(site):
    # qwen HIGH corollary: the lenient poll stays lenient - a failed poll
    # degrades list to "no agent rows" and the state comes from the record.
    run = FakeRun(agents_rc=1, agents_rows=[])
    s = make(site, run)
    _running_record(site)
    rows = s.list()["sessions"]
    assert [r["name"] for r in rows] == ["alpha"]
    assert rows[0]["id"] == UUID1
    assert rows[0]["state"] == "running"  # from the record, not the poll


def test_list_odd_usage_values_never_raise(site):
    # Sonnet LOW: int(usage.get(key) or 0) raised ValueError/TypeError on a
    # non-numeric string or list in a usage field, so one odd transcript broke
    # list for every session. Only int/float (non-bool) values count; a line
    # whose usage has no such values counts as 0 for those keys - never raise.
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    tdir = site.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(site.proj))
    tdir.mkdir(parents=True)
    good = json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": {
        "input_tokens": 250, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0}}})
    junk = json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": {
        "input_tokens": "abc", "output_tokens": [1]}}})
    tf = tdir / f"{UUID1}.jsonl"
    tf.write_text(good + "\n")
    assert s.list()["sessions"][0]["context_tokens"] == 250  # transcript is read
    tf.write_text(good + "\n" + junk + "\n")
    row = s.list()["sessions"][0]  # last assistant line's odd usage: never raises
    assert isinstance(row["context_tokens"], int) and row["context_tokens"] == 0
