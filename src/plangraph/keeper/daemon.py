"""Keeper daemon: run the HTTP API with the singleton flock.

``python -m plangraph.keeper.daemon --config C`` opens the configured DB,
takes the exclusive ``db.lock_keeper`` lock (so only one writer runs per host),
serves the API, and runs the reaper.  ``SIGTERM``/``SIGINT`` shut the server
down cleanly and release the lock.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from contextlib import suppress

from . import anchor, api, config as config_mod, db as kdb, outage
from .errors import KeeperLocked
from .store import Store

__all__ = ["main", "run", "SHUTDOWN_TIMEOUT"]

SHUTDOWN_TIMEOUT = 5.0


def _install_signals(stop: threading.Event) -> None:
    def handler(signum, frame):  # noqa: ARG001 - stdlib signature
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, handler)


def _backup_prune_loop(settings: dict, stop: threading.Event, log=None) -> None:
    """PG-BACKUP-RETENTION hook body: prune once now, then every ``interval_s``.

    Exits promptly when *stop* is set; any exception is logged and never
    propagates (a retention problem must not take the keeper down).
    """
    from . import backup_retention
    if log is None:
        def log(msg):
            print(msg, file=sys.stderr)
    while not stop.is_set():
        try:
            res = backup_retention.prune(
                settings["dir"], settings["keep"], settings["daily_days"],
                apply=True, out=lambda _line: None)
            log(f"backup_prune: deleted {len(res.deleted)}, "
                f"failed {len(res.failed)}, kept {len(res.plan.keep)}")
        except Exception as exc:  # noqa: BLE001 - never crash the keeper
            log(f"backup_prune: error: {exc}")
        if stop.wait(settings["interval_s"]):
            break


def run(cfg, *, host=None, port=None, ready=None):
    """Start the server and block until a signal; returns 0 on clean shutdown."""
    lock = kdb.lock_keeper(cfg.db, **cfg.db_options)
    conn = kdb.open_db(cfg.db, **cfg.db_options)
    try:
        store = Store(conn, ctx=config_mod.verifier_context(cfg),
                      batch_roots=cfg.batch_roots)
        server = api.make_server(store, cfg, host=host, port=port)
        if anchor.enabled(cfg):   # M2B-ANCHOR: refuse a rolled-back or forked DB before serving
            server.anchorer = anchor.Anchorer(cfg, last=anchor.check_start(conn, cfg))
        outage.check_and_record(store, server.system, cfg)   # M2B-OUTBOX: freeze clocks over a downtime
        if server.anchorer is not None:
            server.anchorer.record(conn, "start")
        server.start_reaper()
        try:   # KEEPER-SNAPSHOT-PG: a crash may have skipped the debounced rewrite; re-derive it
            with server.lock:      # the reaper is already running: same lock as every other build
                api.write_snapshot(server, server.system)
        except Exception as exc:  # noqa: BLE001 - never block startup on the derived file
            print(f"keeper: startup snapshot failed: {type(exc).__name__}", file=sys.stderr)
        stop = threading.Event()
        server.on_fatal = stop.set
        _install_signals(stop)
        thread = threading.Thread(target=server.serve_forever, daemon=True,
                                  name="keeper-http")
        thread.start()
        prune_thread = None
        bp = getattr(cfg, "backup_prune", None) or {}
        if bp.get("enabled"):   # opt-in; default off
            prune_thread = threading.Thread(
                target=_backup_prune_loop, args=(bp, stop),
                daemon=True, name="keeper-backup-prune")
            prune_thread.start()
        if ready is not None:
            ready(server)
        try:
            while not stop.wait(1.0):
                if getattr(server, "fatal_reason", None):
                    # F1: fail() may land while on_fatal is unwired (pre-run()
                    # construction, or a hook that raised): notice it ourselves.
                    break
                alive = getattr(lock, "alive", None)
                if alive and not alive():
                    server.fail("postgres keeper lock session lost")
                    break
        finally:
            def _cleanup():
                stop.set()
                if prune_thread is not None:
                    prune_thread.join(timeout=5)
                server.shutdown()
                server.stop_reaper()
                server.server_close()
                thread.join(timeout=5)

            cleaner = threading.Thread(
                target=_cleanup, daemon=True, name="keeper-shutdown")
            cleaner.start()
            cleaner.join(timeout=SHUTDOWN_TIMEOUT)
            if getattr(server, "fatal_reason", None):
                if cleaner.is_alive():
                    os._exit(75)
                return 75
            if cleaner.is_alive():
                os._exit(0)
            return 0
    finally:
        with suppress(Exception):
            conn.close()
        with suppress(Exception):
            lock.release()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="plangraph.keeper.daemon", description="Run the keeper HTTP API.")
    parser.add_argument("--config", required=True, help="path to the JSON config")
    parser.add_argument("--host", default=None, help="override the bind host")
    parser.add_argument("--port", type=int, default=None,
                        help="override the bind port")
    args = parser.parse_args(argv)
    try:
        cfg = config_mod.load_config(args.config)
    except config_mod.ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 1
    try:
        return run(cfg, host=args.host, port=args.port)
    except KeeperLocked as exc:
        print(f"keeper locked: {exc}", file=sys.stderr)
        return 1
    except anchor.AnchorError as exc:
        print(f"keeper: REFUSED by the anchor log: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"keeper: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
