"""Thread-identity leak checks shared by the public fixtures and their tests."""
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
from unittest.mock import patch

_TESTS = Path(__file__).resolve().parent


def _keeper_start():
    """Attribute the start call, not the target callable or the thread's name.

    Keeper frames own their callees too (e.g. an executor). Test/fixture code
    that starts a thread directly owns it, but a runtime reached through an
    event loop does not become keeper code just because a test drives the loop.
    """
    frame = sys._getframe(1)
    caller = None
    try:
        while frame is not None:
            module = frame.f_globals.get("__name__", "")
            if module.startswith("plangraph.keeper."):
                return True
            if (caller is None and module != __name__
                    and module.partition(".")[0] not in sys.stdlib_module_names):
                caller = frame.f_code.co_filename
            frame = frame.f_back
        return caller is not None and Path(caller).resolve().is_relative_to(_TESTS)
    finally:
        del frame


@contextmanager
def guard_new_threads():
    """Bounded-join only identities started by keeper/test code in this scope."""
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
            joined = set()
            while True:
                with lock:
                    pending = started - joined
                if not pending:
                    break
                for thread in pending:
                    if thread.is_alive():
                        thread.join(timeout=5)
                joined.update(pending)
            leaked = [thread for thread in joined if thread.is_alive()]
            assert not leaked, "Leaked keeper threads: " + ", ".join(
                f"{thread.name} (daemon={thread.daemon})" for thread in leaked)
