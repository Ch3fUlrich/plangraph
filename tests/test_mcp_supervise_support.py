"""Shared helpers for the tests/test_mcp_supervise*.py files (collects no tests)."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from plangraph.mcp_supervise.config import load_config
from plangraph.mcp_supervise.health import probe
from plangraph.mcp_supervise.procutil import kill_process_tree, pid_alive, read_pid_file
from plangraph.mcp_supervise.supervisor import Supervisor

ROOT = Path(__file__).resolve().parent.parent
# The CLI runs in a child interpreter: src stays ahead of the ambient PYTHONPATH.
CLI_ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(
    [str(ROOT / "src")] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p])}
CLI = [sys.executable, "-m", "plangraph.mcp_supervise"]

# Fake HTTP JSON-RPC server: argv = port [mode]. Mode "stale" answers `initialize`
# but fails everything else - what a live mcp-proxy does once its stdio child died
# (measured against mcp-proxy 0.13.0). Windows lets a second socket share a port
# when SO_REUSEADDR is set, so the bind is exclusive there like a real server's.
FAKE_SERVER_SCRIPT = """
import sys, json
from http.server import HTTPServer, BaseHTTPRequestHandler

STALE = len(sys.argv) > 2 and sys.argv[2] == "stale"

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/mcp":
            length = int(self.headers.get("Content-Length", 0))
            method = json.loads(self.rfile.read(length) or b"{}").get("method")
            if STALE and method != "initialize":
                body = {"jsonrpc": "2.0", "id": 3, "error": {"code": 0, "message": ""}}
            else:
                body = {"jsonrpc": "2.0", "id": 1, "result": {
                    "protocolVersion": "2024-11-05", "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "1.0"}}}
            resp = json.dumps(body).encode("utf-8")
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

class Server(HTTPServer):
    allow_reuse_address = sys.platform != "win32"

Server(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""

# Tiny stdio MCP server (newline-delimited JSON-RPC) for the real-proxy test. It
# records its pid, and the environment the proxy handed it, in $STARTS_FILE.
STDIO_SERVER_SCRIPT = """
import json, os, sys

with open(os.environ["STARTS_FILE"], "a", encoding="utf-8") as f:
    f.write("start pid=%d utf8=%s mark=%s\\n" % (
        os.getpid(), os.environ.get("PYTHONUTF8"), os.environ.get("MARK")))
for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "stdio-fake", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": []}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\\n")
    sys.stdout.flush()
"""

_handed_out = set()


def free_port() -> int:
    """A port that was free a moment ago and has not been handed out in this process.

    The bind/close race against other processes cannot be removed from the outside;
    never repeating a port at least keeps our own tests from colliding with each other.
    """
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in _handed_out:
            _handed_out.add(port)
            return port


def wait_healthy(port: int, tries: int = 100, delay: float = 0.1) -> bool:
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(tries):
        if probe(url, timeout=0.5):
            return True
        time.sleep(delay)
    return False


def wait_until(pred, tries: int = 100, delay: float = 0.1) -> bool:
    for _ in range(tries):
        if pred():
            return True
        time.sleep(delay)
    return False


def server_entry(name: str, port: int, script: Path, *script_args: str, **extra) -> dict:
    """A proxy-less test server entry running the fake HTTP JSON-RPC server."""
    entry = {
        "name": name,
        "port": port,
        "cmd": [sys.executable, str(script), str(port), *script_args],
        "env": {},
        "scope": "host",
        "proxy": False,
    }
    entry.update(extra)
    return entry


class WorkdirCase(unittest.TestCase):
    """Temp dir, fake server script, log/state paths; cleanup kills what the test started."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mcp-test-"))
        self.script = self.tmp / "fake_srv.py"
        self.script.write_text(FAKE_SERVER_SCRIPT, encoding="utf-8")
        self.log_file = self.tmp / "test.log"
        self.state_dir = self.tmp / "state"
        self.cleanup_procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self.cleanup_procs:
            if p.poll() is None:
                kill_process_tree(p.pid)
            try:
                p.wait(timeout=5)
            except Exception:
                pass
        for f in self.state_dir.glob("*.pid") if self.state_dir.exists() else []:
            pid = read_pid_file(f)
            if pid and pid != os.getpid() and pid_alive(pid):
                kill_process_tree(pid)

    def config(self, *entries) -> dict:
        return {"log_file": str(self.log_file), "state_dir": str(self.state_dir),
                "servers": list(entries)}

    def config_path(self, raw: dict) -> Path:
        p = self.tmp / "config.json"
        p.write_text(json.dumps(raw), encoding="utf-8")
        return p

    def spawn_supervisor(self, raw: dict, **kw):
        proc = subprocess.Popen(
            CLI + ["run", "--config", str(self.config_path(raw))],
            env=CLI_ENV, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        self.cleanup_procs.append(proc)
        return proc

    def spawn_sleeper(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.cleanup_procs.append(proc)
        return proc

    def spawn_orphan(self, port: int, *script_args: str):
        """A fake server not owned by any supervisor (e.g. one that outlived its parent)."""
        proc = subprocess.Popen([sys.executable, str(self.script), str(port), *script_args])
        self.cleanup_procs.append(proc)
        return proc

    def pid_of(self, name: str):
        return read_pid_file(self.state_dir / f"{name}.pid")

    def log_text(self) -> str:
        return self.log_file.read_text(encoding="utf-8") if self.log_file.exists() else ""

    def start_supervisor(self, raw: dict, poll_interval: float = 0.05) -> Supervisor:
        """In-process supervisor on a daemon thread; cleanup stops and joins it."""
        sup = Supervisor(load_config(raw), poll_interval=poll_interval)
        t = threading.Thread(target=sup.run, daemon=True)
        t.start()

        def _stop():
            sup.stop_event.set()
            t.join(timeout=10)

        self.addCleanup(_stop)
        return sup
