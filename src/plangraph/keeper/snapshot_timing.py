"""KEEPER-SNAP-S0-TIMINGS: stderr timing lines (fixed names, non-negative ints).

One ``keeper: snapshot_timing ...`` line per ``Store._snapshot`` build and one
``keeper: anchor_timing ...`` line per anchor write that reached ``submit()``:
ms and counts only - never a decision id, decision text or an item key.
"""
from __future__ import annotations

import json
import sys
import time

__all__ = ["SnapshotTimer", "anchor_line", "emit"]


def _ms(seconds) -> int:
    return max(0, int(round(seconds * 1000)))


def emit(kind, fields, err=None):
    """One ``keeper: <kind>_timing`` line: fixed names, non-negative ints only."""
    line = "".join(f" {name}={max(0, int(value))}" for name, value in fields)
    (err or sys.stderr).write(f"keeper: {kind}_timing{line}\n")


class SnapshotTimer:
    """Phase timer for one build (clock injectable): ``run`` times a call,
    ``lap`` the work since the previous mark, ``count`` books counts, ``finish``
    serialises for ``bytes``, prints the one line, returns the snapshot unchanged.
    """

    def __init__(self, clock=None):
        self._clock = clock or time.perf_counter
        self._t0 = self._mark = self._clock()
        self.phases = []
        self.counts = {}

    def run(self, name, fn, *args, **kw):
        start = self._clock()
        try:
            return fn(*args, **kw)
        finally:
            now = self._clock()
            self.phases.append((name, _ms(now - start)))
            self._mark = now

    def lap(self, name):
        now = self._clock()
        self.phases.append((name, _ms(now - self._mark)))
        self._mark = now

    def count(self, name, value):
        self.counts[name] = int(value)

    def finish(self, snap, err=None):
        """Serialise once for ``bytes``, print the line, return *snap* unchanged."""
        start = self._clock()
        body = json.dumps(snap).encode("utf-8")
        self.phases.append(("serialise", _ms(self._clock() - start)))
        self.counts.setdefault("items", len(snap.get("items") or []))
        self.counts.setdefault("bytes", len(body))
        fields = [("total_ms", _ms(self._clock() - self._t0))]
        fields += [(f"{name}_ms", ms) for name, ms in self.phases]
        fields += list(self.counts.items())
        emit("snapshot", fields, err)
        return snap


def anchor_line(total_s, submit_s, ok, err=None):
    """One ``keeper: anchor_timing`` line for a tick that reached ``submit()``."""
    emit("anchor", [("total_ms", _ms(total_s)), ("submit_ms", _ms(submit_s)),
                    ("ok", 1 if ok else 0)], err)
