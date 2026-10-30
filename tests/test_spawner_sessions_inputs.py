"""tests/test_spawner_sessions_inputs.py — SESSION-SPAWN-MCP input validation and refusal tests."""

from __future__ import annotations

import asyncio
import json
import os
import time
import pytest
from plangraph.routing_errors import ParameterRefused
from plangraph.spawner.sessions import Sessions
from sessions_support import UUID1, UUID2, MODEL, EFFORT, MODE, FakeRun, write_config, make, go, _stopped
from sessions_support import site  # noqa: F401


def test_refuses_bad_name(site):
    run = FakeRun()
    s = make(site, run)
    for bad in ("", "a b", "a/b", "../x", "x" * 65, ".hid den?"):
        with pytest.raises(ParameterRefused, match="name"):
            go(site, s, name=bad)
    assert not run.calls
    assert not (site.state / "sessions").exists() or not list((site.state / "sessions").glob("*.json"))


def test_refuses_cwd_outside_roots(site, tmp_path_factory):
    run = FakeRun()
    s = make(site, run)
    # a sibling of tmp_path: outside every configured root (the fixture admits
    # tmp_path itself via cwd_roots)
    elsewhere = tmp_path_factory.mktemp("outside-cwd")
    with pytest.raises(ParameterRefused, match="cwd_roots"):
        go(site, s, cwd=str(elsewhere))
    link = site.root / "link"
    link.symlink_to(elsewhere)  # symlink inside a root escaping to outside
    with pytest.raises(ParameterRefused, match="cwd_roots"):
        go(site, s, cwd=str(link))
    with pytest.raises(ParameterRefused, match="cwd"):
        go(site, s, cwd=str(site.root / "missing"))
    assert not run.calls


def test_refuses_unlisted_model_effort_mode_mcp(site):
    s = make(site, FakeRun())
    with pytest.raises(ParameterRefused, match="model"):
        go(site, s, model="gpt-4")
    with pytest.raises(ParameterRefused, match="effort"):
        go(site, s, effort="low")
    with pytest.raises(ParameterRefused, match="permission_mode"):
        go(site, s, permission_mode="default")
    with pytest.raises(ParameterRefused, match="mcp_config"):
        go(site, s, mcp_config=str(site.tmp_path / "other.json"))


def test_refuses_missing_config(site):
    run = FakeRun()
    s = make(site, run, config_path=site.tmp_path / "absent.json")
    with pytest.raises(ParameterRefused, match="config"):
        go(site, s)
    bad = site.tmp_path / "bad.json"
    write_config(bad, "{not json")
    s2 = make(site, run, config_path=bad)
    with pytest.raises(ParameterRefused, match="config"):
        go(site, s2)
    # every relaunch is refused too, before anything runs
    (site.state / "sessions").mkdir(parents=True, exist_ok=True)
    (site.state / "sessions" / "alpha.json").write_text(json.dumps({"name": "alpha", "ts": 1000}))
    with pytest.raises(ParameterRefused, match="config"):
        s.relaunch("alpha")  # config load refuses first, marker or not
    assert not run.calls


def test_refuses_unreadable_prompt_or_handoff(site):
    s = make(site, FakeRun())
    with pytest.raises(ParameterRefused, match="prompt_file"):
        go(site, s, prompt=None, prompt_file=str(site.tmp_path / "ghost.md"))
    with pytest.raises(ParameterRefused, match="prompt_file"):
        go(site, s, prompt=None, prompt_file=str(site.tmp_path))
    with pytest.raises(ParameterRefused, match="prompt"):
        go(site, s, prompt="   ")
    pf = site.tmp_path / "prompt.md"
    pf.write_text("from file")
    with pytest.raises(ParameterRefused, match="exactly one"):
        go(site, s, prompt="both", prompt_file=str(pf))
    with pytest.raises(ParameterRefused, match="exactly one"):
        s.launch(name="alpha", cwd=str(site.proj), mcp_config=str(site.mcp),
                 model=MODEL, effort=EFFORT, permission_mode=MODE)
    with pytest.raises(ParameterRefused, match="handoff"):
        go(site, s, handoff=str(site.tmp_path / "ghost-handoff.md"))


