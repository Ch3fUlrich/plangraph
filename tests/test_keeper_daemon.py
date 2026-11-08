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


@pytest.mark.parametrize("node_name", ["test_s2_shutdown_bounded", "renamed_shutdown"])
def test_mock_exit_waits_before_recorder(monkeypatch, node_name, request):
    conftest = _root_conftest(request)
    from plangraph.keeper import daemon

    class Cleaner:
        name = "keeper-shutdown"
        alive = True
        _target = lambda: None

        def join(self, timeout):
            self.alive = False

        def is_alive(self):
            return self.alive

    cleaner = Cleaner()
    calls = []
    monkeypatch.setattr(conftest.threading, "enumerate", lambda: [cleaner])
    monkeypatch.setattr(os, "_exit", lambda code: calls.append((code, cleaner.alive)))
    monkeypatch.setattr(daemon, "run", lambda: os._exit(75))
    fixture = conftest.bounded_shutdown_cleanup.__wrapped__(monkeypatch)
    next(fixture)
    daemon.run()
    with pytest.raises(StopIteration):
        next(fixture)
    assert calls == [(75, False)]


def _root_conftest(request):
    from types import SimpleNamespace
    fixture = request._fixturemanager.getfixturedefs("no_leaked_keeper_threads", request.node)[0]
    return SimpleNamespace(**fixture.func.__globals__)


@pytest.mark.parametrize("shape", ["module", "partial", "lambda", "bound"])
@pytest.mark.parametrize("survives", [False, True])
def test_leak_guard_tracks_unnamed_worker_and_joins(monkeypatch, shape, survives, request):
    conftest = _root_conftest(request)
    import threading
    import functools

    def target():
        pass
    target.__module__ = "plangraph.keeper"
    if shape == "partial":
        target = functools.partial(target)
    elif shape == "lambda":
        target = eval("lambda: None", {"__name__": "plangraph.keeper"})
    elif shape == "bound":
        target = type("Worker", (), {"run": target})().run
    class Worker:
        name = "worker"
        daemon = True
        def __init__(self, target):
            self._target, self.alive, self.joins = target, True, 0
        def is_alive(self):
            return self.alive
        def join(self, timeout):
            self.joins += 1
            self.alive = survives
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    guard = conftest.no_leaked_keeper_threads.__wrapped__()
    next(guard)
    worker, unrelated = Worker(target), Worker(lambda: None)
    try:
        threading.Thread.start(worker)
        threading.Thread.start(unrelated)
        if survives:
            with pytest.raises(AssertionError, match="Leaked keeper threads"):
                next(guard)
        else:
            with pytest.raises(StopIteration):
                next(guard)
        assert worker.joins == 1
        assert unrelated.joins == 0
    finally:
        guard.close()


def test_shutdown_wrapper_is_opt_in(request):
    assert "bounded_shutdown_cleanup" not in request.fixturenames


def test_leak_guard_joins_real_worker(request):
    import threading
    from plangraph.keeper import daemon
    stop = threading.Event()
    guard = _root_conftest(request).no_leaked_keeper_threads.__wrapped__()
    next(guard)
    worker = threading.Thread(target=daemon._backup_prune_loop,
                              args=({"interval_s": 1}, stop, lambda message: None), name="worker")
    worker.start()
    timer = threading.Timer(0.05, stop.set)
    timer.start()
    try:
        with pytest.raises(StopIteration):
            next(guard)
        assert not worker.is_alive()
    finally:
        stop.set()
        worker.join(timeout=5)
        timer.join(timeout=5)
        guard.close()


def test_refused_start_stops_server_workers(tmp_path, monkeypatch):
    import threading
    from plangraph.keeper import daemon, anchor, config
    _, cfg_path = make_db_and_config(tmp_path, 0)
    monkeypatch.setattr(anchor, "enabled", lambda cfg: True)
    def refuse(*args):
        raise RuntimeError("startup refused")
    monkeypatch.setattr(anchor, "check_start", refuse)
    before = set(threading.enumerate())
    with pytest.raises(RuntimeError, match="startup refused"):
        daemon.run(config.load_config(cfg_path))
    workers = [t for t in threading.enumerate() if t not in before]
    for worker in workers:
        worker.join(timeout=1)
    assert not [t.name for t in workers if t.is_alive()]
