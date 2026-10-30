"""tests/test_spawner_sessions.py — SESSION-SPAWN-MCP session lifecycle.

Every subprocess goes through one injectable runner; the fakes assert argv is a
list and that a shell is never involved. No real `claude` call is made.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
import time
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
        if self._is_agents(argv):
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(self.agents_rows), stderr="")
        if self._is_stop(argv):
            return types.SimpleNamespace(returncode=self.stop_rc, stdout="", stderr="")
        return types.SimpleNamespace(returncode=self.launch_rc, stdout=self.stdout, stderr="")

    # Dispatch on the tail and accept both the bare name and the resolved
    # win32 .exe head, so the fakes model what production really runs.
    @staticmethod
    def _is_agents(argv):
        return Path(argv[0]).name in ("claude", "claude.exe") and argv[1:3] == ["agents", "--json"]

    @staticmethod
    def _is_stop(argv):
        return Path(argv[0]).name in ("claude", "claude.exe") and argv[1:2] == ["stop"]

    def launch_calls(self):
        return [argv for argv, _ in self.calls if argv[:2] == ["claude", "--bg"]]


def write_config(path, text):
    """Write a config file pinned owner-only (0600).

    The sessions loader refuses group/world-writable configs; under a umask
    like 002 plain write_text leaves the file group-writable and every action
    would be refused, so the mode is set explicitly after writing."""
    path.write_text(text)
    path.chmod(0o600)
    return path


@pytest.fixture
def site(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "roots"
    proj = root / "proj"
    proj.mkdir(parents=True)
    mcp = tmp_path / "mcp.json"
    write_config(mcp, "{}")
    cfg = tmp_path / "sessions.json"
    # tmp_path is admitted via cwd_roots (file_roots is a cut config key);
    # the tests keep writing their handoff/prompt fixtures under tmp_path.
    write_config(cfg, json.dumps({
        "cwd_roots": [str(root), str(tmp_path)], "models": [MODEL], "efforts": [EFFORT],
        "permission_modes": [MODE], "mcp_configs": [str(mcp)], "ctx_cap_tokens": 300000}))
    return types.SimpleNamespace(tmp_path=tmp_path, home=home, root=root, proj=proj,
                                 mcp=mcp, cfg=cfg, state=tmp_path / "state")


def make(site, run=None, *, platform="linux", now=lambda: 1000.0,
         resolve_budget=15.0, config_path=None, which=None, sleep=lambda _s: None):
    return Sessions(state_root=site.state, config_path=str(config_path or site.cfg),
                    run=run or FakeRun(),
                    now=now, platform=platform, resolve_budget=resolve_budget, which=which,
                    sleep=sleep)


def go(site, s, name="alpha", **over):
    args = dict(BASE, name=name, cwd=str(site.proj), mcp_config=str(site.mcp))
    args.update(over)
    return s.launch(**args)


def test_argv_linux_is_list_no_shell(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
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
    # style_text is appended via --append-system-prompt when configured
    cfg2 = site.tmp_path / "sessions2.json"
    write_config(cfg2, json.dumps({**json.loads(site.cfg.read_text()), "style_text": "be terse"}))
    s.stop(name="alpha")  # a live record may only be replaced via relaunch
    run2 = FakeRun(stdout="session " + UUID1)
    s2 = make(site, run2, config_path=cfg2)
    go(site, s2)
    # the old-id gate polls agents first (round 4): pick the launch argv
    argv2 = run2.launch_calls()[0]
    assert argv2[-3:] == ["--append-system-prompt", "be terse", PROMPT]


def test_argv_windows_bg_and_wt(site, monkeypatch):
    exe = str(site.tmp_path / "claude.exe")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run, platform="win32", which=lambda _: exe)
    go(site, s)
    argv, kw = run.calls[0]
    assert argv[:2] == [exe, "--bg"]  # the resolved .exe path is argv[0]
    assert kw["creationflags"] == DETACHED
    assert kw.get("shell") in (None, False)


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
    run = FakeRun()
    s = make(site, run)
    res = go(site, s, prompt="abcdefg", dry_run=True)
    assert set(res) == {"argv", "cwd", "env_keys"}
    assert res["argv"][-1] == "<7 chars>"
    assert all(isinstance(a, str) for a in res["argv"])
    assert "FLEET_SESSION_NAME" in res["env_keys"]
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in res["env_keys"]
    assert res["cwd"] == str(site.proj.resolve())
    assert run.calls == []
    sessions_dir = site.state / "sessions"
    with pytest.raises(ParameterRefused, match="model"):
        go(site, s, dry_run=True, model="gpt-4")  # a refusing dry run is still dry
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
                "stopped_ts"):
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
    s.stop(name="alpha")  # a live record may only be replaced via relaunch
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
    assert stop_calls == [["claude", "stop", UUID1]]  # exact argv, never rm/kill
    assert run.calls[-1][0] == ["claude", "stop", UUID1]  # the only subprocess call of the stop
    assert len(run.calls) == 2  # the earlier launch + exactly one stop call
    assert run.calls[-1][1]["timeout"] == 30  # bounded stop timeout
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
    # Claude maps every non-alphanumeric cwd char to "-"; spelled out here on
    # purpose so the test never reuses the implementation's regex.
    tdir = site.home / ".claude" / "projects" / "".join(ch if ch.isalnum() else "-" for ch in str(site.proj.resolve()))
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


def test_context_tokens_reads_only_last_256kb(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    tdir = site.home / ".claude" / "projects" / "".join(ch if ch.isalnum() else "-" for ch in str(site.proj.resolve()))
    tdir.mkdir(parents=True)
    usage = json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 900, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})
    filler = json.dumps({"type": "user", "message": {"content": "x" * 1024}}) + "\n"
    (tdir / f"{UUID1}.jsonl").write_text(usage + "\n" + filler * 300)  # > 256 KB past the usage
    row = s.list()["sessions"][0]
    assert row["context_tokens"] == 0  # the 900-token line is outside the tail window


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
    # the secret must reach a recorded field: the ready-marker card (this
    # assertion fails if _scrub is ever removed from the marker write)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf), card="card holding sk-test-xyz")
    marker_text = (site.state / "sessions" / "alpha.ready.json").read_text()
    assert "sk-test-xyz" not in marker_text
    assert "[autoos:redacted]" in marker_text
    # error text scrubbed before it reaches the caller
    with pytest.raises(ParameterRefused) as exc:
        go(site, s, mcp_config=str(site.tmp_path / "sk-test-xyz.json"))
    assert "sk-test-xyz" not in str(exc.value)
    # returned results carry no secret either: _shown length-redacts the style
    # text before it reaches the dry-run argv (SESSION-SPAWN-MCP)
    cfg2 = site.tmp_path / "sessions-secret.json"
    write_config(cfg2, json.dumps(
        {**json.loads(site.cfg.read_text()), "style_text": "be terse with sk-test-xyz"}))
    res = go(site, make(site, FakeRun(), config_path=cfg2), name="beta", prompt="abc", dry_run=True)
    assert "sk-test-xyz" not in json.dumps(res)
    assert f"<{len('be terse with sk-test-xyz')} chars>" in json.dumps(res)


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
    run = FakeRun(stdout="session " + UUID1)
    server = MCPSpawnerServer(state=site.state, session_run=run,
                              session_sleep=lambda _s: None)
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
        run.stdout = "session " + UUID2  # the new process reports a fresh id; the old one is excluded
        rel = await server.call_tool("plangraph_session_relaunch", {"name": "alpha", "force": True})
        assert rel["name"] == "alpha"
        assert (await server.call_tool("plangraph_session_stop", {"name": "alpha"}))["ok"] is True
        refused = await server.handle_rpc_request({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "plangraph_session_launch", "arguments": {
                "name": "bad name", "cwd": str(site.proj), "model": MODEL, "effort": EFFORT,
                "mcp_config": str(site.mcp), "permission_mode": MODE, "prompt": "x", "dry_run": True}}})
        assert refused["error"]["code"] == -32602

    asyncio.run(go_rpc())


def test_config_requires_existing_real_files(site):
    cfg = json.loads(site.cfg.read_text())
    bad = site.tmp_path / "sessions-bad.json"
    write_config(bad, json.dumps({**cfg, "mcp_configs": [str(site.tmp_path / "ghost-mcp.json")]}))
    s = make(site, FakeRun(), config_path=bad)
    with pytest.raises(ParameterRefused, match="config"):
        go(site, s)  # every configured entry must resolve to an existing regular file
    gone = site.tmp_path / "gone.json"
    write_config(gone, "{}")
    cfg2 = site.tmp_path / "sessions-gone.json"
    write_config(cfg2, json.dumps({**cfg, "mcp_configs": [str(gone)]}))
    s2 = make(site, FakeRun(), config_path=cfg2)
    gone.unlink()
    with pytest.raises(ParameterRefused, match="mcp_config"):
        go(site, s2, mcp_config=str(gone))  # requested entry checked again at launch time
    cfg3 = site.tmp_path / "sessions-dir.json"
    write_config(cfg3, json.dumps({**cfg, "mcp_configs": [str(site.tmp_path)]}))
    with pytest.raises(ParameterRefused, match="config"):
        go(site, make(site, FakeRun(), config_path=cfg3))  # a directory is not a regular file


def test_config_refuses_root_roots_and_bad_scalars(site):
    cfg = json.loads(site.cfg.read_text())

    def refused(over, match="config"):
        bad = site.tmp_path / "sessions-bad.json"
        write_config(bad, json.dumps({**cfg, **over}))
        with pytest.raises(ParameterRefused, match=match):
            go(site, make(site, FakeRun(), config_path=bad))

    refused({"cwd_roots": ["/"]})  # a filesystem root would allow any cwd
    refused({"cwd_roots": [str(site.mcp)]})  # a file is not a directory
    refused({"cwd_roots": [str(site.tmp_path / "ghost-dir")]})  # must exist
    refused({"file_roots": ["/"]})
    refused({"style_file": "style.txt"})  # relative style file refused
    refused({"style_file": str(site.tmp_path / "ghost-style.txt")})
    refused({"compact_window": "33x"})
    refused({"compact_window": 330000})
    refused({"ctx_cap_tokens": 0})
    refused({"ctx_cap_tokens": "300000"})
    run = FakeRun()
    s = make(site, run)
    with pytest.raises(ParameterRefused, match="dry_run"):
        go(site, s, dry_run=1)  # dry_run must be a real bool
    with pytest.raises(ParameterRefused, match="force"):
        s.relaunch("alpha", force=1)  # force must be a real bool
    assert not run.calls


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX config ownership model")
def test_config_trust_refuses_loose_permissions(site, monkeypatch):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    site.cfg.chmod(0o666)  # world-writable config: not ours to trust
    with pytest.raises(ParameterRefused, match="config"):
        go(site, s)
    site.cfg.chmod(0o664)  # group-writable refused too
    with pytest.raises(ParameterRefused, match="config"):
        go(site, s)
    site.cfg.chmod(0o600)
    go(site, s)  # owner-only config launches fine
    st = site.cfg.stat()
    monkeypatch.setattr(os, "getuid", lambda: st.st_uid + 1)  # foreign owner refused
    with pytest.raises(ParameterRefused, match="config"):
        go(site, s, name="beta")


def test_config_symlink_refused(site):
    alias = site.tmp_path / "alias-cfg2.json"
    alias.symlink_to(site.cfg)
    with pytest.raises(ParameterRefused, match="symlink"):
        go(site, make(site, FakeRun(), config_path=alias))  # O_NOFOLLOW refuses the alias


def test_config_mode_0640_refused(site):
    site.cfg.chmod(0o640)  # group-readable: any group/other bit refuses
    try:
        with pytest.raises(ParameterRefused, match="0600"):
            go(site, make(site, FakeRun()))
    finally:
        site.cfg.chmod(0o600)


def test_config_owned_by_other_uid_refused(site, monkeypatch):
    # A non-root test cannot chown the config, so falsify the owner its fd
    # reports to fstat: the check must notice the file is not ours.
    ino = os.stat(site.cfg).st_ino
    real_fstat = os.fstat

    def lying_fstat(fd, *args, **kwargs):
        st = real_fstat(fd, *args, **kwargs)
        if st.st_ino == ino:
            return os.stat_result((st[0], st[1], st[2], st[3], 12345, st[5], st[6], st[7], st[8], st[9]))
        return st

    monkeypatch.setattr(os, "fstat", lying_fstat)
    with pytest.raises(ParameterRefused, match="owned"):
        go(site, make(site, FakeRun()))


def test_config_read_without_no_follow_refused(site, monkeypatch):
    monkeypatch.delattr(os, "O_NOFOLLOW")  # no safe open possible: refuse, never fall back
    with pytest.raises(ParameterRefused, match="O_NOFOLLOW"):
        go(site, make(site, FakeRun()))


def test_config_style_text_non_string_refused(site):
    for bad in (5, None, "", ["be terse"]):
        bad_cfg = site.tmp_path / "sessions-style-bad.json"
        write_config(bad_cfg, json.dumps({**json.loads(site.cfg.read_text()), "style_text": bad}))
        with pytest.raises(ParameterRefused, match="style_text"):
            go(site, make(site, FakeRun(), config_path=bad_cfg))


def test_config_refuses_old_keys_as_unknown(site):
    for key, value in (("style_file", "style.txt"), ("file_roots", [str(site.tmp_path)]),
                       ("windows_launcher", "wt")):
        old_cfg = site.tmp_path / "sessions-old-key.json"
        write_config(old_cfg, json.dumps({**json.loads(site.cfg.read_text()), key: value}))
        with pytest.raises(ParameterRefused, match="unknown"):
            go(site, make(site, FakeRun(), config_path=old_cfg))


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
        s.relaunch("alpha", handoff=str(outside_handoff), force=True)


def test_audit_on_refusals_name_truncated(site):
    run = FakeRun()
    s = make(site, run)
    with pytest.raises(ParameterRefused, match="name"):
        go(site, s, name="x" * 100)
    with pytest.raises(ParameterRefused, match="unknown session"):
        s.stop(name="ghost")
    with pytest.raises(ParameterRefused, match="handoff"):
        s.mark_ready("alpha", str(site.tmp_path / "ghost-handoff.md"))
    with pytest.raises(ParameterRefused, match="unknown session"):
        s.relaunch("ghost", force=True)
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert [l["action"] for l in lines] == ["launch", "stop", "mark_ready", "relaunch"]
    assert all(l["ok"] is False and l["reason"] for l in lines)
    assert lines[0]["name"] == "x" * 80  # name as given, truncated to 80 chars
    assert not run.calls


def test_mcp_session_dispatch_rejects_unknown_and_bad_args(site, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer, SESSION_TOOLS
    tools = {t["name"]: t for t in SESSION_TOOLS}
    assert tools["plangraph_session_launch"]["inputSchema"]["required"] == \
        ["name", "cwd", "model", "effort", "mcp_config", "permission_mode"]
    assert tools["plangraph_session_stop"]["inputSchema"].get("required", []) == []
    assert tools["plangraph_session_relaunch"]["inputSchema"]["required"] == ["name"]
    assert tools["plangraph_session_mark_ready"]["inputSchema"]["required"] == ["name", "handoff"]
    server = MCPSpawnerServer(state=site.state, session_run=FakeRun())
    monkeypatch.setenv("PLANGRAPH_SESSIONS_CONFIG", str(site.cfg))

    async def go_rpc():
        with pytest.raises(ValueError):
            await server.call_tool("plangraph_session_bogus", {})  # unknown session tool
        with pytest.raises(ParameterRefused):
            await server.call_tool("plangraph_session_relaunch", {})  # never KeyError
        with pytest.raises(ParameterRefused):
            await server.call_tool("plangraph_session_launch", {
                "name": "alpha", "cwd": 5, "model": MODEL, "effort": EFFORT,
                "mcp_config": str(site.mcp), "permission_mode": MODE, "prompt": PROMPT})
        with pytest.raises(ParameterRefused):
            await server.call_tool("plangraph_session_launch", {
                "name": "alpha", "cwd": str(site.proj), "model": MODEL, "effort": EFFORT,
                "mcp_config": str(site.mcp), "permission_mode": MODE, "prompt": PROMPT,
                "dry_run": "yes"})  # never TypeError
        with pytest.raises(ParameterRefused):
            await server.call_tool("plangraph_session_mark_ready", {"name": "alpha"})
        with pytest.raises(ParameterRefused):
            await server.call_tool("plangraph_session_stop", {"name": 7})

    asyncio.run(go_rpc())


def test_mcp_session_calls_never_block_the_loop(site, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    main_ident = threading.get_ident()
    seen = {}

    def run(argv, **kw):
        seen["ident"] = threading.get_ident()
        return types.SimpleNamespace(returncode=0, stdout=UUID1, stderr="")

    server = MCPSpawnerServer(state=site.state, session_run=run)
    monkeypatch.setenv("PLANGRAPH_SESSIONS_CONFIG", str(site.cfg))

    async def go_rpc():
        res = await server.call_tool("plangraph_session_launch", {
            "name": "alpha", "cwd": str(site.proj), "model": MODEL, "effort": EFFORT,
            "mcp_config": str(site.mcp), "permission_mode": MODE, "prompt": PROMPT})
        assert res["session_id"] == UUID1

    asyncio.run(go_rpc())
    assert seen["ident"] != main_ident  # Sessions ran via asyncio.to_thread


def test_transcript_path_maps_every_non_alnum(site):
    from plangraph.spawner.sessions import transcript_path
    p = transcript_path("C:\\Users\\x\\my_proj", UUID1)
    assert p.parent.name == "C--Users-x-my-proj"
    assert p.name == UUID1 + ".jsonl"
    p2 = transcript_path("/srv/u/my_proj", UUID1)
    assert p2.parent.name == "-srv-u-my-proj"


def test_windows_which_resolution(site):
    exe = str(site.tmp_path / "claude.exe")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run, platform="win32", which=lambda _: exe)
    go(site, s)
    assert run.calls[0][0][0] == exe  # the full .exe path is argv[0]
    run2 = FakeRun(stdout="session " + UUID1)
    s2 = make(site, run2, platform="win32", which=lambda _: exe)
    go(site, s2, name="beta", prompt="do beta")
    # a .cmd/.bat shim would let cmd.exe parse the prompt: refused
    s3 = make(site, FakeRun(), platform="win32", which=lambda _: str(site.tmp_path / "claude.cmd"))
    with pytest.raises(ParameterRefused, match="exe"):
        go(site, s3, name="gamma", prompt="do gamma")
    s4 = make(site, FakeRun(), platform="win32", which=lambda _: None)
    with pytest.raises(ParameterRefused, match="claude"):
        go(site, s4, name="delta", prompt="do delta")
    # non-win32 never consults which
    calls = []
    s5 = make(site, FakeRun(stdout="session " + UUID1), which=lambda n: calls.append(n) or "/x/claude")
    go(site, s5, name="eps", prompt="do eps")
    assert calls == []


def test_win32_stop_argv_uses_resolved_exe(site):
    exe = str(site.tmp_path / "claude.exe")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run, platform="win32", which=lambda _: exe)
    go(site, s)
    res = s.stop(name="alpha")
    assert res["ok"] is True
    stops = [a for a, _ in run.calls if a[1:2] == ["stop"]]
    assert stops == [[exe, "stop", UUID1]]  # the resolved .exe, never bare "claude"


def test_resolve_short_id_miss_falls_back_and_excludes(site):
    row_new = {"id": "f", "sessionId": UUID2, "name": "alpha", "kind": "background", "startedAt": 1_000_000}
    # a short-id miss falls back to name + startedAt
    run = FakeRun(stdout="backgrounded · 00000000 · alpha", agents_rows=[row_new])
    res = go(site, make(site, run))
    assert res["session_id"] == UUID2
    # relaunch excludes the previous session id from every lookup
    run2 = FakeRun(stdout="session " + UUID1)
    s2 = make(site, run2, resolve_budget=0.0)
    go(site, s2, name="beta", prompt="do beta")
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s2.mark_ready("beta", str(hf))
    run2.stdout = "done\n"
    run2.agents_rows = [{"id": "x", "sessionId": UUID1, "name": "beta", "kind": "background",
                         "startedAt": 2_000_000}]
    res2 = s2.relaunch("beta", force=True)
    assert res2["session_id"] is None and "warning" in res2  # only the excluded row matched


def test_timeout_expired_is_audited_failure(site):
    class TimeoutRun(FakeRun):
        def __call__(self, argv, **kw):
            self.calls.append((list(argv), kw))
            if argv[:2] == ["claude", "--bg"]:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
            return super().__call__(argv, **kw)

    run = TimeoutRun()
    s = make(site, run)
    with pytest.raises(subprocess.TimeoutExpired):
        go(site, s)
    assert [kw for argv, kw in run.calls if argv[:2] == ["claude", "--bg"]][0]["timeout"] == 60
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["action"] == "launch" and lines[0]["ok"] is False

    class StopTimeout(FakeRun):
        def __call__(self, argv, **kw):
            self.calls.append((list(argv), kw))
            if argv[:2] == ["claude", "stop"]:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
            return super().__call__(argv, **kw)

    run2 = StopTimeout(stdout="session " + UUID1)
    s2 = make(site, run2)
    go(site, s2, name="beta", prompt="do beta")
    with pytest.raises(subprocess.TimeoutExpired):
        s2.stop(name="beta")
    lines2 = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert lines2[-1]["action"] == "stop" and lines2[-1]["ok"] is False


def test_relaunch_force_without_handoff_launches_bare(site):
    hf = site.tmp_path / "handoff.md"
    hf.write_text("state at stop")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s, handoff=str(hf))
    assert run.launch_calls()[0][-1].startswith("Restart from handoff")  # first launch had one
    res = s.relaunch("alpha", force=True)  # no marker, no handoff argument
    assert res["ok"] is True
    assert run.launch_calls()[-1][-1] == PROMPT  # bare: never the record's old handoff


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


def test_relaunch_stale_handoff_ok_under_force(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run, now=lambda: time.time())
    go(site, s)
    stale = site.tmp_path / "stale.md"
    stale.write_text("old state")
    os.utime(stale, (1_000_000, 1_000_000))
    res = s.relaunch("alpha", handoff=str(stale), force=True)  # force waives freshness
    assert res["ok"] is True
    assert run.launch_calls()[-1][-1] == f"Restart from handoff {stale.resolve()}.\n\n" + PROMPT


def test_relaunch_marker_handoff_not_string_refuses(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    marker = site.state / "sessions" / "alpha.ready.json"
    marker.write_text(json.dumps({"handoff": 1234, "card": None, "ts": 1000.0}))
    with pytest.raises(ParameterRefused, match="handoff"):
        s.relaunch("alpha")
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]


def test_relaunch_validation_failure_stops_nothing(site, tmp_path_factory):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    stray = tmp_path_factory.mktemp("stray3")
    bad = stray / "handoff.md"
    bad.write_text("state")
    with pytest.raises(ParameterRefused, match="handoff"):
        s.relaunch("alpha", handoff=str(bad), force=True)  # handoff outside the roots
    # a stored spec that no longer validates: nothing is stopped either
    (site.state / "sessions" / "alpha.prompt.txt").unlink()
    hf = site.tmp_path / "handoff.md"
    hf.write_text("state")
    s.mark_ready("alpha", str(hf))
    with pytest.raises(ParameterRefused, match="prompt_file"):
        s.relaunch("alpha")
    assert not [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]  # nothing was stopped


def test_relaunch_launch_failure_after_stop_keeps_record(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    run.launch_rc = 1  # the relaunch's claude exits 1 after the old one is stopped
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha")
    assert res["ok"] is False and res["session_id"] is None
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] == UUID1 and rec["stopped_ts"] is not None  # record kept, stopped
    assert (site.state / "sessions" / "alpha.ready.json").exists()  # a retry can relaunch
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert lines[-1]["action"] == "relaunch" and lines[-1]["ok"] is False
    run.launch_rc = 0
    retry = s.relaunch("alpha")  # marker survived: a plain retry works
    assert retry["ok"] is True and retry["session_id"] == UUID2


def _ready(site, run, s):
    """Launch alpha, mark it ready, hand back the marker + record paths."""
    go(site, s)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    return (site.state / "sessions" / "alpha.json"), (site.state / "sessions" / "alpha.ready.json")


def _assert_old_record_kept(rec_path, marker_path, site):
    rec = json.loads(rec_path.read_text())
    assert rec["session_id"] == UUID1 and rec["stopped_ts"] is not None
    assert marker_path.exists()  # a retry can relaunch
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "relaunch" and line["ok"] is False


def _stopped(site, run, s):
    """Launch alpha, mark it ready, stop it: the record keeps its session id
    with stopped_ts set - the state a retrying relaunch finds."""
    rec_path, marker_path = _ready(site, run, s)
    s.stop(name="alpha")
    return rec_path, marker_path


@pytest.mark.parametrize("state", ["running", "idle", None])
def test_relaunch_already_stopped_live_row_is_stopped_first(site, state):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    _stopped(site, run, s)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",
                        "kind": "background", "state": state, "startedAt": 1_000_000}]
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha")
    assert res["ok"] is True and res["session_id"] == UUID2
    kinds = [a[:2] for a, _ in run.calls]
    # the old session is looked up in claude agents and stopped BEFORE the relaunch
    assert kinds == [["claude", "--bg"], ["claude", "stop"], ["claude", "agents"],
                     ["claude", "stop"], ["claude", "--bg"]]
    assert [a for a, _ in run.calls if a[:2] == ["claude", "stop"]] == \
        [["claude", "stop", UUID1], ["claude", "stop", UUID1]]  # exact argv, never rm/kill
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] == UUID2 and rec["stopped_ts"] is None


def test_relaunch_already_stopped_stop_failure_launches_nothing(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    rec_path, marker_path = _stopped(site, run, s)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",
                        "kind": "background", "state": "running", "startedAt": 1_000_000}]
    run.stop_rc = 1
    with pytest.raises(ParameterRefused, match="stop failed"):
        s.relaunch("alpha")
    assert len(run.launch_calls()) == 1  # nothing was relaunched
    rec = json.loads(rec_path.read_text())
    assert rec["session_id"] == UUID1 and rec["stopped_ts"] is not None
    assert marker_path.exists()  # a retry can relaunch


def test_relaunch_already_stopped_unlisted_id_proceeds_without_stop(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    _stopped(site, run, s)
    run.agents_rows = [{"id": "r", "sessionId": UUID3, "name": "alpha",
                        "kind": "background", "state": "running", "startedAt": 1_000_000}]
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha")
    assert res["ok"] is True and res["session_id"] == UUID2
    stops = [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert stops == [["claude", "stop", UUID1]]  # only the earlier user stop
    assert any(a[:3] == ["claude", "agents", "--json"] for a, _ in run.calls)  # id was looked up
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] == UUID2 and rec["stopped_ts"] is None


@pytest.mark.parametrize("state", ["finished", "stopped"])
def test_relaunch_already_stopped_finished_row_proceeds_without_stop(site, state):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    _stopped(site, run, s)
    run.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "alpha",
                        "kind": "background", "state": state, "startedAt": 1_000_000}]
    run.stdout = "session " + UUID2
    res = s.relaunch("alpha")
    assert res["ok"] is True and res["session_id"] == UUID2
    stops = [a for a, _ in run.calls if a[:2] == ["claude", "stop"]]
    assert stops == [["claude", "stop", UUID1]]  # already over: no second stop
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] == UUID2 and rec["stopped_ts"] is None


def test_relaunch_timeout_keeps_record_and_marker(site):
    class TimeoutRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"] and self.broken:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            return super().__call__(argv, **kw)

    run = TimeoutRun(stdout="session " + UUID1)
    run.broken = False
    s = make(site, run)
    rec_path, marker_path = _ready(site, run, s)
    run.stdout = "session " + UUID2
    run.broken = True
    with pytest.raises(subprocess.TimeoutExpired):
        s.relaunch("alpha")
    _assert_old_record_kept(rec_path, marker_path, site)


def test_relaunch_oserror_keeps_record_and_marker(site):
    class OsErrRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"] and self.broken:
                raise OSError("exec format error")
            return super().__call__(argv, **kw)

    run = OsErrRun(stdout="session " + UUID1)
    run.broken = False
    s = make(site, run)
    rec_path, marker_path = _ready(site, run, s)
    run.stdout = "session " + UUID2
    run.broken = True
    res = s.relaunch("alpha")
    assert res["ok"] is False
    _assert_old_record_kept(rec_path, marker_path, site)


def test_relaunch_record_write_failure_keeps_record_and_marker(site, monkeypatch):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    rec_path, marker_path = _ready(site, run, s)
    run.stdout = "session " + UUID2
    real = s._write_record

    def flaky(name, rec):
        if rec.get("session_id") == UUID2:  # only the NEW record's write fails
            raise OSError("no space left on device")
        return real(name, rec)

    monkeypatch.setattr(s, "_write_record", flaky)
    res = s.relaunch("alpha")
    assert res["ok"] is False
    _assert_old_record_kept(rec_path, marker_path, site)


def test_audit_caller_is_principal_or_user(site, monkeypatch):
    import getpass
    monkeypatch.setenv("PLANGRAPH_PRINCIPAL", "operator-7")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["caller"] == "operator-7"  # the principal wins
    monkeypatch.delenv("PLANGRAPH_PRINCIPAL")
    monkeypatch.setattr(getpass, "getuser", lambda: "os-user")
    go(site, s, name="beta", prompt="do beta")
    line2 = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line2["caller"] == "os-user"  # else the OS user, never a source location
    assert ".py" not in line2["caller"] and "sessions.py" not in line["caller"]


def test_ok_false_audit_includes_redacted_argv(site):
    secret_prompt = "secret prompt text"
    run = FakeRun(stdout="session " + UUID1, launch_rc=1)
    s = make(site, run)
    res = go(site, s, prompt=secret_prompt)
    assert res["ok"] is False
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["ok"] is False
    assert line["argv"][-1] == f"<{len(secret_prompt)} chars>"  # prompt-redacted argv
    assert secret_prompt not in json.dumps(line)


def test_exit_failure_reason_carries_claude_output(site):
    run = FakeRun(stdout="Workspace not trusted. Run `claude` in /x once and accept the trust prompt",
                  launch_rc=1)
    s = make(site, run)
    res = go(site, s)
    assert res["ok"] is False and res["session_id"] is None
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["ok"] is False
    assert line["reason"].startswith("claude exited 1: ")  # rc first, then the output
    assert "Workspace not trusted" in line["reason"]  # why claude refused, in the audit


def test_docs_context_pct_fraction(site):
    text = (Path(__file__).resolve().parents[1] / "docs" / "SPAWNER.md").read_text(encoding="utf-8")
    section = text.split("## Claude Code sessions", 1)[1]
    assert "context_pct" in section and "fraction" in section and "0..1" in section


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


def test_relaunch_null_session_id_needs_force(site):
    run = FakeRun(stdout="done\n")  # nothing resolvable: record session_id is null
    s = make(site, run, resolve_budget=0.0)
    res = go(site, s)
    assert res["session_id"] is None
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    with pytest.raises(ParameterRefused, match="session id"):
        s.relaunch("alpha")  # null id refuses unless forced
    res2 = s.relaunch("alpha", force=True)  # forced: continues with a warning
    assert res2["ok"] is True and "warning" in res2


def test_launched_ms_taken_before_start(site):
    clock = {"n": 0}

    def now():
        clock["n"] += 1
        return 1000.0 if clock["n"] == 1 else 3000.0

    row = {"id": "f", "sessionId": UUID2, "name": "alpha", "kind": "background",
           "startedAt": 1_001_000}  # just after the launch, far before the late clock
    run = FakeRun(stdout="done\n", agents_rows=[row])
    s = make(site, run, now=now)
    res = go(site, s)
    assert res["session_id"] == UUID2


def test_resolve_polls_every_half_second_until_budget(site, monkeypatch):
    class LateRows(FakeRun):
        def __init__(self, row_name="alpha"):
            super().__init__(stdout="done\n", agents_rows=[])
            self.agents_calls = 0
            self.row_name = row_name

        def __call__(self, argv, **kw):
            if self._is_agents(argv):
                self.agents_calls += 1
                if self.agents_calls >= 3:
                    self.agents_rows = [{"id": "f", "sessionId": UUID2, "name": self.row_name,
                                         "kind": "background", "startedAt": 1_000_000}]
            return super().__call__(argv, **kw)

    sleeps = []
    run = LateRows()
    s = make(site, run, sleep=sleeps.append)
    res = go(site, s)
    assert res["session_id"] == UUID2  # a row appearing late is still found
    assert run.agents_calls == 3 and sleeps[:2] == [0.5, 0.5]


def test_excluded_id_rejected_for_full_uuid_short_and_fallback(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)  # alpha at UUID1
    run.agents_rows = [{"id": "f", "sessionId": UUID2, "name": "alpha", "kind": "background",
                        "startedAt": 1_000_000}]
    res = s.relaunch("alpha", force=True)
    assert res["session_id"] == UUID2  # the old full UUID in stdout is not the new id
    # a short id row carrying the excluded sessionId is rejected too
    run.agents_rows = [
        {"id": SHORT, "sessionId": UUID2, "name": "alpha", "kind": "background", "startedAt": 1_000_000},
        {"id": "g", "sessionId": UUID3, "name": "alpha", "kind": "background", "startedAt": 1_000_000}]
    run.stdout = "backgrounded · " + SHORT + " · alpha"
    res2 = s.relaunch("alpha", force=True)
    assert res2["session_id"] == UUID3


def test_short_id_matches_row_id_or_session_id_prefix(site):
    row = {"id": "other", "sessionId": SHORT + "-1234-1234-1234-123456789abc",
           "name": "alpha", "kind": "background", "startedAt": 0}
    run = FakeRun(stdout="backgrounded · " + SHORT + " · alpha", agents_rows=[row])
    res = go(site, make(site, run))
    assert res["session_id"] == row["sessionId"]  # sessionId prefix matches the short id


def test_short_id_match_requires_launched_name_and_background_kind(site):
    # a short id may claim only the launched name's own background row: a
    # wrong name, a wrong kind and the excluded id are all rejected
    run = FakeRun(stdout="backgrounded · " + SHORT + " · alpha", agents_rows=[
        {"id": SHORT, "sessionId": UUID2, "name": "beta", "kind": "background", "startedAt": 0}])
    res = go(site, make(site, run))
    assert res["session_id"] is None  # the row name is not the launched name
    run2 = FakeRun(stdout="backgrounded · " + SHORT + " · beta", agents_rows=[
        {"id": SHORT, "sessionId": UUID2, "name": "beta", "kind": "foreground", "startedAt": 0}])
    res2 = go(site, make(site, run2), name="beta", prompt="do beta")
    assert res2["session_id"] is None  # only kind background may be claimed
    run3 = FakeRun(stdout="backgrounded · " + SHORT + " · gamma", agents_rows=[
        {"id": SHORT, "sessionId": UUID2, "name": "gamma", "kind": "background", "startedAt": 0}])
    res3 = go(site, make(site, run3), name="gamma", prompt="do gamma")
    assert res3["session_id"] == UUID2  # name + kind match: the short id resolves
    run4 = FakeRun(stdout="session " + UUID1)
    s4 = make(site, run4)
    go(site, s4, name="delta", prompt="do delta")  # delta at UUID1
    run4.agents_rows = [
        {"id": SHORT, "sessionId": UUID1, "name": "delta", "kind": "background", "startedAt": 0},
        {"id": "g", "sessionId": UUID3, "name": "delta", "kind": "background", "startedAt": 2_000_000}]
    run4.stdout = "backgrounded · " + SHORT + " · delta"
    res4 = s4.relaunch("delta", force=True)
    assert res4["session_id"] == UUID3  # the excluded id stays rejected despite name + kind


def test_unexpected_exception_is_audited_and_refused(site, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test-xyz")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)

    def boom(*_a, **_kw):
        raise RuntimeError("backend exploded holding sk-test-xyz")

    monkeypatch.setattr(s, "_check_cwd", boom)
    with pytest.raises(ParameterRefused) as exc:
        go(site, s)
    msg = str(exc.value)
    assert "RuntimeError" in msg and "backend exploded" in msg
    assert "sk-test-xyz" not in msg  # scrubbed before it reaches the caller
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "launch" and line["ok"] is False
    assert line["reason"] == msg  # type + scrubbed message, same text as the refusal
    assert "sk-test-xyz" not in json.dumps(line)


def test_audit_write_failure_after_success_warns(site, monkeypatch):
    import plangraph.spawner.sessions as mod
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)

    def dead(*_a, **_kw):
        raise OSError("audit disk full")

    monkeypatch.setattr(mod, "append_private", dead)
    res = go(site, s)
    assert res["ok"] is True and res["session_id"] == UUID1  # the launch succeeded
    assert "audit" in res.get("warning", "")  # the caller is warned, not failed


def test_scrub_touches_only_free_text_fields(site, monkeypatch):
    from plangraph.spawner.sessions import _scrub
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test-xyz")
    out = _scrub({
        "name": "sk-test-xyz", "cwd": "/srv/u/sk-test-xyz", "session_id": "sk-test-xyz",
        "prompt_file": "/tmp/sk-test-xyz.txt", "handoff": "/tmp/sk-test-xyz.md",
        "mcp": "/tmp/sk-test-xyz.json",
        "reason": "r sk-test-xyz", "error": "e sk-test-xyz", "warning": "w sk-test-xyz",
        "card": "c sk-test-xyz", "message": "m sk-test-xyz", "argv": ["claude", "sk-test-xyz"],
        "children": [{"warning": "nested sk-test-xyz"}],
    })
    for key, val in [("name", "sk-test-xyz"), ("cwd", "/srv/u/sk-test-xyz"),
                     ("session_id", "sk-test-xyz"), ("prompt_file", "/tmp/sk-test-xyz.txt"),
                     ("handoff", "/tmp/sk-test-xyz.md"), ("mcp", "/tmp/sk-test-xyz.json")]:
        assert out[key] == val  # structured fields are never redacted
    for key in ("reason", "error", "warning", "card", "message"):
        assert "sk-test-xyz" not in out[key] and "[autoos:redacted]" in out[key]
    assert "sk-test-xyz" not in json.dumps(out["argv"])
    assert "sk-test-xyz" not in out["children"][0]["warning"]  # nested free text too


def test_config_refuses_symlink_and_nonregular(site):
    alias = site.tmp_path / "alias-cfg.json"
    alias.symlink_to(site.cfg)
    s = make(site, FakeRun(), config_path=alias)
    with pytest.raises(ParameterRefused, match="symlink"):
        go(site, s)  # the config path itself must be the real file
    dircfg = site.tmp_path / "cfg-dir"
    dircfg.mkdir()
    s2 = make(site, FakeRun(), config_path=dircfg)
    with pytest.raises(ParameterRefused, match="regular"):
        go(site, s2)


def test_config_owner_mismatch_refused(site, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 12345)  # config owned by someone else
    with pytest.raises(ParameterRefused, match="owned"):
        go(site, make(site, FakeRun()))


def test_config_world_writable_refused(site):
    site.cfg.chmod(0o666)  # regular file, but anyone may rewrite it
    try:
        with pytest.raises(ParameterRefused, match="group/world-writable"):
            go(site, make(site, FakeRun()))
    finally:
        site.cfg.chmod(0o600)


def test_mcp_symlink_alias_to_allowed_file_is_not_a_finding(site):
    alias = site.tmp_path / "alias-mcp.json"
    alias.symlink_to(site.mcp.resolve())
    run = FakeRun(stdout="session " + UUID1)
    res = go(site, make(site, run), mcp_config=str(alias))
    assert res["ok"] is True  # resolved-path comparison: the alias is fine


def test_default_config_path_used_when_env_unset(site, monkeypatch):
    monkeypatch.delenv("PLANGRAPH_SESSIONS_CONFIG", raising=False)
    dflt = site.home / ".config" / "plangraph"
    dflt.mkdir(parents=True, mode=0o700)
    write_config(dflt / "sessions.json", site.cfg.read_text())
    s = Sessions(state_root=site.state, run=FakeRun(), now=lambda: 1000.0, sleep=lambda _s: None)
    res = go(site, s)
    assert res["ok"] is True  # ~/.config/plangraph/sessions.json is the default


def test_mcp_undeclared_args_refused_for_all_five(site, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=site.state, session_run=FakeRun())
    monkeypatch.setenv("PLANGRAPH_SESSIONS_CONFIG", str(site.cfg))

    async def go_rpc():
        calls = [("plangraph_session_launch", {
                     "name": "alpha", "cwd": str(site.proj), "model": MODEL, "effort": EFFORT,
                     "mcp_config": str(site.mcp), "permission_mode": MODE, "prompt": PROMPT,
                     "dry_run": True, "bogus": 1}),
                 ("plangraph_session_stop", {"name": "alpha", "bogus": 1}),
                 ("plangraph_session_relaunch", {"name": "alpha", "bogus": 1}),
                 ("plangraph_session_mark_ready", {"name": "alpha", "handoff": "x", "bogus": 1}),
                 ("plangraph_session_list", {"bogus": 1})]
        for tool, args in calls:
            with pytest.raises(ParameterRefused, match="bogus"):
                await server.call_tool(tool, args)

    asyncio.run(go_rpc())


def test_mcp_missing_wrong_type_args_audited(site, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=site.state, session_run=FakeRun())
    monkeypatch.setenv("PLANGRAPH_SESSIONS_CONFIG", str(site.cfg))

    async def go_rpc():
        with pytest.raises(ParameterRefused):
            await server.call_tool("plangraph_session_launch", {
                "name": "alpha", "cwd": 5, "model": MODEL, "effort": EFFORT,
                "mcp_config": str(site.mcp), "permission_mode": MODE, "prompt": PROMPT})
        with pytest.raises(ParameterRefused):
            await server.call_tool("plangraph_session_relaunch", {})
        with pytest.raises(ParameterRefused):
            await server.call_tool("plangraph_session_stop", {"name": 7})

    asyncio.run(go_rpc())
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert [l["action"] for l in lines] == ["launch", "relaunch", "stop"]
    assert all(l["ok"] is False and l["reason"] for l in lines)  # same audit path


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
    assert s.stop(name="alpha")["ok"] is True
    out = json.loads(rec_path.read_text())
    assert out["stopped_ts"] is not None
    assert not (site.state / "sessions" / "beta.json").exists()


def test_launch_run_oserror_writes_null_id_record(site):
    class OsErrRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"] and self.broken:
                raise OSError("exec format error")
            return super().__call__(argv, **kw)

    run = OsErrRun(stdout="session " + UUID1)
    run.broken = True
    s = make(site, run)
    res = go(site, s)
    assert res["ok"] is False and res["name"] == "alpha" and res["session_id"] is None
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] is None and rec["stopped_ts"] is None  # list shows it
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert lines[-1]["action"] == "launch" and lines[-1]["ok"] is False
    assert [r for r in s.list()["sessions"] if r["name"] == "alpha"][0]["id"] is None
    run.broken = False
    run.stdout = "session " + UUID2
    retry = s.relaunch("alpha", force=True)  # force replaces the orphan record
    assert retry["ok"] is True and retry["session_id"] == UUID2
    rec2 = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec2["session_id"] == UUID2 and rec2["stopped_ts"] is None


def test_launch_timeout_writes_null_id_record(site):
    class TimeoutRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"]:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
            return super().__call__(argv, **kw)

    s = make(site, TimeoutRun())
    with pytest.raises(subprocess.TimeoutExpired):
        go(site, s)
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] is None and rec["stopped_ts"] is None  # the process may exist
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert lines[-1]["action"] == "launch" and lines[-1]["ok"] is False


def test_successful_launch_unlinks_stale_ready_marker(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    marker = site.state / "sessions" / "alpha.ready.json"
    assert marker.is_file()
    s.stop(name="alpha")
    run.stdout = "session " + UUID2
    res = go(site, s)  # a fresh successful launch over the stopped record
    assert res["ok"] is True
    assert not marker.is_file()  # the old session's ready marker is stale and gone


def test_refusal_outcome_survives_audit_write_failure(site, monkeypatch):
    import plangraph.spawner.sessions as mod
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)

    def dead(*_a, **_kw):
        raise OSError("audit disk full")

    monkeypatch.setattr(mod, "append_private", dead)
    with pytest.raises(ParameterRefused) as exc:
        go(site, s, name="bad name")
    assert "audit" in exc.value.warning  # the refusal stands; the caller is warned


def test_timeout_outcome_survives_audit_write_failure(site, monkeypatch):
    import plangraph.spawner.sessions as mod
    class TimeoutRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"]:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
            return super().__call__(argv, **kw)

    s = make(site, TimeoutRun())

    def dead(*_a, **_kw):
        raise OSError("audit disk full")

    monkeypatch.setattr(mod, "append_private", dead)
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        go(site, s)
    assert "audit" in exc.value.warning  # the timeout stands; the caller is warned


def test_action_failed_outcome_survives_audit_write_failure(site, monkeypatch):
    import plangraph.spawner.sessions as mod
    run = FakeRun(stdout="session " + UUID1, launch_rc=1)
    s = make(site, run)

    def dead(*_a, **_kw):
        raise OSError("audit disk full")

    monkeypatch.setattr(mod, "append_private", dead)
    res = go(site, s)
    assert res["ok"] is False and res["name"] == "alpha" and res["session_id"] is None
    assert "audit" in res["warning"]  # the failure stands; the caller is warned


def test_run_oserror_outcome_survives_audit_write_failure(site, monkeypatch):
    import plangraph.spawner.sessions as mod
    class OsErrRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"]:
                raise OSError("exec format error")
            return super().__call__(argv, **kw)

    s = make(site, OsErrRun())

    def dead(*_a, **_kw):
        raise OSError("audit disk full")

    monkeypatch.setattr(mod, "append_private", dead)
    res = go(site, s)
    assert res["ok"] is False and res["name"] == "alpha"
    assert "audit" in res["warning"]  # the failure stands; the caller is warned


def test_unexpected_outcome_survives_audit_write_failure(site, monkeypatch):
    import plangraph.spawner.sessions as mod
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)

    def boom(*_a, **_kw):
        raise RuntimeError("backend exploded")

    def dead(*_a, **_kw):
        raise OSError("audit disk full")

    monkeypatch.setattr(s, "_check_cwd", boom)
    monkeypatch.setattr(mod, "append_private", dead)
    with pytest.raises(ParameterRefused) as exc:
        go(site, s)
    assert "RuntimeError" in str(exc.value) and "backend exploded" in str(exc.value)
    assert "audit" in exc.value.warning  # the refusal stands; the caller is warned


def test_config_and_style_are_read_from_the_checked_fd(site, monkeypatch):
    import io
    cfg2 = site.tmp_path / "sessions-fd.json"
    write_config(cfg2, json.dumps({**json.loads(site.cfg.read_text()), "style_text": "be terse"}))
    forbidden = {str(cfg2.resolve())}
    real_open, real_io_open = open, io.open

    def guard(fn):
        def wrapped(file, *args, **kwargs):
            # A by-name open of the config is exactly the finding: the checked
            # fd is the only fd its bytes may come from (style_text rides in the
            # config itself now - the style_file half is a cut feature).
            if isinstance(file, (str, Path)) and str(Path(file).resolve()) in forbidden:
                raise AssertionError("the config must be read from the checked fd")
            return fn(file, *args, **kwargs)
        return wrapped

    monkeypatch.setattr("builtins.open", guard(real_open))
    monkeypatch.setattr(io, "open", guard(real_io_open))
    run = FakeRun(stdout="session " + UUID1)
    res = go(site, make(site, run, config_path=cfg2))
    assert res["ok"] is True
    argv, _ = run.calls[0]
    assert argv[-3:] == ["--append-system-prompt", "be terse", PROMPT]


def test_config_rejects_unknown_keys_and_bad_scalar_types(site):
    cfg = json.loads(site.cfg.read_text())

    def refused(over, match="config"):
        bad = site.tmp_path / "sessions-bad.json"
        write_config(bad, json.dumps({**cfg, **over}))
        with pytest.raises(ParameterRefused, match=match):
            go(site, make(site, FakeRun(), config_path=bad))

    refused({"extra": 1}, match="unknown")  # an unknown top-level key refuses every action
    refused({"style_file": None})           # a present key must carry its type - null is not a value
    refused({"style_file": 5})
    refused({"compact_window": None})
    refused({"compact_window": []})
    refused({"ctx_cap_tokens": None})
    refused({"ctx_cap_tokens": True})       # a bool is not a positive int
    refused({"ctx_cap_tokens": 1.5})
    refused({"windows_launcher": None})
    refused({"windows_launcher": "cmd"})    # the wt launcher is cut: the key is refused outright
    refused({"windows_launcher": 1})


def test_audit_argv_redacts_style_text(site):
    # The value after --append-system-prompt is config style prose: the audit
    # argv must show its length like the prompt, never its text (SESSION-SPAWN-MCP).
    style_text = "STYLE-SECRET-TEXT"  # 17 chars
    cfg2 = site.tmp_path / "sessions-secret.json"
    write_config(cfg2, json.dumps({**json.loads(site.cfg.read_text()), "style_text": style_text}))
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run, config_path=cfg2)
    assert go(site, s)["ok"] is True
    audit_text = (site.state / "sessions" / "audit.log").read_text()
    assert style_text not in audit_text
    line = json.loads(audit_text.splitlines()[-1])
    argv = line["argv"]
    assert argv[argv.index("--append-system-prompt") + 1] == "<17 chars>"
    res = go(site, s, name="beta", dry_run=True)
    assert all(style_text not in a for a in res["argv"])


# ---- round 4: fail-closed agents polls and post-launch record writes ----


def test_relaunch_already_stopped_poll_failure_refuses(site):
    class BrokenPoll(FakeRun):
        def __call__(self, argv, **kw):
            if self._is_agents(argv) and self.poll_broken:
                raise OSError("agents unavailable")
            return super().__call__(argv, **kw)

    run = BrokenPoll(stdout="session " + UUID1)
    run.poll_broken = False
    s = make(site, run)
    rec_path, marker_path = _stopped(site, run, s)
    run.poll_broken = True
    res = s.relaunch("alpha")
    assert res["ok"] is False  # fail closed: the old id could not be checked
    assert len(run.launch_calls()) == 1  # nothing was relaunched
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "relaunch" and line["ok"] is False
    assert "agents poll failed" in line["reason"]
    rec = json.loads(rec_path.read_text())
    assert rec["session_id"] == UUID1 and rec["stopped_ts"] is not None
    assert marker_path.exists()  # a retry can relaunch once polling works


def test_relaunch_already_stopped_poll_garbage_refuses(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    rec_path, marker_path = _stopped(site, run, s)
    run.agents_rows = {"not": "a list"}  # garbage where rows should be
    res = s.relaunch("alpha")
    assert res["ok"] is False  # garbage is no answer: the relaunch is refused
    assert len(run.launch_calls()) == 1  # nothing was relaunched
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "relaunch" and line["ok"] is False
    assert "agents poll failed" in line["reason"]
    rec = json.loads(rec_path.read_text())
    assert rec["session_id"] == UUID1 and rec["stopped_ts"] is not None
    assert marker_path.exists()


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


def test_launch_record_write_failure_reports_started_id(site, monkeypatch):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    real = s._write_record

    def dead(name, rec):
        # Only the post-start write fails: the write-ahead "launching" record
        # must land before the process starts (D-3193) - this test's subject
        # is the write AFTER the start, which must report the started id.
        if rec.get("state") == "running":
            raise OSError("no space left on device")
        return real(name, rec)

    monkeypatch.setattr(s, "_write_record", dead)
    res = go(site, s)
    assert res["ok"] is False and res["session_id"] is None
    assert len(run.launch_calls()) == 1  # Claude may be running: the id must survive
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "launch" and line["ok"] is False
    assert f"session started (id {UUID1}) but record write failed" in line["reason"]


def test_launch_marker_unlink_failure_reports_started_id(site, monkeypatch):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)

    class Boom:
        def unlink(self, missing_ok=False):
            raise OSError("unlink refused")

    monkeypatch.setattr(s, "_marker_path", lambda name: Boom())
    res = go(site, s)
    assert res["ok"] is False  # the session started: its id must reach the audit line
    assert len(run.launch_calls()) == 1
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "launch" and line["ok"] is False
    assert f"session started (id {UUID1}) but record write failed" in line["reason"]


def test_relaunch_record_write_failure_reports_new_id(site, monkeypatch):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    rec_path, marker_path = _ready(site, run, s)
    run.stdout = "session " + UUID2
    real = s._write_record

    def flaky(name, rec):
        if rec.get("session_id") == UUID2:  # only the NEW record's write fails
            raise OSError("no space left on device")
        return real(name, rec)

    monkeypatch.setattr(s, "_write_record", flaky)
    res = s.relaunch("alpha")
    assert res["ok"] is False
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "relaunch" and line["ok"] is False
    assert f"session started (id {UUID2}) but record write failed" in line["reason"]
    _assert_old_record_kept(rec_path, marker_path, site)


def test_launch_parameter_refused_after_start_writes_orphan_record(site):
    exe = str(site.tmp_path / "claude.exe")
    seen = {"n": 0}

    def which(cmd):
        seen["n"] += 1
        return exe if seen["n"] == 1 else None  # resolution dies after argv is built

    run = FakeRun(stdout="backgrounded, id pending")  # no UUID: _resolve_id polls
    s = make(site, run, platform="win32", which=which)
    with pytest.raises(ParameterRefused, match="claude not found"):
        go(site, s)
    assert any(a[0] == exe and a[1:2] == ["--bg"] for a, _ in run.calls)  # it started
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] is None and rec["stopped_ts"] is None  # the orphan is recorded
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "launch" and line["ok"] is False


def test_record_state_launching_when_run_called(site):
    class ProbeRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"]:
                rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
                assert rec["state"] == "launching"  # written before the run
                assert rec["session_id"] is None
                assert rec["launched_ms"] == 1_000_000.0  # taken before the start
                assert rec["argv"][-1] == f"<{len(PROMPT)} chars>"  # redacted argv
            return super().__call__(argv, **kw)

    s = make(site, ProbeRun(stdout="session " + UUID1), now=lambda: 1000.0)
    go(site, s)
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "running" and rec["session_id"] == UUID1


def test_timeout_record_state_unknown(site):
    class TimeoutRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"]:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
            return super().__call__(argv, **kw)

    s = make(site, TimeoutRun())
    with pytest.raises(subprocess.TimeoutExpired):
        go(site, s)
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "unknown" and rec["stopped_ts"] is None  # the process may exist


def test_exit_failure_record_state_stopped(site):
    run = FakeRun(stdout="untrusted workspace", launch_rc=1)
    s = make(site, run)
    res = go(site, s)
    assert res["ok"] is False
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["state"] == "stopped" and rec["stopped_ts"] is not None  # nothing was started
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert "claude exited 1: untrusted workspace" in line["reason"]  # slice-2a stdout text


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


def test_dry_run_writes_no_launch_record(site):
    run = FakeRun()
    s = make(site, run)
    go(site, s, dry_run=True)
    assert run.calls == []  # nothing ran
    assert not (site.state / "sessions" / "alpha.json").exists()  # no write-ahead record
    assert not (site.state / "sessions" / "audit.log").exists()


def test_legacy_record_without_state_is_read(site):
    sessions = site.state / "sessions"
    sessions.mkdir(parents=True)
    legacy = {"name": "alpha", "session_id": None, "stopped_ts": None}
    (sessions / "alpha.json").write_text(json.dumps(legacy))
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    listed = [r for r in s.list()["sessions"] if r["name"] == "alpha"][0]
    assert listed["state"] == "running"  # no state field, stopped_ts unset
    with pytest.raises(ParameterRefused, match="has a running record"):
        go(site, s)  # a legacy live record refuses a plain launch
    assert run.launch_calls() == []
    legacy["stopped_ts"] = 500.0
    (sessions / "alpha.json").write_text(json.dumps(legacy))
    listed = [r for r in s.list()["sessions"] if r["name"] == "alpha"][0]
    assert listed["state"] == "stopped"  # no state field, stopped_ts set
    res = go(site, s)  # a legacy stopped record is replaced by a plain launch
    assert res["ok"] is True
    rec = json.loads((sessions / "alpha.json").read_text())
    assert rec["state"] == "running"  # the new record carries the state field


def test_list_state_prefers_agents_row(site):
    run = FakeRun(stdout="session " + UUID1, agents_rows=[
        {"id": "r", "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "working", "startedAt": 1_000_000}])
    s = make(site, run)
    go(site, s)
    listed = [r for r in s.list()["sessions"] if r["name"] == "alpha"][0]
    assert listed["state"] == "working"  # the live row wins over the record
