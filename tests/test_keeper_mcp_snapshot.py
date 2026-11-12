"""Frozen Rust MCP contract; regeneration is explicit, never part of a test."""
import io
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
    # Invalid requests reply even when there is no id (unlike notifications).
    None, False, 42, "not an object", [], [{"method": "ping", "id": 1}],
    {}, {"jsonrpc": "2.0", "id": "missing-method"},
    {"jsonrpc": "2.0", "id": 0, "method": "ping"},
    {"jsonrpc": "2.0", "id": "ping-λ\n\"\\", "method": "ping"},
    *({"jsonrpc": "2.0", "id": mid, "method": "ping"}
      for mid in (False, 1.5, 1e-7, 1e16, 123.0, -0.0, 0.0001, 1e-5)),
    *({"jsonrpc": "2.0", "id": "unknown", "method": method}
      for method in ("bogus", "", "unknown-λ\n\"\\", None, True, False,
                     0, 42, -7, 1.5, 1e-7, 1e16, 123.0, -0.0, 0.0001, 1e-5, [], {},
                     [None, True, "λ", "it's", 'both\'\"\\\n\r\t\b\f\x00'],
                     {"nested": [False, {"quote'": 'double"'}],
                      "nonprintable": "\x7f\x85\xa0\u200b\u2028\u2029\u0378\ue000\U000e0001"})),
    # Explicit notifications stay silent even with an id and unusual params.
    {"method": "notifications/initialized"},
    {"id": 9, "method": "notifications/initialized", "params": []},
    {"id": "notice-λ", "method": "notifications/λ"},
    *({"method": method} for method in
      ("initialize", "ping", "tools/list", "bogus", None, 42, [])),
    *({"id": None, "method": method} for method in
      ("initialize", "ping", "tools/list", "bogus", None, False, {})),
    # Unknown tools are rejected locally by Python before reaching NoKeeper.
    {"method": "tools/call", "params": {"name": "not-a-tool"}},
    {"id": None, "method": "tools/call", "params": {"name": "not-a-tool"}},
    {"method": "tools/call", "params": {"name": "claim", "arguments": []}},
]


class NoKeeper:
    def call(self, *args, **kwargs):
        raise AssertionError("snapshot must not call the keeper")


def python_reply(request):
    """Capture the production framing, including zero bytes for notifications."""
    output = io.StringIO()
    mcp.serve(io.StringIO(json.dumps(request, ensure_ascii=False) + "\n"),
              output, NoKeeper())
    return output.getvalue()


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
        response = mcp.handle(record["request"], NoKeeper())
        if record["response"]:
            assert record["response"].endswith("\n")
            assert not record["response"].endswith("\n\n")
            assert json.loads(record["response"]) == response
        else:
            assert response is None


def test_keeper_mcp_serve_stream_snapshot(tmp_path, monkeypatch):
    """Errors and notifications must not stop or add frames to the serve loop."""
    monkeypatch.chdir(tmp_path)
    records = json.loads((FIXTURES / "keeper_mcp_replies.json").read_text(encoding="utf-8"))

    class Output(io.StringIO):
        flushes = 0

        def flush(self):
            self.flushes += 1
            super().flush()

    output = Output()
    source = "\n \t\n".join(json.dumps(record["request"], ensure_ascii=False)
                             for record in records) + "\n"
    mcp.serve(io.StringIO(source), output, NoKeeper())
    assert output.getvalue().encode() == "".join(record["response"] for record in records).encode()
    assert output.flushes == sum(bool(record["response"]) for record in records)
