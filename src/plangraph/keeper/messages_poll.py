"""Bounded long polling, with a dedicated unpooled Postgres LISTEN session."""
import os
import threading
import time
from contextlib import ExitStack, nullcontext

from . import db_errors, timing
from .errors import ConnectionLost, Refused

_LISTEN_SEMAPHORE = None
_LISTEN_LOCK = threading.Lock()


def _get_listen_semaphore():
    global _LISTEN_SEMAPHORE
    if _LISTEN_SEMAPHORE is None:
        with _LISTEN_LOCK:
            if _LISTEN_SEMAPHORE is None:
                cap_str = os.environ.get("PLANGRAPH_BUS_LISTEN_CAP") or os.environ.get("PLANGRAPH_LISTEN_CAP") or "16"
                try:
                    cap = int(cap_str)
                except ValueError:
                    cap = 16
                _LISTEN_SEMAPHORE = threading.BoundedSemaphore(cap)
    return _LISTEN_SEMAPHORE


def read_messages(server, principal, query, *, committed=False):
    filters = {k: v[0] for k, v in query.items() if k not in ("wait_ms", "fmt")}
    if not filters:
        raise Refused("validation", "messages needs a filter or since cursor")
    try:
        wait = int(query.get("wait_ms", ["0"])[0])
    except (ValueError, TypeError):
        raise Refused("validation", "invalid wait_ms") from None
    if not 0 <= wait <= 30000 or (wait and "since" not in filters):
        raise Refused("validation", "wait_ms requires since and range 0..30000")
    with timing.outside_request_lock(server.lock), ExitStack() as stack:
        listener = None
        if committed:
            postgres = server.read_schema is not None
            dsn = server.read_dsn if postgres else None
        else:
            with _legacy_read_lock(server):
                raw = getattr(server.store.conn, "_raw", None)
                postgres = raw is not None
                dsn = raw.info.dsn if postgres and wait else None
        deadline = time.monotonic() + wait / 1000
        if wait and postgres and dsn:
            sem = _get_listen_semaphore()
            if sem.acquire(blocking=False):
                stack.callback(sem.release)
                try:
                    import psycopg
                    # Connect directly, never through a transaction pool. LISTEN precedes
                    # the first read so a commit between read and wait cannot disappear.
                    options = {"connect_timeout": max(1, min(5, wait // 1000))} if committed else {}
                    listener = stack.enter_context(psycopg.connect(dsn, autocommit=True, **options))
                    listener.execute("LISTEN keeper_messages")
                except Exception:
                    listener = None
        while True:
            rows = _read(server, principal, filters, committed)
            remaining = deadline - time.monotonic()
            if rows or remaining <= 0:
                return {"rows": rows}
            # Periodic re-reads cover lost NOTIFY and the listener cap.
            if listener:
                for _ in listener.notifies(timeout=min(remaining, .25), stop_after=1):
                    break
            else:
                time.sleep(min(remaining, .25 if postgres else .025))


def _legacy_read_lock(server):
    if hasattr(server.lock, "__enter__"):
        # HTTP dispatch already times the whole poll. Each read only needs lock attribution.
        return timing.lock_scope(server.lock, "messages", phase="dispatch")
    return nullcontext()


def _read(server, principal, filters, committed):
    if not committed:
        with _legacy_read_lock(server):
            return server.store.messages(principal, **filters)
    try:
        with server.read_store(defer_audit=True) as store:
            return store.messages(principal, **filters)
    except (RuntimeError, ConnectionLost, *db_errors.OPERATIONAL) as exc:
        raise Refused("unavailable", "keeper read connection unavailable") from exc
