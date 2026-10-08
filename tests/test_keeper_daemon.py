"""Daemon: singleton flock, serves health, exits cleanly on SIGTERM."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from plangraph.keeper import db as kdb

SRC = Path(__file__).resolve().parents[1] / "src"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_db_and_config(tmp_path, port):
    db_path = tmp_path / "keeper.db"
    kdb.open_db(db_path, create=True).close()
    cfg_path = tmp_path / "keeper.json"
    cfg_path.write_text(json.dumps({
        "db": str(db_path), "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": f"127.0.0.1:{port}",
    }), encoding="utf-8")
    os.chmod(cfg_path, 0o600)
    return db_path, cfg_path


def spawn(cfg_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "plangraph.keeper.daemon", "--config", str(cfg_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def wait_health(port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/health", timeout=0.5) as resp:
                return json.loads(resp.read())
        except Exception:
            time.sleep(0.05)
    raise AssertionError("daemon did not become healthy")


def test_daemon_serves_and_sigterm_is_clean(tmp_path):
    port = free_port()
    _, cfg_path = make_db_and_config(tmp_path, port)
    proc = spawn(cfg_path)
    try:
        assert wait_health(port)["status"] == "ok"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_daemon_refuses_when_lock_is_held(tmp_path):
    port = free_port()
    db_path, cfg_path = make_db_and_config(tmp_path, port)
    lock = kdb.lock_keeper(db_path)
    try:
        proc = spawn(cfg_path)
        assert proc.wait(timeout=10) != 0
        _, err = proc.communicate()
        assert b"locked" in err.lower()
    finally:
        lock.release()
