"""Debounced, coalescing ``plan.snapshot.json`` writer (KEEPER-SNAPSHOT-PG).

The snapshot is derived state: the write that changed the plan is committed in the database
before the HTTP response, the file is rebuilt from the database afterwards.  Rebuilding on every
write put an O(items) read on the request path (PG: ~870 ms per write at 1000 items).  Here a
write only ``mark()``s the writer; one thread rebuilds once the writes went quiet for
``debounce_s``, and never later than ``max_delay_s`` after the first unwritten change, so a
steady stream of writes cannot starve the file.

Contract (bounded staleness): the file lags the database by at most ``max_delay_s`` plus one
build while the keeper runs; ``flush()`` (shutdown, tests) brings it level before returning; a
crash loses nothing committed because the startup rebuild (daemon) re-derives it from the
database.  Rewrites stay atomic (temp file + rename; signature first, see snapshot_io).
``debounce_s <= 0`` keeps the old behaviour: the request thread rewrites it synchronously.
"""
from __future__ import annotations

import sys
import threading
import time

from .snapshot_io import write_snapshot

__all__ = ["SnapshotWriter", "ALARM_AFTER"]

#: Consecutive failed builds before an ALARM line (a single failure retries quietly-ish: one line).
ALARM_AFTER = 3


def _log(message):
    print(f"keeper: {message}", file=sys.stderr, flush=True)


class SnapshotWriter(threading.Thread):
    def __init__(self, server, debounce_s, max_delay_s):
        super().__init__(name="keeper-snapshot", daemon=True)
        self.server = server
        self.debounce_s = float(debounce_s)
        self.max_delay_s = max(float(max_delay_s), self.debounce_s)
        self._cond = threading.Condition()
        self._gen = 0              # bumped by every mark()
        self._written = 0          # the _gen the file reflects
        self._first = None         # monotonic time of the oldest unwritten mark
        self._last = None          # monotonic time of the newest mark
        self._halt = False
        self.failures = 0          # consecutive failed builds (resets on success)
        self.alarm = False         # up once failures reach ALARM_AFTER, until a build succeeds

    def mark(self):
        """A change was committed (caller holds ``server.lock``): schedule a rebuild."""
        with self._cond:
            now = time.monotonic()
            self._gen += 1
            self._last = now
            if self._first is None:
                self._first = now
            self._cond.notify_all()

    def _write_locked(self):
        """Rebuild under the server lock; True when the file now reflects the database."""
        with self.server.lock:     # writes mark() under this lock, so ``gen`` is exact
            with self._cond:
                gen = self._gen
            try:
                write_snapshot(self.server, self.server.system)
            except Exception as exc:   # noqa: BLE001 - a committed write stays committed; retry later
                self._failed(exc)
                return False
            self._succeeded()
        with self._cond:
            if gen >= self._written:
                self._written = gen
            if self._written == self._gen:
                self._first = self._last = None
            else:
                self._first = self._last = time.monotonic()
        return True

    def _failed(self, exc):
        """Log one line per failed build (exception type only: messages can carry paths)."""
        self.failures += 1
        _log(f"snapshot build failed ({type(exc).__name__}), "
             f"{self.failures} consecutive; plan.snapshot.json is stale, retrying")
        if self.failures == ALARM_AFTER:
            self.alarm = True
            _log(f"ALARM snapshot writer failed {ALARM_AFTER} consecutive builds: "
                 "plan.snapshot.json lags the database; check disk space, permissions, signing key")

    def _succeeded(self):
        if self.failures:
            _log(f"snapshot build recovered after {self.failures} failed attempts")
        self.failures, self.alarm = 0, False

    def run(self):
        while True:
            with self._cond:
                while not self._halt and self._written == self._gen:
                    self._cond.wait()
                if self._halt:
                    return
                due = min(self._last + self.debounce_s, self._first + self.max_delay_s)
                wait = due - time.monotonic()
                if wait > 0:
                    self._cond.wait(wait)      # more marks move ``due`` out; re-evaluate
                    continue
            if not self._write_locked():
                with self._cond:               # failed build: back off one debounce, then retry
                    self._first = self._last = time.monotonic()

    def flush(self):
        """Synchronously bring the file level with the database (raises on a write error)."""
        with self.server.lock:
            with self._cond:
                gen, pending = self._gen, self._written != self._gen
            if not pending:
                return
            write_snapshot(self.server, self.server.system)
            self._succeeded()
        with self._cond:
            self._written = max(self._written, gen)
            if self._written == self._gen:
                self._first = self._last = None

    def stop(self):
        """Flush what is pending, then end the thread."""
        try:
            self.flush()
        finally:
            with self._cond:
                self._halt = True
                self._cond.notify_all()
            if self.is_alive() and threading.current_thread() is not self:
                self.join(timeout=5)
