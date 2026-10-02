"""MCP stdio server tests: JSON-RPC framing, tools, refusals and token safety.

No real keeper API and no network beyond a loopback ``http.server`` on an
ephemeral port.  The server is driven through pipes with ``io.StringIO``.
"""
import io
import json
import os
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.keeper.mcp import (
    PROTOCOL_VERSION, TOOLS, HttpKeeper, serve,
)

TOKEN = "s3cret-token-must-never-print-42"
SRC = Path(__file__).resolve().parents[1] / "src"


class FakeKeeper(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass

    def _reply(self, status, body, content_type="text/plain"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.server.calls.append({"method": "GET", "path": self.path,
                                  "headers": dict(self.headers)})
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/v1/ready":
            self._reply(200, "demo:A | a | est 1h | unblocks 0 | prio 0")
        elif parsed.path == "/v1/show" and query.get("key") == ["missing"]:
            self._reply(404, '{"error":"not_found","msg":"no item"}',
                        "application/json")
        elif parsed.path == "/v1/whoami":
            self._reply(200, '{"name":"l1main"}', "application/json")
        else:
            self._reply(200, "ok")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        self.server.calls.append({"method": "POST", "path": self.path,
                                  "headers": dict(self.headers), "body": body})
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/v1/claim":
            self._reply(200, '{"key":"demo:A","epoch":1}', "application/json")
        elif parsed.path == "/v1/cancel":
            self._reply(403, '{"error":"forbidden","msg":"nope"}',
                        "application/json")
        else:
            self._reply(200, "ok")


@pytest.fixture
def api(tmp_path, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), FakeKeeper)
    server.calls = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("PG_URL", base)
    monkeypatch.setenv("PG_TOKEN_FILE", str(token_file))
    client = HttpKeeper(base, str(token_file), timeout=5)
    yield SimpleNamespace(server=server, client=client, base=base,
                          token_file=token_file)
    server.shutdown()
    server.server_close()


def run(lines, client):
    """Feed JSON values (dicts) or raw strings to ``serve``; return output."""
    src = "".join(line if isinstance(line, str) else json.dumps(line) + "\n"
                  for line in lines)
    out = io.StringIO()
    serve(io.StringIO(src), out, client)
    return out.getvalue()


def replies(text):
    return [json.loads(line) for line in text.splitlines()]


# --------------------------------------------------------------- handshake

def test_initialize_echoes_requested_version(api):
    text = run([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05"}}], api.client)
    (reply,) = replies(text)
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["serverInfo"]["name"] == "plangraph-keeper"


def test_initialize_defaults_version(api):
    text = run([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}],
               api.client)
    (reply,) = replies(text)
    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_initialized_notification_is_silent(api):
    assert run([{"jsonrpc": "2.0", "method": "notifications/initialized"}],
               api.client) == ""


def test_ping(api):
    (reply,) = replies(run([{"jsonrpc": "2.0", "id": 9, "method": "ping"}],
                           api.client))
    assert reply["result"] == {}


def test_unknown_method_is_jsonrpc_error(api):
    (reply,) = replies(run([{"jsonrpc": "2.0", "id": 3, "method": "bogus"}],
                           api.client))
    assert reply["error"]["code"] == -32601


# --------------------------------------------------------------- tools/list

def test_tools_list_count_and_budget(api):
    text = run([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
               api.client)
    (reply,) = replies(text)
    tools = reply["result"]["tools"]
    assert len(tools) == 21
    assert {tool["name"] for tool in tools} == {tool["name"] for tool in TOOLS}
    assert len(text.encode("utf-8")) < 9000  # the ~2,500-token budget


# --------------------------------------------------------------- tools/call

def test_ready_round_trip(api):
    (reply,) = replies(run([{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "ready",
                                        "arguments": {"n": 5}}}], api.client))
    content = reply["result"]["content"][0]
    assert reply["result"]["isError"] is False
    assert content["type"] == "text" and "demo:A" in content["text"]
    call = api.server.calls[-1]
    assert call["method"] == "GET" and call["path"].startswith("/v1/ready")
    assert call["headers"]["Authorization"] == "Bearer " + TOKEN


def test_claim_round_trip_posts_json(api):
    (reply,) = replies(run([{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "claim",
                                        "arguments": {"key": "demo:A"}}}],
                           api.client))
    assert reply["result"]["isError"] is False
    call = api.server.calls[-1]
    assert call["method"] == "POST" and call["path"] == "/v1/claim"
    assert json.loads(call["body"]) == {"key": "demo:A"}
    assert call["headers"]["Authorization"] == "Bearer " + TOKEN


def test_refusal_is_tool_error(api):
    text = run([{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "show",
                            "arguments": {"key": "missing"}}}], api.client)
    (reply,) = replies(text)
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"] == "not_found: no item"


def test_unknown_tool_is_jsonrpc_error(api):
    text = run([{"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "nope", "arguments": {}}}], api.client)
    (reply,) = replies(text)
    assert reply["error"]["code"] == -32602


def test_unreachable_api_is_tool_error(tmp_path):
    token_file = tmp_path / "t"
    token_file.write_text(TOKEN, encoding="utf-8")
    client = HttpKeeper("http://127.0.0.1:1", str(token_file), timeout=2)
    text = run([{"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "ready", "arguments": {}}}], client)
    (reply,) = replies(text)
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"].startswith("unreachable:")


def test_missing_token_file_is_tool_error(tmp_path):
    client = HttpKeeper("http://127.0.0.1:1", str(tmp_path / "absent"))
    text = run([{"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                 "params": {"name": "ready", "arguments": {}}}], client)
    (reply,) = replies(text)
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"].startswith("unreachable:")


# --------------------------------------------------------------- framing

def test_malformed_json_line_errors_and_loop_continues(api):
    text = run(["not json at all\n",
                {"jsonrpc": "2.0", "id": 7, "method": "ping"}], api.client)
    parse_error, ping = replies(text)
    assert parse_error["error"]["code"] == -32700
    assert ping["result"] == {}


# --------------------------------------------------------------- token safety

def test_token_never_appears_in_output(api):
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "whoami", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "cancel", "arguments": {"key": "demo:A"}}},
    ]
    text = run(lines, api.client)
    assert TOKEN not in text
    # the token did reach the API as a bearer header, and nowhere else
    assert any(call["headers"].get("Authorization") == "Bearer " + TOKEN
               for call in api.server.calls)


# --------------------------------------------------------------- entry point

def test_module_entry_point(api):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    process = subprocess.run(
        [sys.executable, "-m", "plangraph.keeper.mcp"],
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        + "\n",
        capture_output=True, text=True, env=env, timeout=30)
    assert process.returncode == 0
    (reply,) = replies(process.stdout)
    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert TOKEN not in process.stdout + process.stderr
