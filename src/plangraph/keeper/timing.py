"""Best-effort, payload-free keeper timings; clocks are replaceable in tests."""
from contextvars import ContextVar
from functools import wraps
import sys
import time

clock = time.monotonic
_parts = ContextVar("keeper_timing_parts", default=None)
_fields = ("build_ms", "encode_ms", "sign_ms", "write_ms", "tick_ms", "record_ms", "submit_ms")
_events = {"request_timing", "writer_timing", "reaper_timing", "snapshot_timing",
           "changelog_timing"}


def _now():
    try:
        return clock()
    except Exception:
        return 0.0


def _ms(start, end):
    return max(0, round((end - start) * 1000))


def _emit(event, kind, wait, hold, parts):
    try:
        if event not in _events or kind not in {"read", "write", "other"}:
            return
        if event == "request_timing" and kind in {"read", "write"} and wait + hold < 250:
            return
        values = (wait, hold, *(parts.get(field, 0) for field in _fields))
        if any(type(value) is not int or value < 0 for value in values):
            return
        names = ("wait_ms", "hold_ms", *_fields)
        line = f"keeper: {event} kind={kind} " + " ".join(
            f"{name}={value}" for name, value in zip(names, values))
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass  # closed stderr (or broken instrumentation) must never affect the operation


class RequestTimer:
    """Own the lock: wait starts at acquire, hold ends only after release."""

    def __init__(self, lock, kind="other", event="request_timing"):
        self.lock, self.kind, self.event = lock, kind, event

    def __enter__(self):
        self.start = _now()
        self.lock.acquire()
        self.held = _now()
        self.parts = {}
        self.token = _parts.set(self.parts)
        return self

    def __exit__(self, *exc):
        try:
            self.lock.release()
        finally:
            try:
                _emit(self.event, self.kind, _ms(self.start, self.held),
                      _ms(self.held, _now()), self.parts)
            except Exception:
                pass
            finally:
                _parts.reset(self.token)


def measured(event=None, part=None):
    """Finally-path decorator; nested parts also accrue to the enclosing cycle."""
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            parent = _parts.get()
            parts = {} if event else parent
            token = _parts.set(parts)
            start = _now()
            try:
                return fn(*args, **kwargs)
            finally:
                try:
                    elapsed = _ms(start, _now())
                    if part and parts is not None:
                        parts[part] = parts.get(part, 0) + elapsed
                    if event:
                        _emit(event, "other", 0, elapsed, parts)
                except Exception:
                    pass
                finally:
                    _parts.reset(token)
        return run
    return decorate


reaper_cycle = measured("reaper_timing", "tick_ms")
