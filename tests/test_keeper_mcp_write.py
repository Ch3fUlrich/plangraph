"""PG-MCP-WRITE: ``create``/``edit`` MCP tools over a live temp keeper.

Live HTTP daemon on 127.0.0.1:0 (pattern: tests/test_keeper_e2e.py) driven
through ``plangraph.keeper.mcp.handle`` (pattern: tests/test_mcp.py).  The
store keeps its own role rules; the MCP layer only checks the envelope.
"""
from __future__ import annotations

import json
import threading

import pytest

from keeper_util import World, defn, seed, to_done
from plangraph.keeper import api, config
from plangraph.keeper import mcp


@pytest.fixture
def live(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    cfg_path = tmp_path / "keeper.json"
    cfg_path.write_text(json.dumps({
        "db": str(world.path), "snapshot": str(tmp_path / "snap.json"),
        "bind": "127.0.0.1:0", "reap_interval": 3600}), encoding="utf-8")
    import os
    os.chmod(cfg_path, 0o600)
    cfg = config.load_config(cfg_path)
    server = api.make_server(world.store, cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield world, base, tmp_path
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    world.close()


def _client(live, name):
    world, base, tmp = live
    path = tmp / f"{name}.token"
    path.write_text(world.tok(name), encoding="utf-8")
    return mcp.HttpKeeper(base, str(path), timeout=10)


def _call(client, name, args):
    return mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}, client)


def _text(reply):
    return reply["result"]["content"][0]["text"]


def _pair(pid, project="demo"):
    return [defn(pid, [], project=project),
            defn(pid + "G", [pid], project=project, kind="goal")]


def test_l1_create_own_project_ok_and_show(live):
    client = _client(live, "l1main")
    reply = _call(client, "create", {"items": _pair("W1")})
    assert reply["result"]["isError"] is False, _text(reply)
    shown = _call(client, "show", {"key": "demo:W1"})
    assert shown["result"]["isError"] is False
    assert "demo:W1" in _text(shown)


def test_l1_create_other_project_refused(live):
    client = _client(live, "l1main")
    reply = _call(client, "create", {"items": _pair("X1", "other")})
    assert reply["result"]["isError"] is True
    assert "forbidden" in _text(reply)


def test_edit_open_own_item_ok(live):
    client = _client(live, "l1main")
    reply = _call(client, "edit",
                  {"key": "demo:A", "fields": {"title": "renamed"}})
    assert reply["result"]["isError"] is False, _text(reply)
    assert live[0].store._item("demo:A")["title"] == "renamed"


def test_edit_done_item_locked(live):
    world, _, _ = live
    to_done(world, "demo:A")
    reply = _call(_client(live, "l1main"),
                  "edit", {"key": "demo:A", "fields": {"title": "late"}})
    assert reply["result"]["isError"] is True
    assert "locked" in _text(reply)


def test_router_may_create_anywhere(live):
    client = _client(live, "router")
    reply = _call(client, "create", {"items": _pair("R1", "other")})
    assert reply["result"]["isError"] is False, _text(reply)
    shown = _call(client, "show", {"key": "other:R1"})
    assert shown["result"]["isError"] is False
    assert "other:R1" in _text(shown)


def test_tools_list_has_create_edit_schemas(live):
    client = _client(live, "l1main")
    reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       client)
    tools = {t["name"]: t for t in reply["result"]["tools"]}
    assert "create" in tools and "edit" in tools
    assert tools["create"]["inputSchema"]["properties"].get("items") == {
        "type": "array", "items": {"type": "object"}}
    assert tools["create"]["inputSchema"]["required"] == ["items"]
    assert tools["edit"]["inputSchema"]["required"] == ["key", "fields"]
    assert "own project" in tools["create"]["description"]
    assert "open" in tools["edit"]["description"]


class _FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, verb, args):
        self.calls.append((method, verb, args))
        return "ok", False


def test_create_envelope_rejected_without_http():
    for bad in ({"items": "nope"}, {"items": [{"ok": 1}, "nope"]},
                {"items": [{"project": "demo", "id": f"W{i}"}
                            for i in range(51)]},
                {"key": "demo:A"}):
        client = _FakeClient()
        reply = _call(client, "create", bad)
        assert reply["result"]["isError"] is True
        assert "validation" in _text(reply)
        assert client.calls == []
    client = _FakeClient()
    reply = _call(client, "edit", {"key": "demo:A"})
    assert reply["result"]["isError"] is True
    assert client.calls == []