def test_stop_confirmation_still_live_refuses_keeps_record(site):
    class RowsOutliveStop(FakeRun):
        """The unconfirmed stop: the row outlives the stop call."""
        def _finish_rows(self, sid):
            pass  # the row keeps its "working" state

    run = RowsOutliveStop(stdout="session " + UUID1, agents_rows=[
        {"id": UUID1[:8], "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "working", "startedAt": 1_000_000}])
    s = make(site, run)
    go(site, s)
    before = (site.state / "sessions" / "alpha.json").read_text()
    with pytest.raises(ParameterRefused, match="stop not confirmed"):
        s.stop(name="alpha")
    assert [a for a, _ in run.calls if a[:2] == ["claude", "stop"]] == [["claude", "stop", UUID1]]
    assert (site.state / "sessions" / "alpha.json").read_text() == before  # record unchanged


def test_relaunch_refuses_without_ready_marker(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    with pytest.raises(ParameterRefused, match="ready marker"):
        s.relaunch("alpha")
    marker = site.state / "sessions" / "alpha.ready.json"
    marker.write_text(json.dumps({"handoff": str(site.tmp_path / "ghost.md"), "card": None, "ts": 1000.0}))
    with pytest.raises(ParameterRefused, match="handoff"):
        s.relaunch("alpha")
    old = site.tmp_path / "old.md"
    old.write_text("old")
    os.utime(old, (500, 500))
    marker.write_text(json.dumps({"handoff": str(old), "card": None, "ts": 1000.0}))
    with pytest.raises(ParameterRefused, match="newer"):
        s.relaunch("alpha")
    assert len(run.launch_calls()) == 1  # nothing was ever relaunched


def test_spec_defaults_ctx_cap_and_compact_window(site):
    cfg = json.loads(site.cfg.read_text())
    cfg.pop("ctx_cap_tokens")  # spec defaults only when the config is silent
    cfg2 = site.tmp_path / "sessions-defaults.json"
    write_config(cfg2, json.dumps(cfg))
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run, config_path=cfg2)
    go(site, s)
    argv, kw = run.calls[0]
    assert kw["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "330000"
    tdir = site.home / ".claude" / "projects" / "".join(ch if ch.isalnum() else "-" for ch in str(site.proj.resolve()))
    tdir.mkdir(parents=True)
    (tdir / f"{UUID1}.jsonl").write_text(json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 350, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}}}) + "\n")
    row = s.list()["sessions"][0]
    assert row["context_tokens"] == 365
    assert row["context_pct"] == pytest.approx(365 / 300000)


def test_mcp_relaunch_refuses_undeclared_force(site, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=site.state, session_run=FakeRun())
    monkeypatch.setenv("PLANGRAPH_SESSIONS_CONFIG", str(site.cfg))

    async def go_rpc():
        with pytest.raises(ParameterRefused, match="undeclared"):
            await server.call_tool("plangraph_session_relaunch",
                                   {"name": "alpha", "force": True})  # cut key: undeclared

    asyncio.run(go_rpc())


def test_refuses_dash_prompt(site):
    run = FakeRun()
    s = make(site, run)
    with pytest.raises(ParameterRefused, match="prompt"):
        go(site, s, prompt="-n pwn --dangerously-skip-permissions")
    pf = site.tmp_path / "flag-prompt.md"
    pf.write_text("-flag in a file")
    with pytest.raises(ParameterRefused, match="prompt"):
        go(site, s, prompt=None, prompt_file=str(pf))
    assert not run.calls


def test_prompt_file_outside_cwd_roots_refused(site, tmp_path_factory):
    stray = tmp_path_factory.mktemp("stray-prompt")
    outside = stray / "prompt.md"
    outside.write_text("from outside")
    with pytest.raises(ParameterRefused, match="prompt_file"):
        go(site, make(site, FakeRun()), prompt=None, prompt_file=str(outside))


def test_files_must_stay_inside_roots(site, tmp_path_factory):
    stray = tmp_path_factory.mktemp("stray")
    outside_prompt = stray / "prompt.md"
    outside_prompt.write_text("from outside")
    outside_handoff = stray / "handoff.md"
    outside_handoff.write_text("state")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    with pytest.raises(ParameterRefused, match="prompt_file"):
        go(site, s, prompt=None, prompt_file=str(outside_prompt))
    with pytest.raises(ParameterRefused, match="handoff"):
        go(site, s, handoff=str(outside_handoff))
    with pytest.raises(ParameterRefused, match="handoff"):
        s.mark_ready("alpha", str(outside_handoff))
    with pytest.raises(ParameterRefused, match="handoff"):
        s.relaunch("alpha", handoff=str(outside_handoff))


