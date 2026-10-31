"""tests/test_spawner_sessions_mcp.py — SESSION-SPAWN-MCP MCP dispatch tests."""

from __future__ import annotations

import asyncio
import json
import threading
import types
import pytest
from plangraph.routing_errors import ParameterRefused
from sessions_support import UUID1, UUID2, MODEL, EFFORT, MODE, PROMPT, FakeRun, make, go
from sessions_support import site  # noqa: F401


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
    assert set(tools["plangraph_session_relaunch"]["inputSchema"]["properties"]) == {"name", "handoff"}
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
        hf.write_text("carry on again")  # the marker's handoff must be newer than the record
        rel = await server.call_tool("plangraph_session_relaunch", {"name": "alpha"})
        assert rel["name"] == "alpha"
        assert (await server.call_tool("plangraph_session_stop", {"name": "alpha"}))["ok"] is True
        refused = await server.handle_rpc_request({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "plangraph_session_launch", "arguments": {
                "name": "bad name", "cwd": str(site.proj), "model": MODEL, "effort": EFFORT,
                "mcp_config": str(site.mcp), "permission_mode": MODE, "prompt": "x", "dry_run": True}}})
        assert refused["error"]["code"] == -32602

    asyncio.run(go_rpc())


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


def test_mcp_symlink_alias_to_allowed_file_is_not_a_finding(site):
    alias = site.tmp_path / "alias-mcp.json"
    alias.symlink_to(site.mcp.resolve())
    run = FakeRun(stdout="session " + UUID1)
    res = go(site, make(site, run), mcp_config=str(alias))
    assert res["ok"] is True  # resolved-path comparison: the alias is fine


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


def test_mcp_launch_missing_required_arg_refused_and_audited(site, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=site.state, session_run=FakeRun())
    monkeypatch.setenv("PLANGRAPH_SESSIONS_CONFIG", str(site.cfg))

    async def go_rpc():
        with pytest.raises(ParameterRefused, match="missing required argument: cwd"):
            await server.call_tool("plangraph_session_launch", {
                "name": "alpha", "model": MODEL, "effort": EFFORT,
                "mcp_config": str(site.mcp), "permission_mode": MODE})

    asyncio.run(go_rpc())
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert [l["action"] for l in lines] == ["launch"]  # exactly one audit line
    assert lines[0]["ok"] is False
    assert lines[0]["reason"] == "missing required argument: cwd"


def test_mcp_wrong_typed_name_refused_and_audited(site, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=site.state, session_run=FakeRun())
    monkeypatch.setenv("PLANGRAPH_SESSIONS_CONFIG", str(site.cfg))

    async def go_rpc():
        with pytest.raises(ParameterRefused, match="argument name must be string"):
            await server.call_tool("plangraph_session_launch", {
                "name": 5, "cwd": str(site.proj), "model": MODEL, "effort": EFFORT,
                "mcp_config": str(site.mcp), "permission_mode": MODE})

    asyncio.run(go_rpc())
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert [l["action"] for l in lines] == ["launch"]
    assert lines[0]["ok"] is False and lines[0]["reason"] == "argument name must be string"


def test_mcp_dry_run_string_refused_and_audited(site, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=site.state, session_run=FakeRun())
    monkeypatch.setenv("PLANGRAPH_SESSIONS_CONFIG", str(site.cfg))

    async def go_rpc():
        with pytest.raises(ParameterRefused, match="argument dry_run must be boolean"):
            await server.call_tool("plangraph_session_launch", {
                "name": "alpha", "cwd": str(site.proj), "model": MODEL, "effort": EFFORT,
                "mcp_config": str(site.mcp), "permission_mode": MODE, "dry_run": "yes"})

    asyncio.run(go_rpc())
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert [l["action"] for l in lines] == ["launch"]  # refused before dispatch, still audited
    assert lines[0]["ok"] is False


def test_mcp_extra_key_refused_writes_one_audit_line(site, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=site.state, session_run=FakeRun())
    monkeypatch.setenv("PLANGRAPH_SESSIONS_CONFIG", str(site.cfg))

    async def go_rpc():
        with pytest.raises(ParameterRefused,
                match="undeclared session arguments for plangraph_session_list: bogus"):
            await server.call_tool("plangraph_session_list", {"bogus": 1})

    asyncio.run(go_rpc())
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert [l["action"] for l in lines] == ["list"]  # exactly one audit line
    assert lines[0]["ok"] is False and "bogus" in lines[0]["reason"]


def test_mcp_list_with_empty_args_still_works(site, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=site.state, session_run=FakeRun())
    monkeypatch.setenv("PLANGRAPH_SESSIONS_CONFIG", str(site.cfg))

    async def go_rpc():
        res = await server.call_tool("plangraph_session_list", {})
        assert res["sessions"] == []

    asyncio.run(go_rpc())
