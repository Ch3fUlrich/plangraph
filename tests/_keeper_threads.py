"""Thread-identity leak checks shared by the public fixtures and their tests."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import sys
import threading
from time import monotonic
from unittest.mock import patch

import pytest

_TESTS = Path(__file__).resolve().parent
THREAD_SHUTDOWN_TIMEOUT = 10.0
JOIN_INTERVAL = 1.0
_FIXTURE_REQUEST = ContextVar("keeper_fixture_request", default=None)
_TEST_REQUEST = ContextVar("keeper_test_request", default=None)


@contextmanager
def thread_scope(request, *, fixture=False):
    """Fixture setup overrides the current test, including dynamic fixture requests."""
    context = _FIXTURE_REQUEST if fixture else _TEST_REQUEST
    token = context.set(request)
    try:
        yield
    finally:
        context.reset(token)


def current_scope(session_request):
    return (_FIXTURE_REQUEST.get() or _TEST_REQUEST.get() or session_request).node


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    with thread_scope(request, fixture=True):
        return (yield)


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
def guard_new_threads(*, timeout=THREAD_SHUTDOWN_TIMEOUT, scope=None):
    """Join owned starts/descendants at context exit or their pytest scope's teardown."""
    started, groups = {}, {}
    lock = threading.RLock()
    original_start = threading.Thread.start

    def finish(owner):
        group = groups[owner]
        try:
            _join_threads(group, lock, timeout)
        finally:
            with lock:
                groups.pop(owner)
                for thread in group:
                    started.pop(thread, None)

    def watch_scope(owner):
        with lock:
            if owner not in groups:
                groups[owner] = set()
                if owner is not None:
                    owner.addfinalizer(lambda: finish(owner))
            return groups[owner]

    def start(thread, *args, **kwargs):
        with lock:
            parent = threading.current_thread()
            owned = parent in started or _keeper_start()
            if owned:
                group = started[parent] if parent in started else watch_scope(
                    scope() if scope is not None else None)
                group.add(thread)  # before start: descendants inherit scope ownership
                started[thread] = group
        try:
            return original_start(thread, *args, **kwargs)
        except BaseException:
            if owned:
                with lock:
                    group.discard(thread)
                    started.pop(thread, None)
            raise

    with patch.object(threading.Thread, "start", start):
        try:
            yield watch_scope
        finally:
            for owner in list(groups):
                finish(owner)


def _join_threads(started, lock, timeout):
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
