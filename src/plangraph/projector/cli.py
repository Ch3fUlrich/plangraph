"""``python3 -m plangraph.projector``: project Temporal histories into the keeper.

The keeper is opened in-process from the same ``keeper.json`` the keeper CLI and daemon read
(backend sqlite/postgres by config or ``PG_KEEPER_BACKEND``) and written as the ``system``
principal, exactly like the daemon's reaper: no token is read, minted or printed.  Exit codes:
0 ok, 2 usage/config error, 1 runtime failure.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import signal
import sys
import threading
from pathlib import Path

from plangraph.keeper import config as config_mod, db as kdb
from plangraph.keeper.auth import Principal
from plangraph.keeper.store import Store

from .apply import GLOBAL_WATERMARK, Projector, project_source
from .source import FileSource

__all__ = ["main"]

DEFAULT_INTERVAL_S = 30.0
_SECRET = re.compile(r"(\w+://\S+|(?:password|passwd|token|secret)\s*[=:]\s*\S+)", re.I)


class UsageError(Exception):
    """Bad arguments or configuration (exit 2)."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m plangraph.projector",
        description="Project Temporal workflow histories into the keeper.")
    parser.add_argument("--config", default=None,
                        help="keeper config path (default: env PG_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, *, open_only=True):
        p.add_argument("--history-dir", default=None,
                       help="read recorded history files instead of a Temporal server")
        if open_only:
            p.add_argument("--open-only", action="store_true",
                           help="project only workflows that are still running")

    common(sub.add_parser("run-once", help="one projection pass"))
    run = sub.add_parser("run", help="projection passes in a loop until SIGTERM")
    common(run)
    run.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                     help="seconds between passes (default 30)")
    common(sub.add_parser("rebuild", help="project every run again (idempotent)"), open_only=False)
    common(sub.add_parser("reconcile", help="compare open workflows with keeper state"),
           open_only=False)
    sub.add_parser("watermark", help="print the global watermark row")
    return parser


def _clean(text: str) -> str:
    return _SECRET.sub("<redacted>", text)


def _open_store(args):
    path = args.config or os.environ.get("PG_CONFIG")
    if not path:
        raise UsageError("config: --config or PG_CONFIG is required")
    try:
        cfg = config_mod.load_config(path)
    except config_mod.ConfigError as exc:
        raise UsageError(f"config: {_clean(str(exc))}") from None
    conn = kdb.open_db(cfg.db, **cfg.db_options)
    return conn, Store(conn, ctx=config_mod.verifier_context(cfg), batch_roots=cfg.batch_roots)


def _source(args):
    if args.history_dir:
        if not Path(args.history_dir).is_dir():
            raise UsageError("--history-dir is not a directory")
        return FileSource(args.history_dir)
    from .temporal_source import TemporalSource
    return TemporalSource()


def _emit(doc) -> None:
    print(json.dumps(doc, sort_keys=True, default=str), flush=True)


def _report_dict(report) -> dict:
    if hasattr(report, "as_dict"):
        return report.as_dict()
    if dataclasses.is_dataclass(report):
        return dataclasses.asdict(report)
    return dict(vars(report))


def _watermark(store):
    row = store.conn.execute(
        "SELECT * FROM projector_watermark WHERE workflow_id=?", (GLOBAL_WATERMARK,)).fetchone()
    return None if row is None else {k: row[k] for k in row.keys()}


def _pass(args, source, projector) -> dict:
    return project_source(source, projector, open_only=bool(getattr(args, "open_only", False)))


def _loop(args, source, projector) -> int:
    stop = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, name, None)
            if sig is not None:
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            try:
                _emit({"ok": True, **_pass(args, source, projector)})
            except Exception as exc:  # noqa: BLE001 - one failed pass must not end the loop
                print(f"projector: pass failed: {type(exc).__name__}: {_clean(str(exc))}",
                      file=sys.stderr)
            stop.wait(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def _dispatch(args) -> int:
    if args.command == "run" and not args.interval > 0:
        raise UsageError("--interval must be greater than 0")
    source = None if args.command == "watermark" else _source(args)
    conn, store = _open_store(args)
    try:
        if args.command == "watermark":
            _emit(_watermark(store))
            return 0
        projector = Projector(store, Principal("system", "system", [], None))
        if args.command in ("run-once", "rebuild"):
            _emit({"ok": True, **_pass(args, source, projector)})
            return 0
        if args.command == "run":
            return _loop(args, source, projector)
        from .reconcile import open_alarms, reconcile   # lands with the reconciler
        report = reconcile(store, source, projector)
        _emit({"ok": True, **_report_dict(report), "open_alarms": len(open_alarms(store))})
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        return _dispatch(args)
    except UsageError as exc:
        print(f"projector: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level: one line, never a DSN
        print(f"projector: {type(exc).__name__}: {_clean(str(exc))}", file=sys.stderr)
        return 1
