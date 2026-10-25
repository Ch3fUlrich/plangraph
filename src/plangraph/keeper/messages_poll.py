"""Bounded long polling, with a dedicated unpooled Postgres LISTEN session."""
import os
import threading
import time
from contextlib import ExitStack

from .errors import Refused

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


def read_messages(server, principal, query):
    filters = {k: v[0] for k, v in query.items() if k not in ("wait_ms", "fmt")}
    if not filters:
        raise Refused("validation", "messages needs a filter or since cursor")
    try:
        wait = int(query.get("wait_ms", ["0"])[0])
    except (ValueError, TypeError):
        raise Refused("validation", "invalid wait_ms") from None
    if not 0 <= wait <= 30000 or (wait and "since" not in filters):
        raise Refused("validation", "wait_ms requires since and range 0..30000")
    with ExitStack() as stack:
        listener = None
        conn = server.store.conn
        if wait and hasattr(conn, "_raw"):
            sem = _get_listen_semaphore()
            if sem.acquire(blocking=False):
                stack.callback(sem.release)
                try:
                    import psycopg
                    # Connect directly, never through a transaction pool. LISTEN precedes
                    # the first read so a commit between read and wait cannot disappear.
                    listener = stack.enter_context(psycopg.connect(conn._raw.info.dsn, autocommit=True))
                    listener.execute("LISTEN keeper_messages")
                except Exception:
                    listener = None
            else:
                # Over concurrent LISTEN cap: fall back to the 250ms re-read path
                listener = None
        deadline = time.monotonic() + wait / 1000
        while True:
            rows = server.store.messages(principal, **filters)
            remaining = deadline - time.monotonic()
            if rows or remaining <= 0:
                return {"rows": rows}
            # Release the keeper lock while waiting so POST can commit. Always
            # re-read at least every 250ms, including when NOTIFY is lost or over cap.
            server.lock.release()
            try:
                if listener:
                    for _ in listener.notifies(timeout=min(remaining, .25), stop_after=1):
                        break
                else:
                    sleep_time = min(remaining, 0.25 if hasattr(conn, "_raw") else 0.025)
                    time.sleep(sleep_time)
            finally:
                server.lock.acquire()
