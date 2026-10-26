"""R2c: keeper fatal paths must compose with the bus registry (BUS-REGISTRY-PERSIST).

Covers I10 (fail() recorded before on_fatal is wired still ends daemon.run with
75), I11 (ConnectionLost in the bus persistence hook is fatal, other errors are
not) and I12 (_cleanup stops and joins the bus registry before server.shutdown).
Real keeper objects throughout; the only stand-ins are the DB seam (a store stub
raising a fake ConnectionLost) and the lock template's always-alive lock.
"""
import json
import os
import signal
import socket
import threading
import time
from contextlib import suppress

from keeper_util import World
from plangraph.keeper import config, daemon, db as kdb


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeLock:
    """Keeper lock that never expires and releases cleanly (template copy)."""

    def alive(self):
        return True

    def release(self):
        pass


def _world(tmp_path, name):
    """One seeded World plus its daemon config, isolated per daemon.run."""
    sub = tmp_path / name
    sub.mkdir(exist_ok=True)
    path = sub / "keeper.json"
    path.write_text(json.dumps({
        "db": str(sub / "keeper.db"),
        "snapshot": str(sub / "plan.snapshot.json"),
        "bind": f"127.0.0.1:{free_port()}"}), encoding="utf-8")
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
