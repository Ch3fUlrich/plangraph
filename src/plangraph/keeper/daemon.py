"""Keeper daemon: run the HTTP API with the singleton flock.

``python -m plangraph.keeper.daemon --config C`` opens the configured DB,
takes the exclusive ``db.lock_keeper`` lock (so only one writer runs per host),
serves the API, and runs the reaper.  ``SIGTERM``/``SIGINT`` shut the server
down cleanly and release the lock.
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading

from . import api, config as config_mod, db as kdb, outage
from .errors import KeeperLocked
from .store import Store

__all__ = ["main", "run"]


def _install_signals(stop: threading.Event) -> None:
    def handler(signum, frame):  # noqa: ARG001 - stdlib signature
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, handler)


def run(cfg, *, host=None, port=None, ready=None):
    """Start the server and block until a signal; returns 0 on clean shutdown."""
    lock = kdb.lock_keeper(cfg.db)
    conn = kdb.open_db(cfg.db)
    try:
        store = Store(conn, ctx=config_mod.verifier_context(cfg),
                      batch_roots=cfg.batch_roots)
        server = api.make_server(store, cfg, host=host, port=port)
        outage.check_and_record(store, server.system, cfg)   # M2B-OUTBOX: freeze clocks over a downtime
        server.start_reaper()
        stop = threading.Event()
        _install_signals(stop)
        thread = threading.Thread(target=server.serve_forever, daemon=True,
                                  name="keeper-http")
        thread.start()
        if ready is not None:
            ready(server)
        try:
            stop.wait()
        finally:
            server.shutdown()
            server.stop_reaper()
            server.server_close()
            thread.join(timeout=5)
        return 0
    finally:
        conn.close()
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
    except RuntimeError as exc:
        print(f"keeper: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
