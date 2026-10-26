"""R2c: keeper fatal paths vs the bus registry (BUS-REGISTRY-PERSIST).

I10: fail() before on_fatal is wired still exits daemon.run 75 within 3 s.
I11: ConnectionLost in the bus persistence hook is fatal, other errors are not.
I12: _cleanup stops and joins the bus registry before server.shutdown()."""
import json
import os
import signal
import socket
import threading
import time
from contextlib import suppress

import pytest
from door_util import Doors
from keeper_util import World
from plangraph.fleet_bus import discovery
from plangraph.keeper import config, daemon, db as kdb
from plangraph.keeper.errors import ConnectionLost


class _FakeLock:            # the template's always-alive keeper lock
    def alive(self):
        return True

    def release(self):
        pass


def _world(tmp_path, name):
    sub = tmp_path / name
    sub.mkdir(exist_ok=True)
    with socket.socket() as s:          # one free TCP port per daemon run
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    path = sub / "keeper.json"
    path.write_text(json.dumps({
        "db": str(sub / "keeper.db"),
        "snapshot": str(sub / "plan.snapshot.json"),
        "bind": f"127.0.0.1:{port}"}), encoding="utf-8")
    path.chmod(0o600)
    return World(sub), config.load_config(path)


def _run_with_backstop(cfg, ready, seconds):
    """daemon.run with a SIGTERM backstop; returns (rc, seconds spent inside)."""
    timer = threading.Timer(seconds, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        t0 = time.monotonic()
        rc = daemon.run(cfg, ready=ready)
        return rc, time.monotonic() - t0
    finally:
        timer.cancel()


# ------------------------------------------------------------------ I10 / F1
def test_fatal_before_on_fatal_wiring_exits_75(tmp_path, monkeypatch):
    """I10: a fail() recorded while on_fatal is unwired still exits 75 in 3 s."""
    w_ctl, cfg_ctl = _world(tmp_path, "ctl")
    w_fat, cfg_fat = _world(tmp_path, "fatal")
    monkeypatch.setattr(kdb, "lock_keeper", lambda *a, **kw: _FakeLock())
    conn = {"now": w_ctl.conn}
    monkeypatch.setattr(kdb, "open_db", lambda *a, **kw: conn["now"])
    seen = []
    # positive control: same setup without the fault -> exit 0, no fatal
    rc, _ = _run_with_backstop(cfg_ctl, seen.append, 1.2)
    assert rc == 0
    assert seen[-1].fatal_reason is None
    # the fault: fail() lands while on_fatal is unwired (pre-run() or raised hook)
    conn["now"] = w_fat.conn
    def pre_wiring_fail(server):
        seen.append(server)
        server.on_fatal = None     # unwired: fail() can only record, never stop.set
        server.fail("fatal before on_fatal wiring")

    rc, duration = _run_with_backstop(cfg_fat, pre_wiring_fail, 3.5)
    assert rc == 75
    assert duration < 3.0          # ... and the watchdog notices within a tick
    assert seen[-1].fatal_reason == "fatal before on_fatal wiring"
    for world in (w_ctl, w_fat):
        with suppress(Exception):  # run() already closed the shared connection
            world.close()


# ------------------------------------------------------------------ I11 / F2
class _StubStore:
    def __init__(self, exc):
        self.exc = exc

    def upsert_bus_hosts(self, rows):
        raise self.exc


def test_bus_persist_connection_lost_is_fatal(tmp_path):
    """I11: ConnectionLost in _persist_bus_hosts is fatal, a ValueError is not."""
    w = World(tmp_path)
    doors = Doors(w, tmp_path)
    host = discovery.Host("h1", "n1", "k1", tmp_path / "h1.key")
    try:
        # positive control: the healthy path persists and never goes fatal
        assert doors.server._persist_bus_hosts([host]) is None
        assert doors.server.store.list_bus_hosts()[0]["host"] == "h1"
        assert doors.server.fatal_reason is None
        # every other exception keeps today's path (discovery logs it, not fatal)
        doors.server.store = _StubStore(ValueError("bad rows"))
        with pytest.raises(ValueError):
            doors.server._persist_bus_hosts([host])
        assert doors.server.fatal_reason is None
        # the fault: a lost session fails loud instead of being swallowed
        doors.server.store = _StubStore(ConnectionLost("statement"))
        assert doors.server._persist_bus_hosts([host]) is None  # never re-raises
        assert doors.server.fatal_reason == "postgres session lost (bus registry)"
    finally:
        doors.close()
        w.close()


# ------------------------------------------------------------------ I12 / F3
class _JoinSpy:             # records join() calls; the real thread still runs
    def __init__(self, real):
        self._real = real
        self.joins = []

    def join(self, timeout=None):
        self.joins.append(timeout)
        return self._real.join(timeout)


def test_cleanup_stops_bus_registry_first(tmp_path, monkeypatch):
    """I12: _cleanup sets bus_stop and joins bus_thread before server.shutdown()."""
    inv = tmp_path / "bus_inventory.json"
    inv.write_text(json.dumps({"hosts": [{"host": "h1", "node_id": "n1",
                                          "key_id": "k1",
                                          "key_file": str(tmp_path / "h1.key")}],
                               "agents": {}, "port": 8765}), encoding="utf-8")
    monkeypatch.setenv("PLANGRAPH_BUS_INVENTORY", str(inv))
    monkeypatch.setattr(os, "_exit", lambda code: None)  # never kill the worker
    w, cfg = _world(tmp_path, "bus")                     # worlds before the
    w2, cfg2 = _world(tmp_path, "nobus")                 # open_db stub below
    monkeypatch.setattr(kdb, "lock_keeper", lambda *a, **kw: _FakeLock())
    conn = {"now": w.conn}
    monkeypatch.setattr(kdb, "open_db", lambda *a, **kw: conn["now"])
    order, threads, spies = [], [], []

    def spy(server):
        assert server.bus_thread is not None      # the real registry started
        threads.append(server.bus_thread)
        spies.append(_JoinSpy(server.bus_thread))
        server.bus_thread = spies[-1]
        real_shutdown = server.shutdown

        def recording_shutdown():
            order.append(server.bus_stop.is_set())
            real_shutdown()

        server.shutdown = recording_shutdown

    rc, _ = _run_with_backstop(cfg, spy, 2.5)
    assert rc == 0                                # positive outcome: clean exit
    assert spies[0].joins == [2]                  # F3: bounded join, before shutdown
    assert order == [True]                        # F3: bus_stop already set there
    threads[0].join(timeout=5)
    assert not threads[0].is_alive()              # the registry loop really stopped
    with suppress(Exception):
        w.close()
    # positive control: same setup without the registry -> exit 0, no fatal
    monkeypatch.delenv("PLANGRAPH_BUS_INVENTORY", raising=False)
    conn["now"] = w2.conn
    seen = []
    rc, _ = _run_with_backstop(cfg2, seen.append, 1.2)
    assert rc == 0
    assert seen[-1].fatal_reason is None
    assert seen[-1].bus_thread is None            # nothing to stop or join
    with suppress(Exception):
        w2.close()
