"""Frozen Rust MCP contract; regeneration is explicit, never part of a test."""
import json
from pathlib import Path

from plangraph.keeper import mcp

FIXTURES = Path(__file__).parent / "fixtures"
REQUESTS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    {"jsonrpc": "2.0", "id": "init-echo", "method": "initialize",
     "params": {"protocolVersion": "future-λ\n\"version"}},
    *({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": params}
      for params in ({"protocolVersion": ""}, {"protocolVersion": None},
                     {"protocolVersion": 42}, [])),
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": "tools-λ", "method": "tools/list"},
]


class NoKeeper:
    def call(self, *args, **kwargs):
        raise AssertionError("snapshot must not call the keeper")


def python_reply(request):
    return json.dumps(mcp.handle(request, NoKeeper()), ensure_ascii=False) + "\n"


def write_fixtures(directory):
    """Run once explicitly after reviewing Python contract changes."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    tools = ",\n".join(json.dumps(tool, ensure_ascii=False) for tool in mcp.TOOLS)
    (directory / "keeper_mcp_tools.json").write_text("[\n" + tools + "\n]\n", encoding="utf-8")
    records = [{"request": request, "response": python_reply(request)} for request in REQUESTS]
    (directory / "keeper_mcp_replies.json").write_text(
        "[\n" + ",\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n]\n",
        encoding="utf-8")


def test_keeper_mcp_tools_snapshot():
    assert json.loads((FIXTURES / "keeper_mcp_tools.json").read_text(encoding="utf-8")) == list(mcp.TOOLS)


def test_keeper_mcp_reply_snapshots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    records = json.loads((FIXTURES / "keeper_mcp_replies.json").read_text(encoding="utf-8"))
    assert [record["request"] for record in records] == REQUESTS
    for record in records:
        assert record["response"].encode() == python_reply(record["request"]).encode()
