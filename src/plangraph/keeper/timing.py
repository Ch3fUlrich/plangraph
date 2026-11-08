"""Best-effort, payload-free keeper timings; clocks are replaceable in tests."""
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
import sys
import time

clock = time.monotonic
THRESHOLD_MS = 250
_parts = ContextVar("keeper_timing_parts", default=None)
_request_lock = ContextVar("keeper_request_lock", default=None)


@contextmanager
def outside_request_lock():
    """Suspend the HTTP request lock for IO that never accesses the store."""
    lock = _request_lock.get()
    if lock is not None:
        lock.release()
    try:
        yield
    finally:
        if lock is not None:
            lock.acquire()
_fields = ("build_ms", "encode_ms", "sign_ms", "write_ms", "tick_ms", "record_ms", "submit_ms")
_events = {"request_timing", "writer_timing", "reaper_timing", "snapshot_timing",
           "changelog_timing"}


def _now():
    try:
        return clock()
    except Exception:
        return None


def _ms(start, end):
    if start is None or end is None:
        return None
    return max(0, round((end - start) * 1000))


def _sample():
    try:
        return _now()
    except Exception:
        return None


class _Parts(dict):
    pending = None


def _context(value=None, token=None, get=False):
    try:
        return _parts.get() if get else (_parts.reset(token) if token is not None else _parts.set(value))
    except Exception:
        return None


def _output(line):
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass


def _emit(event, kind, wait, hold, parts):
    try:
        if event not in _events or kind not in {"read", "write", "other"}:
            return
        if parts is None:
            return
        values = (wait, hold, *(parts.get(field, 0) for field in _fields))
        if any(type(value) is not int or value < 0 for value in values):
            return  # a failed clock sample leaves None and its line is dropped here
        if (event in {"snapshot_timing", "changelog_timing", "reaper_timing"}
                or event == "request_timing" and kind in {"read", "write"}) and wait + hold < THRESHOLD_MS:
            return
        names = ("wait_ms", "hold_ms", *_fields)
        line = f"keeper: {event} kind={kind} " + " ".join(
            f"{name}={value}" for name, value in zip(names, values))
        if getattr(parts, "pending", None) is not None:
            parts.pending.append(line)
        else:
            _output(line)
    except Exception:
        pass  # closed stderr (or broken instrumentation) must never affect the operation


class RequestTimer:
    """Own the lock: wait starts at acquire, hold ends before release."""

    def __init__(self, lock, kind="other", event="request_timing"):
        self.lock, self.kind, self.event = lock, kind, event

    def __enter__(self):
        self.start = _sample()
        self.lock.acquire()
        self.lock_token = _request_lock.set(self.lock)
        self.held = _sample()
        self.parts = _Parts()
        self.parts.pending = []
        self.token = _context(self.parts)
        return self

    def __exit__(self, *exc):
        _request_lock.reset(self.lock_token)
        end = _sample()
        try:
            self.lock.release()
        finally:
            try:
                if self.token is not None and None not in (self.start, self.held, end):
                    _emit(self.event, self.kind, _ms(self.start, self.held),
                          _ms(self.held, end), self.parts)
                else:
                    self.parts.pending.clear()
            except Exception:
                pass
            finally:
                if self.token is not None:
                    _context(token=self.token)
                pending, self.parts.pending = self.parts.pending, None
                for line in pending:
                    _output(line)


def measured(event=None, part=None):
    """Finally-path decorator; nested parts also accrue to the enclosing cycle."""
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            parent = _context(get=True)
            parts = _Parts() if event else parent
            if event:
                parts.pending = getattr(parent, "pending", None)
            token = _context(parts)
            start = _sample()
            try:
                return fn(*args, **kwargs)
            finally:
                try:
                    elapsed = _ms(start, _sample())
                    if part and parts is not None:
                        previous = parts.get(part, 0)
                        parts[part] = None if elapsed is None or previous is None else previous + elapsed
                        if elapsed is None and parent is not None:
                            parent[part] = None
                    if event and token is not None and elapsed is not None:
                        _emit(event, "other", 0, elapsed, parts)
                except Exception:
                    pass
                finally:
                    if token is not None:
                        _context(token=token)
        return run
    return decorate


reaper_cycle = measured("reaper_timing", "tick_ms")
