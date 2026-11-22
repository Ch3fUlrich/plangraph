"""Best-effort, payload-free keeper timings; clocks are replaceable in tests."""
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
import sys
import time

clock = time.monotonic
THRESHOLD_MS = 250
_LATENCY_VERBS = frozenset({"show", "heartbeat", "set_priority"})
_parts = ContextVar("keeper_timing_parts", default=None)
_request_timer = ContextVar("keeper_request_timer", default=None)


@contextmanager
def outside_request_lock():
    """Suspend the HTTP request lock for IO that never accesses the store."""
    timer = _request_timer.get()
    if timer is None:
        yield
        return
    token = _request_timer.set(None)
    timer.release()
    try:
        yield
    finally:
        try:
            timer.acquire()
        finally:
            _request_timer.reset(token)


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
        verb = getattr(parts, "verb", None)
        if verb not in _LATENCY_VERBS and (event in {"snapshot_timing", "changelog_timing", "reaper_timing"}
                or event == "request_timing" and kind in {"read", "write"}) and wait + hold < THRESHOLD_MS:
            return
        names = ("wait_ms", "hold_ms", *_fields)
        line = f"keeper: {event} kind={kind} " + " ".join(
            f"{name}={value}" for name, value in zip(names, values))
        if verb:
            query = parts.get("query_ms", 0)
            if type(query) is not int or query < 0:
                return
            line += (f" verb={verb} total_ms={parts.total_ms} query_ms={query}"
                     f" snapshot_write_ms={parts.get('write_ms', 0)}")
        if getattr(parts, "pending", None) is not None:
            parts.pending.append(line)
        else:
            _output(line)
    except Exception:
        pass  # closed stderr (or broken instrumentation) must never affect the operation


class RequestTimer:
    """Own the lock: wait starts at acquire, hold ends before release."""

    def __init__(self, lock, kind="other", event="request_timing", *, verb=None):
        self.lock, self.kind, self.event = lock, kind, event
        self.verb = verb

    def acquire(self):
        start = _sample()
        self.lock.acquire()
        self.held = _sample()
        elapsed = _ms(start, self.held)
        self.wait_ms = None if elapsed is None or self.wait_ms is None else self.wait_ms + elapsed

    def release(self):
        end = _sample()
        self.lock.release()
        elapsed = _ms(self.held, end)
        self.hold_ms = None if elapsed is None or self.hold_ms is None else self.hold_ms + elapsed
        return end

    def __enter__(self):
        self.start = _sample()
        self.wait_ms = self.hold_ms = 0
        self.acquire()
        self.lock_token = _request_timer.set(self)
        self.parts = _Parts()
        self.parts.verb = self.verb
        self.parts.pending = []
        self.token = _context(self.parts)
        return self

    def __exit__(self, *exc):
        _request_timer.reset(self.lock_token)
        try:
            end = self.release()
            self.parts.total_ms = _ms(self.start, end)
        finally:
            try:
                if self.token is not None and None not in (self.wait_ms, self.hold_ms,
                                                           getattr(self.parts, "total_ms", None)):
                    _emit(self.event, self.kind, self.wait_ms, self.hold_ms, self.parts)
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
