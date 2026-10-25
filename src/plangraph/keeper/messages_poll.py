"""Bounded long polling, with a dedicated unpooled Postgres LISTEN session."""
import time
from contextlib import ExitStack

from .errors import Refused


def read_messages(server, principal, query):
    filters = {k: v[0] for k, v in query.items() if k != "wait_ms"}
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
            import psycopg
            # Connect directly, never through a transaction pool. LISTEN precedes
            # the first read so a commit between read and wait cannot disappear.
            listener = stack.enter_context(psycopg.connect(conn._raw.info.dsn, autocommit=True))
            listener.execute("LISTEN keeper_messages")
        deadline = time.monotonic() + wait / 1000
        while True:
            rows = server.store.messages(principal, **filters)
            remaining = deadline - time.monotonic()
            if rows or remaining <= 0:
                return {"rows": rows}
            # Release the keeper lock while waiting so POST can commit. Always
            # re-read at least every 250ms, including when NOTIFY is lost.
            server.lock.release()
            try:
                if listener:
                    for _ in listener.notifies(timeout=min(remaining, .25), stop_after=1):
                        break
                else:
                    time.sleep(min(remaining, .025))
            finally:
                server.lock.acquire()