def test_relaunch_refuses_stale_handoff_real_clock(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run, now=lambda: time.time())
    go(site, s)  # record ts is real wall clock
    stale = site.tmp_path / "stale.md"
    stale.write_text("old state")
    os.utime(stale, (1_000_000, 1_000_000))  # strictly older than the record ts
    fresh = site.tmp_path / "fresh.md"
    fresh.write_text("new state")
    s.mark_ready("alpha", str(fresh))
    with pytest.raises(ParameterRefused, match="newer"):
        s.relaunch("alpha", handoff=str(stale))  # supplied handoff older than record ts
    os.utime(fresh, (1_000_000, 1_000_000))  # marker handoff goes stale too
    with pytest.raises(ParameterRefused, match="newer"):
        s.relaunch("alpha")
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]


def test_launch_refuses_while_record_active(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    with pytest.raises(ParameterRefused, match="relaunch"):
        go(site, s)  # an active record is replaced only via relaunch
    s.stop(name="alpha")
    res = go(site, s)  # a stopped record may be replaced by a plain launch
    assert res["ok"] is True and res["session_id"] == UUID1


def test_prompt_file_written_before_start(site):
    seen = {}

    class ProbeRun(FakeRun):
        def __call__(self, argv, **kw):
            seen["prompt_file"] = (site.state / "sessions" / "alpha.prompt.txt").is_file()
            return super().__call__(argv, **kw)

    s = make(site, ProbeRun(stdout="session " + UUID1))
    go(site, s, prompt="early write")
    assert seen["prompt_file"] is True  # written before the session starts


def test_default_config_path_used_when_env_unset(site, monkeypatch):
    monkeypatch.delenv("PLANGRAPH_SESSIONS_CONFIG", raising=False)
    dflt = site.home / ".config" / "plangraph"
    dflt.mkdir(parents=True, mode=0o700)
    write_config(dflt / "sessions.json", site.cfg.read_text())
    s = Sessions(state_root=site.state, run=FakeRun(), now=lambda: 1000.0, sleep=lambda _s: None)
    res = go(site, s)
    assert res["ok"] is True  # ~/.config/plangraph/sessions.json is the default


@pytest.mark.parametrize("dry", [False, True])
def test_launch_refuses_when_stopped_old_id_still_listed(site, dry):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    rec_path, marker_path = _stopped(site, run, s)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",
                        "kind": "background", "state": "working", "startedAt": 1_000_000}]
    run.stdout = "session " + UUID2
    with pytest.raises(ParameterRefused,
                       match="old id .* still listed \\(working\\); use relaunch"):
        go(site, s, dry_run=dry)  # checked in dry_run too: it only reads
    assert len(run.launch_calls()) == 1  # no second session was started
    rec = json.loads(rec_path.read_text())
    assert rec["session_id"] == UUID1 and rec["stopped_ts"] is not None  # record untouched
    assert marker_path.exists()


def test_launch_refuses_over_unknown_record(site):
    s = make(site, FakeRun(stdout="done\n"), resolve_budget=0.0)
    res = go(site, s)
    assert res["ok"] is True and res["session_id"] is None and "warning" in res
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "unknown"  # rc 0 but no resolvable id
    run2 = FakeRun(stdout="session " + UUID1)
    s2 = make(site, run2)
    with pytest.raises(ParameterRefused, match="has a unknown record; stop or relaunch it"):
        go(site, s2)  # a possibly-live record is replaced only via relaunch
    assert run2.launch_calls() == []  # nothing was started


def test_relaunch_stop_not_confirmed_refuses_and_never_sees_bg_argv(site):
    class UnconfirmedRun(FakeRun):
        def _finish_rows(self, sid):
            pass  # the row outlives the stop: the confirmation poll must see it live

    run = UnconfirmedRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",
                        "kind": "background", "state": "running", "startedAt": 1_000_000}]
    run.stdout = "session " + UUID2
    with pytest.raises(ParameterRefused, match="stop not confirmed"):
        s.relaunch("alpha")
    assert [a[:2] for a, _ in run.calls] == \
        [["claude", "--bg"], ["claude", "stop"], ["claude", "agents"]]
    assert len(run.launch_calls()) == 1  # the relaunch never handed the run a --bg argv
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] == UUID1 and rec["stopped_ts"] is None  # fail closed: unchanged
    assert (site.state / "sessions" / "alpha.ready.json").exists()
