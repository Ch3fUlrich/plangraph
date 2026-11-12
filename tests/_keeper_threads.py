"""Thread-identity leak checks shared by the public fixtures and their tests."""
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
from time import monotonic
from unittest.mock import patch

_TESTS = Path(__file__).resolve().parent
THREAD_SHUTDOWN_TIMEOUT = 10.0
JOIN_INTERVAL = 1.0


def _keeper_start():
    """Attribute the start call, not the target callable or the thread's name.

    Keeper frames own their callees too (e.g. an executor). Otherwise only a
    direct test/fixture start is owned: stdlib executor/event-loop frames count
    as runtime callers, even when a test submits the work that starts the pool.
    """
    frame = sys._getframe(1)
    caller = None
    try:
        while frame is not None:
            module = frame.f_globals.get("__name__", "")
            if module.startswith("plangraph.keeper."):
                return True
            if caller is None and module != __name__:
                caller = frame.f_code.co_filename
            frame = frame.f_back
        return caller is not None and Path(caller).resolve().is_relative_to(_TESTS)
    finally:
        del frame


@contextmanager
def guard_new_threads(*, timeout=THREAD_SHUTDOWN_TIMEOUT):
    """Join owned starts/descendants until stopped or the shared deadline expires."""
    started = set()
    lock = threading.Lock()
    original_start = threading.Thread.start

    def start(thread, *args, **kwargs):
        with lock:
            owned = threading.current_thread() in started or _keeper_start()
            if owned:
                started.add(thread)  # before start: descendants inherit ownership
        try:
            return original_start(thread, *args, **kwargs)
        except BaseException:
            if owned:
                with lock:
                    started.discard(thread)
            raise

    with patch.object(threading.Thread, "start", start):
        try:
            yield
        finally:
            deadline = monotonic() + timeout
            while True:
                with lock:
                    pending = [thread for thread in started if thread.is_alive()]
                if not pending or monotonic() >= deadline:
                    break
                for thread in pending:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    thread.join(timeout=min(JOIN_INTERVAL, remaining))
            with lock:
                leaked = sorted((thread for thread in started if thread.is_alive()),
                                key=lambda thread: thread.name)
            assert not leaked, "Leaked keeper threads: " + ", ".join(
                f"{thread.name} (daemon={thread.daemon})" for thread in leaked)
