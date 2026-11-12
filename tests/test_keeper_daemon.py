"""Daemon: singleton flock, serves health, exits cleanly on SIGTERM."""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import nullcontext
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
    from plangraph.keeper import daemon

    class Cleaner:
        name = "keeper-shutdown"
        alive = True

        def join(self, timeout):
            self.alive = False

        def is_alive(self):
            return self.alive

    cleaner = Cleaner()
    calls = []
    monkeypatch.setattr(os, "_exit", lambda code: calls.append((code, cleaner.alive)))
    monkeypatch.setattr(daemon, "run", lambda: os._exit(75))
    monkeypatch.setattr(request.node, "name", node_name)
    request.getfixturevalue("bounded_shutdown_cleanup")
    assert request.node.name == node_name
    assert cleaner.is_alive()
    with monkeypatch.context() as patch:
        patch.setattr(threading, "enumerate", lambda: [cleaner])
        daemon.run()
    assert calls == [(75, False)]


@pytest.mark.parametrize("shape", ["keeper_util", "test_module", "partial", "lambda", "bound"])
@pytest.mark.parametrize("survives", [False, True])
def test_leak_guard_tracks_unnamed_worker_and_joins(
        monkeypatch, shape, survives, keeper_thread_guard):
    import functools

    def target(self=None):
        pass
    target.__module__ = "keeper_util" if shape == "keeper_util" else __name__
    if shape == "partial":
        target = functools.partial(target)
    elif shape == "lambda":
        target = lambda: None
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

    worker, unrelated = Worker(target), Worker(lambda: None)
    visible = [unrelated]
    expected = (pytest.raises(AssertionError, match="Leaked keeper threads: worker")
                if survives else nullcontext())
    with monkeypatch.context() as patch:
        patch.setattr(threading, "enumerate", lambda: list(visible))
        with expected:
            with keeper_thread_guard():
                visible.append(worker)
                assert worker.is_alive()
        assert worker.joins == 1
        assert unrelated.joins == 0


@pytest.mark.parametrize("opt_in", [False, True])
def test_shutdown_wrapper_is_opt_in(monkeypatch, request, opt_in):
    from plangraph.keeper import daemon
    entered, release = threading.Event(), threading.Event()
    calls = []

    def cleanup():
        entered.set()
        release.wait()

    cleaner = threading.Thread(target=cleanup, name="keeper-shutdown", daemon=True)
    real_join = cleaner.join

    def join(timeout):
        calls.append("join")
        release.set()
        real_join(timeout)

    class Connection:
        def close(self):
            calls.append(("conn.close", cleaner.is_alive()))

    def run():
        cleaner.start()
        assert entered.wait(timeout=5)
        assert cleaner.is_alive()
        os._exit(75)
        Connection().close()

    monkeypatch.setattr(cleaner, "join", join)
    monkeypatch.setattr(os, "_exit", lambda code: calls.append((code, cleaner.is_alive())))
    monkeypatch.setattr(daemon, "run", run)
    if opt_in:
        request.getfixturevalue("bounded_shutdown_cleanup")
    try:
        daemon.run()
        assert calls == (["join", (75, False), ("conn.close", False)] if opt_in
                         else [(75, True), ("conn.close", True)])
    finally:
        release.set()
        real_join(timeout=5)
    assert not cleaner.is_alive()


def test_leak_guard_joins_real_worker(tmp_path, monkeypatch, keeper_thread_guard):
    from plangraph.keeper import daemon
    stop, entered = threading.Event(), threading.Event()
    settings = {"enabled": True, "dir": str(tmp_path), "keep": 1,
                "daily_days": 1, "interval_s": 60}
    worker = threading.Thread(target=daemon._backup_prune_loop,
                              args=(settings, stop, lambda message: entered.set()), name="worker")
    real_join = worker.join
    joins = []

    def stop_and_join(timeout):
        assert worker.is_alive()
        joins.append(timeout)
        stop.set()
        real_join(timeout)

    monkeypatch.setattr(worker, "join", stop_and_join)
    try:
        with keeper_thread_guard():
            worker.start()
            assert entered.wait(timeout=5)
            assert worker.is_alive()
        assert joins == [5]
        assert not worker.is_alive()
    finally:
        stop.set()
        real_join(timeout=5)


@pytest.mark.parametrize("cleanup_error", [None, "stop_reaper", "server_close", "both"])
def test_refused_start_stops_server_workers(tmp_path, monkeypatch, cleanup_error):
    from plangraph.keeper import daemon, anchor, config
    _, cfg_path = make_db_and_config(tmp_path, 0)
    servers, workers, calls, cleanup = [], [], [], []
    real_make_server = daemon.api.make_server

    def make_server(*args, **kwargs):
        server = real_make_server(*args, **kwargs)
        servers.append(server)
        workers.append(server.reaper)
        real_stop, real_close = server.stop_reaper, server.server_close
        cleanup.extend([real_stop, real_close])

        def stop_reaper():
            calls.append("stop_reaper")
            real_stop()
            # Inject once: server_close() also calls stop_reaper internally.
            if cleanup_error in ("stop_reaper", "both") and calls.count("stop_reaper") == 1:
                raise RuntimeError("reaper cleanup failed")

        def server_close():
            calls.append("server_close")
            real_close()
            if cleanup_error in ("server_close", "both"):
                raise RuntimeError("server cleanup failed")

        monkeypatch.setattr(server, "stop_reaper", stop_reaper)
        monkeypatch.setattr(server, "server_close", server_close)
        return server

    monkeypatch.setattr(daemon.api, "make_server", make_server)
    monkeypatch.setattr(anchor, "enabled", lambda cfg: True)

    def refuse(*args):
        assert len(servers) == 1
        assert isinstance(servers[0], daemon.api.KeeperHTTPServer)
        assert servers[0].socket.fileno() >= 0
        assert servers[0].reaper is workers[0]
        assert workers[0].is_alive()
        raise RuntimeError("startup refused")

    monkeypatch.setattr(anchor, "check_start", refuse)
    try:
        with pytest.raises(RuntimeError, match="^startup refused$"):
            daemon.run(config.load_config(cfg_path))
        assert calls[:2] == ["stop_reaper", "server_close"]
        assert servers[0].socket.fileno() == -1
        assert not workers[0].is_alive()
    finally:
        for close in cleanup:
            close()
