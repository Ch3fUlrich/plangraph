"""Shared helpers for the tests/test_mcp_supervise*.py files (collects no tests)."""

import os
import socket
import sys
import time
from pathlib import Path

from plangraph.mcp_supervise.health import probe

ROOT = Path(__file__).resolve().parent.parent
# The CLI runs in a child interpreter: src stays ahead of the ambient PYTHONPATH.
CLI_ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(
    [str(ROOT / "src")] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p])}

FAKE_SERVER_SCRIPT = """
import sys, json
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/mcp":
            length = int(self.headers.get("Content-Length", 0))
            _ = self.rfile.read(length)
            resp = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "1.0"}
                }
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass

port = int(sys.argv[1])
server = HTTPServer(("127.0.0.1", port), Handler)
server.serve_forever()
"""


def free_port() -> int:
    """A port that was free a moment ago (bound to port 0, then closed)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_healthy(port: int, tries: int = 100, delay: float = 0.1) -> bool:
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(tries):
        if probe(url, timeout=0.5):
            return True
        time.sleep(delay)
    return False


def server_entry(name: str, port: int, script: Path, **extra) -> dict:
    """A proxy-less test server entry running the fake HTTP JSON-RPC server."""
    entry = {
        "name": name,
        "port": port,
        "cmd": [sys.executable, str(script), str(port)],
        "env": {},
        "scope": "host",
        "proxy": False,
    }
    entry.update(extra)
    return entry
