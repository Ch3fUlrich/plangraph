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
    # list for every session. Only usable values count and an unusable line is
    # skipped - never raise (r10 corrects the r9 "counts as 0" expectation).
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    tdir = site.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(site.proj))
    tdir.mkdir(parents=True)
    good = json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": {
        "input_tokens": 250, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0}}})
    junk = json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": {
        "input_tokens": "abc", "cache_read_input_tokens": True,
        "cache_creation_input_tokens": [1]}}})
    tf = tdir / f"{UUID1}.jsonl"
    tf.write_text(good + "\n")
    assert s.list()["sessions"][0]["context_tokens"] == 250  # transcript is read
    tf.write_text(good + "\n" + junk + "\n")
    row = s.list()["sessions"][0]  # newest line's odd usage: never raises
    # r10 qwen F2 fixes this test's r9 expectation (pinned 0 here): a newest
    # assistant line whose usage holds no usable value is skipped entirely,
    # so the last good reading - 250 - is what a recycle-deciding poller sees.
    assert isinstance(row["context_tokens"], int) and row["context_tokens"] == 250


def test_list_context_pct_clamped_to_one(site):
    # Sonnet LOW: _list_inner computed context_pct = tokens / cap unclamped,
    # so a transcript whose last assistant usage passed ctx_cap_tokens yielded
    # a fraction above 1 despite the docs saying "a fraction in 0..1". The
    # pct clamps at 1.0; the raw context_tokens is still reported over the cap.
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    tdir = site.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(site.proj))
    tdir.mkdir(parents=True)
    over = json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": {
        "input_tokens": 400000, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0}}})
    (tdir / f"{UUID1}.jsonl").write_text(over + "\n")
    row = s.list()["sessions"][0]
    assert row["context_tokens"] == 400000  # raw size, above the 300000 cap
    assert row["context_tokens"] > 300000
    assert row["context_pct"] == 1.0  # clamped, not 400000 / 300000


def test_list_usage_nan_inf_negative_and_fractional_never_count(site):
    # Sonnet LOW (F1): the r9 filter accepted any int/float, so NaN,
    # Infinity, a negative or a fractional usage value gave a non-integer or
    # negative context_tokens (and a NaN context_pct - not even valid JSON
    # for MCP callers). Only non-negative ints count, the total is an int,
    # and context_pct stays inside the documented 0..1 fraction.
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    tdir = site.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(site.proj))
    tdir.mkdir(parents=True)
    tf = tdir / f"{UUID1}.jsonl"
    mixed = json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": {
        "input_tokens": -5, "cache_read_input_tokens": 1.5,
        "cache_creation_input_tokens": 77,
        "output_tokens": float("nan"), "total_tokens": float("inf")}}})
    tf.write_text(mixed + "\n")
    row = s.list()["sessions"][0]
    assert row["context_tokens"] == 77 and type(row["context_tokens"]) is int
    assert 0.0 <= row["context_pct"] <= 1.0
    json.dumps(s.list(), allow_nan=False)  # NaN/Infinity must never leak out
    # NaN/Infinity/-5/1.5 in the counted keys leave no usable value at all:
    # the line is skipped and the previous good reading (77) survives.
    gone = json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": {
        "input_tokens": float("nan"), "cache_read_input_tokens": float("inf"),
        "cache_creation_input_tokens": -5, "output_tokens": 1.5}}})
    tf.write_text(mixed + "\n" + gone + "\n")
    row = s.list()["sessions"][0]
    assert row["context_tokens"] == 77
    assert 0.0 <= row["context_pct"] <= 1.0
    json.dumps(s.list(), allow_nan=False)


def test_stop_poll_row_dash_session_id_refuses_without_stop_argv(site):
    # qwen LOW (F3): a poll row's sessionId is passed straight through as
    # `claude stop <id>` argv - "-rf" becomes a flag. A row whose id is not
    # a safe token fails closed: the stop is refused "agents poll failed",
    # no claude stop argv runs, the record keeps running, and list still
    # degrades without raising.
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    before = (site.state / "sessions" / "alpha.json").read_text()
    run.agents_rows = [{"id": "r", "sessionId": "-rf", "name": "alpha",
                        "kind": "background", "state": "running"}]
    res = s.stop(name="alpha")
    assert res["ok"] is False and "agents poll failed" in res["error"]
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert (site.state / "sessions" / "alpha.json").read_text() == before
    rows = s.list()["sessions"]  # the lenient poll still degrades to the record
    assert [r["name"] for r in rows] == ["alpha"] and rows[0]["state"] == "running"


def test_relaunch_poll_row_spaced_session_id_refuses_and_starts_nothing(site):
    # qwen LOW (F3) on the relaunch path: "a b" would split into two argv
    # words. The unshapeable id refuses the relaunch before a second --bg.
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    before = (site.state / "sessions" / "alpha.json").read_text()
    run.agents_rows = [{"id": "r", "sessionId": "a b", "name": "alpha",
                        "kind": "background", "state": "running"}]
    res = s.relaunch("alpha")
    assert res["ok"] is False and "agents poll failed" in res["error"]
    assert len(run.launch_calls()) == 1  # no second --bg call
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert (site.state / "sessions" / "alpha.json").read_text() == before


def test_stop_record_flag_session_id_refuses_before_any_subprocess(site):
    # qwen LOW (F3): a record's session_id is never shape-checked before it
    # becomes `claude stop <id>` argv and is echoed back to the caller -
    # "--help" would become a flag. The id is validated before any poll or
    # argv: the stop refuses "stop refused: bad session id" and the runner
    # records no subprocess at all.
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    sess = site.state / "sessions"
    sess.mkdir(parents=True)
    (sess / "alpha.json").write_text(json.dumps({
        "name": "alpha", "state": "running", "session_id": "--help",
        "ts": 1000, "stopped_ts": None}) + "\n")
    res = s.stop(name="alpha")
    assert res["ok"] is False and "stop refused: bad session id" in res["error"]
    assert run.calls == []  # no poll, no argv
    rec = json.loads((sess / "alpha.json").read_text())
    assert rec["state"] == "running" and rec["session_id"] == "--help"
