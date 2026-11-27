"""Best-effort, payload-free keeper timings; clocks are replaceable in tests."""
from contextvars import ContextVar
from contextlib import contextmanager
from collections import deque
from functools import wraps
from concurrent.futures import Future, wait
from itertools import takewhile
from threading import Lock, RLock

from .errors import Refused
import sys
import time

clock = time.monotonic
ns_clock = time.monotonic_ns
THRESHOLD_MS = 250
DEFAULT_CLAIM_PREFLIGHT_S = 30
_LATENCY_VERBS = frozenset({"show", "ready", "snapshot", "messages", "close_merged", "close_merged_auto",
                            "claim", "claim_batch", "heartbeat", "heartbeat_batch", "set_priority"})
_parts = ContextVar("keeper_timing_parts", default=None)
_request_timer = ContextVar("keeper_request_timer", default=None)
_io_deadline = ContextVar("keeper_io_deadline", default=None)
_MS_NS = 1_000_000
_LONG_NS = 2_000 * _MS_NS
_RELEVANT = {"writer", "snapshot", "messages", "merge_refresh"}
_MAINTENANCE = {"writer_timing": "writer", "snapshot_timing": "snapshot",
                "reaper_timing": "reaper", "changelog_timing": "changelog"}
_RAW_LABELS = _RELEVANT | set(_MAINTENANCE.values()) | {"auth", "host_samples", "read_audit"}
_PHASES = {"auth", "dispatch", "reacquire", "maintenance"}

_inflight = {}
_inflight_lock = Lock()


def _lock_label(label):
    if not isinstance(label, str):
        return "unclassified"
    if label in _RAW_LABELS:
        return label
    # The route tables enumerate the only request labels, including the inbox verbs.
    from .api_routes import READ_VERBS, WRITE_VERBS
    return label if label in READ_VERBS or label in WRITE_VERBS else "unclassified"


