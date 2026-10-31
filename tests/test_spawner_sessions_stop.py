"""tests/test_spawner_sessions_stop.py — SESSION-SPAWN-MCP stop-path tests."""

from __future__ import annotations

import json
import pytest
from plangraph.routing_errors import ParameterRefused
from sessions_support import UUID1, UUID2, UUID3, FakeRun, make, go
from sessions_support import site  # noqa: F401


def test_stop_calls_claude_stop_never_rm(site):
    # r7 adapt: the fake rows carry the live row a real stop sees - with no
    # row the poll-first check marks stopped without calling claude stop.
    run = FakeRun(stdout="session " + UUID1, agents_rows=[
        {"id": "r", "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "running", "startedAt": 1_000_000}])
    s = make(site, run, now=lambda: 2000.0)
    go(site, s)
    res = s.stop(name="alpha")
    assert res["ok"] is True and res["session_id"] == UUID1
    stop_calls = [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert stop_calls == [["claude", "stop", UUID1[:8]]]  # short job id, never rm/kill
    # the stop is one subprocess call plus the confirmation agents poll
    assert run.calls[-2][0] == ["claude", "stop", UUID1[:8]]
    assert run.calls[-1][0][:3] == ["claude", "agents", "--json"]  # the confirmation
    # r7: the earlier launch + the pre-stop poll + the stop + its confirmation
    assert len(run.calls) == 4
    assert run.calls[-2][1]["timeout"] == 30  # bounded stop timeout
    for argv, _ in run.calls:
        assert "rm" not in argv and "kill" not in argv and "-9" not in argv
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["stopped_ts"] == 2000.0


def test_stop_without_id_refuses(site):
    run = FakeRun(stdout="done\n")  # no id anywhere; claude agents returns nothing
    s = make(site, run, resolve_budget=0.0)
    res = go(site, s)
    assert res["session_id"] is None
    # a null id is no longer refused (slice 3a): no row means nothing to stop
    res = s.stop(name="alpha")
    assert res["ok"] is True
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "stopped" and rec["stopped_ts"] is not None
    with pytest.raises(ParameterRefused, match="unknown session"):
        s.stop(name="ghost")
    with pytest.raises(ParameterRefused, match="name or id"):
        s.stop()
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]


def test_stop_confirmation_poll_failure_leaves_record_unchanged(site):
    class PollDown(FakeRun):
        def __call__(self, argv, **kw):
            if self._is_agents(argv):
                raise OSError("agents poll down")  # -> _ActionFailed from the strict poll
            return super().__call__(argv, **kw)

    run = PollDown(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    before = (site.state / "sessions" / "alpha.json").read_text()
    res = s.stop(name="alpha")
    # the poll failure propagates as is (_ActionFailed): the stop does not
    # succeed and the record is never marked stopped
    assert res["ok"] is False and res["session_id"] is None
    assert (site.state / "sessions" / "alpha.json").read_text() == before  # record unchanged


def test_stop_null_id_finds_id_by_name_and_stops(site):
    run = FakeRun(stdout="done\n")  # launch resolves no id -> record state "unknown"
    s = make(site, run, resolve_budget=0.0)
    res = go(site, s)
    assert res["session_id"] is None
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "unknown" and rec["session_id"] is None
    # by stop time the agent is listed: only name + kind + startedAt near the
    # record's own launch (now*1000 = 1_000_000) may resolve it, newest wins
    run.agents_rows = [
        {"id": "s", "sessionId": UUID3, "name": "alpha", "kind": "background",
         "startedAt": 1_000_000 - 60_000},  # too old for the launch window
        {"id": "k", "sessionId": UUID2, "name": "alpha", "kind": "foreground",
         "startedAt": 1_000_000},  # wrong kind
        {"id": "o", "sessionId": UUID2, "name": "alpha", "kind": "background",
         "startedAt": 996_000},  # in window but older
        {"id": "f", "sessionId": UUID1, "name": "alpha", "kind": "background",
         "startedAt": 1_000_000},  # newest in-window background row
    ]
    res = s.stop(name="alpha")
    assert res["ok"] is True and res["session_id"] == UUID1
    assert [a for a, _ in run.calls if a[:2] == ["claude", "stop"]] == [["claude", "stop", UUID1[:8]]]
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "stopped" and rec["session_id"] == UUID1
    assert rec["stopped_ts"] is not None


def test_stop_already_stopped_runs_no_subprocess(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    s.stop(name="alpha")
    calls_before = list(run.calls)
    res = s.stop(name="alpha")  # the record is already stopped
    assert res["ok"] is True and res["session_id"] == UUID1
    # adapted (r5 slice 4, seat HIGH): the stopped record's id is re-checked
    # with one strict poll - and never stopped twice, no other subprocess.
    added = [a for a, _ in run.calls[len(calls_before):]]
    assert [a[:3] for a in added] == [["claude", "agents", "--json"]]
    assert not any(a[:2] == ["claude", "stop"] for a in added)


def test_stop_argv_is_never_rm_or_kill(site):
    run = FakeRun(stdout="session " + UUID1, agents_rows=[  # r7 adapt: live row
        {"id": "r", "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "running", "startedAt": 1_000_000}])
    s = make(site, run)
    go(site, s)
    s.stop(name="alpha")
    for argv, _ in run.calls:
        assert not {"rm", "kill", "killall", "pkill"} & set(argv)
        assert "-9" not in argv
    stops = [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert stops == [["claude", "stop", UUID1[:8]]]  # claude stop is the only stop mechanism


def test_win32_stop_argv_uses_resolved_exe(site):
    exe = str(site.tmp_path / "claude.exe")
    run = FakeRun(stdout="session " + UUID1, agents_rows=[  # r7 adapt: live row
        {"id": "r", "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "running", "startedAt": 1_000_000}])
    s = make(site, run, platform="win32", which=lambda _: exe)
    go(site, s)
    res = s.stop(name="alpha")
    assert res["ok"] is True
    stops = [a for a, _ in run.calls if a[1:2] == ["stop"]]
    assert stops == [[exe, "stop", UUID1[:8]]]  # the resolved .exe, never bare "claude"


def test_stop_rejects_name_id_mismatch(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    with pytest.raises(ParameterRefused, match="match"):
        s.stop(name="alpha", session_id=UUID2)
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["stopped_ts"] is None  # nothing was stopped
    assert not any(argv[:2] == ["claude", "stop"] for argv, _ in run.calls)
    assert s.stop(name="alpha", session_id=UUID1)["ok"] is True  # matching pair works


def test_stop_writes_record_only_to_validated_name_path(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    rec_path = site.state / "sessions" / "alpha.json"
    rec = json.loads(rec_path.read_text())
    rec["name"] = "beta"  # a lying record must not redirect the write
    rec_path.write_text(json.dumps(rec))
    # r5 slice 2: a record whose name field does not match the lookup name is
    # never returned - the lying record refuses the stop outright (fail closed)
    with pytest.raises(ParameterRefused, match="unknown session"):
        s.stop(name="alpha")
    out = json.loads(rec_path.read_text())
    assert out["stopped_ts"] is None  # nothing was stopped or written back
    assert not (site.state / "sessions" / "beta.json").exists()  # never a content-derived path


def test_exit_failure_record_state_stopped(site):
    run = FakeRun(stdout="untrusted workspace", launch_rc=1)
    s = make(site, run)
    res = go(site, s)
    assert res["ok"] is False
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "stopped" and rec["stopped_ts"] is not None  # nothing was started
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert "claude exited 1: untrusted workspace" in line["reason"]  # slice-2a stdout text
