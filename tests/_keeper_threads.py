"""Thread-identity leak checks shared by the public fixtures and their tests."""
from contextlib import contextmanager
import threading


@contextmanager
def guard_new_threads():
    """Ignore pre-existing threads; bounded-join and report new live threads."""
    before = set(threading.enumerate())
    try:
        yield
    finally:
        leaked = [thread for thread in threading.enumerate() if thread not in before]
        for thread in leaked:
            thread.join(timeout=5)
        leaked = [thread for thread in leaked if thread.is_alive()]
        assert not leaked, "Leaked keeper threads: " + ", ".join(
            f"{thread.name} (daemon={thread.daemon})" for thread in leaked)