def _lock_wait_line(phase, waiter, start, end, spans, evicted):
    totals = {}
    # Outermost ownership of one RLock makes these intervals disjoint.
    for left, right, label in spans:
        overlap = min(end, right) - max(start, left)
        if overlap > 0 and label != "unclassified":
            totals[label] = totals.get(label, 0) + overlap
    waited = (end - start) // _MS_NS
    covered = min(waited, sum(totals.values()) // _MS_NS)
    coverage = 10_000 * covered // waited if waited else 10_000
    reported = {label: total // _MS_NS for label, total in sorted(totals.items())}
    highest = max(reported.values(), default=0)
    primary = [label for label, total in reported.items() if total == highest and total > 0]
    holder = ("tie" if len(primary) > 1 else primary[0]) if primary else "none"
    overlaps = ",".join(f"{label}:{total}" for label, total in reported.items()) or "-"
    return (f"keeper: lock_wait phase={phase} waiter={waiter} wait_ms={waited} covered_ms={covered} "
            f"coverage_bp={coverage} overflow={int(evicted > start)} holder={holder} overlaps={overlaps}")


class KeeperLock:
    """Track outer RLock holds. Timing defaults on for direct use and off for HTTP servers."""

    def __init__(self, *, log_timing=True):
        self._lock = RLock()
        self._spans = deque(maxlen=512)
        self._evicted = 0
        self.log_timing = log_timing

    def depth(self):
        return self._lock._recursion_count()

    def _is_owned(self):
        return self._lock._is_owned()

    def acquire(self, blocking=True, timeout=-1, *, label=None, phase="maintenance", waiter=None):
        start = ns_clock()
        if not self._lock.acquire(blocking, timeout):
            return False
        if self.depth() == 1:
            end = ns_clock()
            self._holder = _lock_label(label)
            self._start = end
            self._wait = None
            # Sample every shared-DB auth attempt, including refusals and zero waits.
            if self.log_timing and (phase == "auth" or end - start > _LONG_NS):
                phase = phase if phase in _PHASES else "maintenance"
                spans = tuple(takewhile(lambda span: span[1] > start, reversed(self._spans)))
                self._wait = phase, _lock_label(waiter or label), start, end, spans, self._evicted
        return True

    def release(self):
        if self.depth() != 1:
            return self._lock.release()
        holder, start, end, waiting = self._holder, self._start, ns_clock(), self._wait
        if len(self._spans) == self._spans.maxlen:
            self._evicted = self._spans[0][1]
        self._spans.append((start, end, holder))
        self._lock.release()
        if self.log_timing and (holder in _RELEVANT or end - start > _LONG_NS):
            _output(f"keeper: lock_hold holder={holder} hold_ms={(end - start) // _MS_NS}")
        if waiting is not None:
            _output(_lock_wait_line(*waiting))

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


@contextmanager
def lock_scope(lock, label, *, phase="maintenance", waiter=None):
    """Time a labelled raw hold without changing the encompassing request totals."""
    if isinstance(lock, KeeperLock):
        lock.acquire(label=label, phase=phase, waiter=waiter)
        try:
            yield
        finally:
            lock.release()
    else:
        with lock:
            yield


def share_inflight(key, fetch, *, step="authority refresh"):
    """Share a running authority lookup, then discard its answer."""
    with _inflight_lock:
        entry = _inflight.get(key)
        if entry is None:
            pending = Future()
            _inflight[key] = pending, _io_deadline.get()
            leader = True
        else:
            pending, deadline = entry
            leader = False
    if not leader:
        remaining = DEFAULT_CLAIM_PREFLIGHT_S if deadline is None else max(0, deadline - time.monotonic())
        done, _ = wait((pending,), timeout=io_timeout(remaining))
        if not done:
            raise _io_refusal(step)
        result = pending.result()
        check_io_deadline(step)
        return result
    try:
        check_io_deadline(step)
        result = fetch()
        check_io_deadline(step)
    except BaseException as exc:
        with _inflight_lock:
            del _inflight[key]
            pending.set_exception(exc)
        raise
    else:
        with _inflight_lock:
            del _inflight[key]
            pending.set_result(result)
        return result


@contextmanager
def io_budget(seconds):
    """Keep subprocesses in a claim preflight within a common elapsed budget."""
    token = _io_deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _io_deadline.reset(token)


def io_timeout(seconds):
    deadline = _io_deadline.get()
    if deadline is None:
        return seconds
    return max(0, min(seconds, deadline - time.monotonic()))


def _io_refusal(step):
    return Refused("conflict", f"claim preflight exceeded its deadline during {step}")


def check_io_deadline(step):
    """Refuse an expired preflight with the step responsible for the delay."""
    deadline = _io_deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise _io_refusal(step)


@contextmanager
def outside_request_lock(lock=None):
    """Suspend the HTTP request lock for IO that never accesses the store."""
    timer = _request_timer.get()
    if timer is None or timer.lock is None or (lock is not None and timer.lock is not lock):
        yield
        return
    token = _request_timer.set(None)
    timer.release()
    try:
        yield
    finally:
        try:
            timer.acquire(phase="reacquire")
        finally:
            _request_timer.reset(token)


_fields = ("build_ms", "encode_ms", "sign_ms", "write_ms", "tick_ms", "record_ms", "submit_ms", "fleet_ms")
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
                or event == "request_timing" and kind in {"read", "write"}) and max(wait + hold, parts.get("fleet_ms", 0)) < THRESHOLD_MS:
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
    """Time a lock hold or an unlocked operation using the same logging setting."""

    def __init__(self, lock, kind="other", event="request_timing", *, verb=None, label=None, unlocked=False):
        self.lock, self.kind, self.event = None if unlocked else lock, kind, event
        self.log_timing = getattr(lock, "log_timing", True)
        self.verb = verb
        self.label = label or verb or _MAINTENANCE.get(event)

    def acquire(self, *, phase=None):
        if self.lock is None:
            return
        start = _sample()
        if isinstance(self.lock, KeeperLock):
            phase = phase or ("dispatch" if self.event == "request_timing" else "maintenance")
            self.lock.acquire(label=self.label, phase=phase)
        else:
            self.lock.acquire()
        self.held = _sample()
        elapsed = _ms(start, self.held)
        self.wait_ms = None if elapsed is None or self.wait_ms is None else self.wait_ms + elapsed

    def release(self):
        end = _sample()
        if self.lock is None:
            return end
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
                totals = self.wait_ms, self.hold_ms, getattr(self.parts, "total_ms", None)
                if self.log_timing and self.token is not None and None not in totals:
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
