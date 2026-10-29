"""tests/test_spawner_sessions.py — SESSION-SPAWN-MCP session lifecycle.

Every subprocess goes through one injectable runner; the fakes assert argv is a
list and that a shell is never involved. No real `claude` call is made.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import types
from pathlib import Path

import pytest

from plangraph.routing_errors import ParameterRefused
from plangraph.spawner.sessions import Sessions

UUID1 = "12345678-1234-1234-1234-123456789abc"
UUID2 = "abcdefab-1234-1234-1234-123456789abc"
UUID3 = "cccccccc-1234-1234-1234-123456789abc"
SHORT = "77b56beb"
MODEL, EFFORT, MODE = "mimo/mimo-v2.6-pro", "high", "bypassPermissions"
PROMPT = "do the thing"
DETACHED = 0x8 | 0x200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
BASE = dict(model=MODEL, effort=EFFORT, permission_mode=MODE, prompt=PROMPT)


class FakeRun:
    """The one injectable runner: records argv+kwargs, refuses shells."""

    def __init__(self, stdout=UUID1, launch_rc=0, agents_rows=(), stop_rc=0):
        self.stdout, self.launch_rc = stdout, launch_rc
        self.agents_rows, self.stop_rc = list(agents_rows), stop_rc
        self.calls = []

    def __call__(self, argv, **kw):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv), "argv must be a list of str"
        assert kw.get("shell") in (None, False), "shell=True must never be used"
        self.calls.append((list(argv), kw))
        if argv[:3] == ["claude", "agents", "--json"]:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(self.agents_rows), stderr="")
        if argv[:2] == ["claude", "stop"]:
            return types.SimpleNamespace(returncode=self.stop_rc, stdout="", stderr="")
        return types.SimpleNamespace(returncode=self.launch_rc, stdout=self.stdout, stderr="")

    def launch_calls(self):
        return [argv for argv, _ in self.calls if argv[:2] == ["claude", "--bg"] or argv[0] == "wt.exe"]


class FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv), "argv must be a list of str"
        assert kw.get("shell") in (None, False), "shell=True must never be used"
        self.calls.append((list(argv), kw))
        return types.SimpleNamespace(pid=1)


@pytest.fixture
def site(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "roots"
    proj = root / "proj"
    proj.mkdir(parents=True)
    mcp = tmp_path / "mcp.json"
    mcp.write_text("{}")
    cfg = tmp_path / "sessions.json"
    cfg.write_text(json.dumps({
        "cwd_roots": [str(root)], "models": [MODEL], "efforts": [EFFORT],
        "permission_modes": [MODE], "mcp_configs": [str(mcp)], "ctx_cap_tokens": 300000}))
    return types.SimpleNamespace(tmp_path=tmp_path, home=home, root=root, proj=proj,
                                 mcp=mcp, cfg=cfg, state=tmp_path / "state")


def make(site, run=None, popen=None, *, platform="linux", now=lambda: 1000.0,
         resolve_budget=15.0, config_path=None):
    return Sessions(state_root=site.state, config_path=str(config_path or site.cfg),
                    run=run or FakeRun(), popen=popen or FakePopen(),
                    now=now, platform=platform, resolve_budget=resolve_budget)


def go(site, s, name="alpha", **over):
    args = dict(BASE, name=name, cwd=str(site.proj), mcp_config=str(site.mcp))
    args.update(over)
    return s.launch(**args)


def test_argv_linux_is_list_no_shell(site):
    run = FakeRun(stdout="session " + UUID1)
    pop = FakePopen()
    s = make(site, run, pop)
    res = go(site, s)
    assert res["session_id"] == UUID1
    argv, kw = run.calls[0]
    assert argv == ["claude", "--bg", "--strict-mcp-config", "--mcp-config", str(site.mcp.resolve()),
                    "--remote-control", "alpha", "-n", "alpha", "--model", MODEL, "--effort", EFFORT,
                    "--permission-mode", MODE, PROMPT]
    assert all(isinstance(a, str) for a in argv)
    assert kw.get("shell") in (None, False)
    assert kw["cwd"] == str(site.proj.resolve())
    assert kw["env"]["FLEET_SESSION_NAME"] == "alpha"
    assert kw["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "330000"
    assert not pop.calls
    # style_file is appended via --append-system-prompt when configured
    style = site.tmp_path / "style.txt"
    style.write_text("be terse\n")
    cfg2 = site.tmp_path / "sessions2.json"
    cfg2.write_text(json.dumps({**json.loads(site.cfg.read_text()), "style_file": str(style)}))
    run2 = FakeRun(stdout="session " + UUID1)
    s2 = make(site, run2, config_path=cfg2)
    go(site, s2)
    argv2, _ = run2.calls[0]
    assert argv2[-3:] == ["--append-system-prompt", "be terse", PROMPT]


def test_argv_windows_bg_and_wt(site):
    run = FakeRun(stdout="session " + UUID1)
    pop = FakePopen()
    s = make(site, run, pop, platform="win32")
    go(site, s)
    argv, kw = run.calls[0]
    assert argv[:2] == ["claude", "--bg"]
    assert kw["creationflags"] == DETACHED
    assert kw.get("shell") in (None, False)
    assert not pop.calls
    cfg2 = site.tmp_path / "sessions-wt.json"
    cfg2.write_text(json.dumps({**json.loads(site.cfg.read_text()), "windows_launcher": "wt"}))
    run2 = FakeRun(stdout="session " + UUID1, agents_rows=[
        {"id": "f", "sessionId": UUID2, "name": "alpha", "kind": "background", "startedAt": 1_000_000}])
    pop2 = FakePopen()
    s2 = make(site, run2, pop2, platform="win32", config_path=cfg2)
    res = go(site, s2)
    wt_argv, wt_kw = pop2.calls[0]
    assert wt_argv[:9] == ["wt.exe", "-w", "0", "new-tab", "--title", "alpha",
                           "-d", str(site.proj.resolve()), "claude"]
    assert "--bg" not in wt_argv
    assert wt_argv[-1] == PROMPT
    assert wt_kw["creationflags"] == DETACHED
    assert wt_kw.get("shell") in (None, False)
    assert not run2.launch_calls()
    # wt resolves by name + startedAt only: the UUID in stdout must be ignored
    assert res["session_id"] == UUID2


def test_handoff_prefixes_prompt(site):
    hf = site.tmp_path / "handoff.md"
    hf.write_text("state at stop")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s, handoff=str(hf))
    assert run.calls[0][0][-1] == f"Restart from handoff {hf.resolve()}.\n\n" + PROMPT


def test_refuses_bad_name(site):
    run = FakeRun()
    s = make(site, run)
    for bad in ("", "a b", "a/b", "../x", "x" * 65, ".hid den?"):
        with pytest.raises(ParameterRefused, match="name"):
            go(site, s, name=bad)
    assert not run.calls
    assert not (site.state / "sessions").exists() or not list((site.state / "sessions").glob("*.json"))


def test_refuses_cwd_outside_roots(site):
    run = FakeRun()
    s = make(site, run)
    elsewhere = site.tmp_path / "elsewhere"
    elsewhere.mkdir()
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
    bad.write_text("{not json")
    s2 = make(site, run, config_path=bad)
    with pytest.raises(ParameterRefused, match="config"):
        go(site, s2)
    # every relaunch is refused too, before anything runs
    (site.state / "sessions").mkdir(parents=True, exist_ok=True)
    (site.state / "sessions" / "alpha.json").write_text(json.dumps({"name": "alpha", "ts": 1000}))
    with pytest.raises(ParameterRefused, match="config"):
        s.relaunch("alpha", force=True)
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


def test_dry_run_writes_nothing(site):
    run, pop = FakeRun(), FakePopen()
    s = make(site, run, pop)
    res = go(site, s, prompt="abcdefg", dry_run=True)
    assert set(res) == {"argv", "cwd", "env_keys"}
    assert res["argv"][-1] == "<7 chars>"
    assert all(isinstance(a, str) for a in res["argv"])
    assert "FLEET_SESSION_NAME" in res["env_keys"]
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in res["env_keys"]
    assert res["cwd"] == str(site.proj.resolve())
    assert run.calls == [] and pop.calls == []
    sessions_dir = site.state / "sessions"
    assert not sessions_dir.exists() or not any(sessions_dir.iterdir())


def test_launch_writes_record_and_audit(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    res = go(site, s)
    assert res["ok"] is True
    rec_path = site.state / "sessions" / "alpha.json"
    rec = json.loads(rec_path.read_text())
    for key in ("name", "cwd", "model", "effort", "mcp", "prompt_file", "handoff", "parent",
                "children", "ts", "session_id", "job_id", "permission_mode", "platform",
                "launcher", "stopped_ts"):
        assert key in rec, key
    assert rec["name"] == "alpha" and rec["session_id"] == UUID1
    assert rec["job_id"] == UUID1[:8] and rec["stopped_ts"] is None
    prompt_file = site.state / "sessions" / "alpha.prompt.txt"
    assert rec["prompt_file"] == str(prompt_file)
    assert prompt_file.read_text() == PROMPT
    assert stat.S_IMODE(prompt_file.stat().st_mode) == 0o600
    audit_lines = (site.state / "sessions" / "audit.log").read_text().splitlines()
    assert len(audit_lines) == 1
    line = json.loads(audit_lines[0])
    assert line["action"] == "launch" and line["name"] == "alpha" and line["session_id"] == UUID1
    assert line["ok"] is True and "caller" in line and "ts" in line
    assert line["argv"][-1] == f"<{len(PROMPT)} chars>"
    assert PROMPT not in audit_lines[0]


def test_id_from_full_uuid(site):
    run = FakeRun(stdout="backgrounded · 88888888 · alpha\nsession " + UUID1 + " ready")
    s = make(site, run)
    res = go(site, s)
    assert res["session_id"] == UUID1
    assert len(run.calls) == 1  # full UUID wins; no claude agents lookup


def test_id_from_short_backgrounded(site):
    run = FakeRun(stdout="backgrounded · " + SHORT + " · alpha", agents_rows=[
        {"id": SHORT, "sessionId": UUID1, "name": "alpha", "kind": "background", "startedAt": 1_000_000}])
    s = make(site, run)
    res = go(site, s)
    assert res["session_id"] == UUID1
    assert any(a[:3] == ["claude", "agents", "--json"] for a, _ in run.calls)


def test_id_from_name_fallback_ignores_stale(site):
    stale = {"id": "s", "sessionId": UUID1, "name": "alpha", "kind": "background",
             "startedAt": 1_000_000 - 60_000}
    wrong_kind = {"id": "k", "sessionId": UUID3, "name": "alpha", "kind": "foreground",
                  "startedAt": 1_000_000}
    fresh = {"id": "f", "sessionId": UUID2, "name": "alpha", "kind": "background",
             "startedAt": 1_000_000}
    run = FakeRun(stdout="done\n", agents_rows=[stale, wrong_kind, fresh])
    s = make(site, run)
    res = go(site, s)
    assert res["session_id"] == UUID2  # newest fresh background row, never the stale one
    # stale-only: nothing resolves and the record keeps session_id null
    run2 = FakeRun(stdout="done\n", agents_rows=[stale])
    s2 = make(site, run2, resolve_budget=0.0)
    res2 = go(site, s2)
    assert res2["session_id"] is None and "warning" in res2
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] is None


def test_stop_calls_claude_stop_never_rm(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run, now=lambda: 2000.0)
    go(site, s)
    res = s.stop(name="alpha")
    assert res["ok"] is True and res["session_id"] == UUID1
    stop_calls = [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert stop_calls == [["claude", "stop", UUID1]]
    for argv, _ in run.calls:
        assert "rm" not in argv and "kill" not in argv and "-9" not in argv
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["stopped_ts"] == 2000.0


def test_stop_without_id_refuses(site):
    run = FakeRun(stdout="done\n")  # no id anywhere; claude agents returns nothing
    s = make(site, run, resolve_budget=0.0)
    res = go(site, s)
    assert res["session_id"] is None
    with pytest.raises(ParameterRefused, match="no session id"):
        s.stop(name="alpha")
    with pytest.raises(ParameterRefused, match="unknown session"):
        s.stop(name="ghost")
    with pytest.raises(ParameterRefused, match="name or id"):
        s.stop()
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]


def test_relaunch_keeps_name_and_uses_handoff(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha")
    assert res["name"] == "alpha" and res["session_id"] == UUID2
    launches = run.launch_calls()
    assert len(launches) == 2
    assert launches[1][-1] == f"Restart from handoff {hf.resolve()}.\n\n" + PROMPT
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["name"] == "alpha" and rec["session_id"] == UUID2
    assert rec["handoff"] == str(hf.resolve()) and rec["children"] == []
    assert not (site.state / "sessions" / "alpha.ready.json").exists()
    actions = [json.loads(l)["action"] for l in
               (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert actions == ["launch", "mark_ready", "relaunch"]
    res2 = s.relaunch("alpha", force=True)  # force works without a fresh marker
    assert res2["name"] == "alpha" and len(run.launch_calls()) == 3


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


def test_relaunch_stop_failure_launches_nothing(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    run.stop_rc = 1
    with pytest.raises(ParameterRefused, match="stop failed"):
        s.relaunch("alpha")
    assert len(run.launch_calls()) == 1  # only the original launch


def test_list_reports_context_tokens(site):
    run = FakeRun(stdout="session " + UUID1, agents_rows=[
        {"id": UUID1[:8], "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "running", "startedAt": 1_000_000}])
    s = make(site, run)
    go(site, s)
    tdir = site.home / ".claude" / "projects" / re.sub(r"[/.]", "-", str(site.proj.resolve()))
    tdir.mkdir(parents=True)
    usage = lambda n: json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": n, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}}})
    (tdir / f"{UUID1}.jsonl").write_text(
        '{"type":"user","message":{}}\n' + usage(100) + "\n" + usage(350) + "\n")
    out = s.list()
    row = out["sessions"][0]
    assert row["name"] == "alpha" and row["id"] == UUID1
    assert row["cwd"] == str(site.proj.resolve())
    assert row["context_tokens"] == 350 + 10 + 5
    assert row["context_pct"] == pytest.approx(365 / 300000)
    assert row["state"] == "running" and row["ready"] is False
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    assert s.list()["sessions"][0]["ready"] is True


def test_audit_and_record_hold_no_secret(site, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test-xyz")
    prompt = "ship the fix; secret sk-test-xyz must stay out of logs"
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s, prompt=prompt)
    audit_text = (site.state / "sessions" / "audit.log").read_text()
    rec_text = (site.state / "sessions" / "alpha.json").read_text()
    assert "sk-test-xyz" not in audit_text  # env value never logged
    assert prompt not in audit_text         # prompt text never logged
    assert "sk-test-xyz" not in rec_text    # env value never recorded
    assert "sk-test-xyz" in (site.state / "sessions" / "alpha.prompt.txt").read_text()


def test_mcp_tools_listed_and_dispatch(site, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer, TOOLS_METADATA
    tools = {t["name"]: t for t in TOOLS_METADATA}
    for name in ("plangraph_session_launch", "plangraph_session_stop", "plangraph_session_relaunch",
                 "plangraph_session_mark_ready", "plangraph_session_list"):
        assert name in tools and tools[name]["inputSchema"]["type"] == "object"
    props = tools["plangraph_session_launch"]["inputSchema"]["properties"]
    for key in ("name", "cwd", "model", "effort", "prompt", "prompt_file", "mcp_config",
                "handoff", "parent", "permission_mode", "dry_run"):
        assert key in props
    assert set(tools["plangraph_session_stop"]["inputSchema"]["properties"]) == {"name", "id"}
    assert set(tools["plangraph_session_relaunch"]["inputSchema"]["properties"]) == {"name", "handoff", "force"}
    assert set(tools["plangraph_session_mark_ready"]["inputSchema"]["properties"]) == {"name", "handoff", "card"}
    assert tools["plangraph_session_list"]["inputSchema"]["properties"] == {}
    run, pop = FakeRun(stdout="session " + UUID1), FakePopen()
    server = MCPSpawnerServer(state=site.state, session_run=run, session_popen=pop)
    monkeypatch.setenv("PLANGRAPH_SESSIONS_CONFIG", str(site.cfg))
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")

    async def go_rpc():
        res = await server.call_tool("plangraph_session_launch", {
            "name": "alpha", "cwd": str(site.proj), "model": MODEL, "effort": EFFORT,
            "mcp_config": str(site.mcp), "permission_mode": MODE, "prompt": PROMPT, "dry_run": False})
        assert res["session_id"] == UUID1
        assert (site.state / "sessions" / "alpha.json").is_file()  # state root = service.state
        assert (await server.call_tool("plangraph_session_mark_ready",
                {"name": "alpha", "handoff": str(hf)}))["ok"] is True
        assert (await server.call_tool("plangraph_session_list", {}))["sessions"][0]["name"] == "alpha"
        rel = await server.call_tool("plangraph_session_relaunch", {"name": "alpha", "force": True})
        assert rel["name"] == "alpha"
        assert (await server.call_tool("plangraph_session_stop", {"name": "alpha"}))["ok"] is True
        refused = await server.handle_rpc_request({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "plangraph_session_launch", "arguments": {
                "name": "bad name", "cwd": str(site.proj), "model": MODEL, "effort": EFFORT,
                "mcp_config": str(site.mcp), "permission_mode": MODE, "prompt": "x", "dry_run": True}}})
        assert refused["error"]["code"] == -32602

    asyncio.run(go_rpc())
