"""M2B-OUTBOX: keeper liveness stamp + the start-up outage check.

The reaper touches ``<db dir>/keeper.alive`` every interval.  At start the daemon
compares its mtime with now: a gap longer than two reap intervals (+30 s) is an
outage, recorded by ``Store.record_outage`` (leases + live run tokens pushed out by
the gap, one ``outage_recorded`` event).  No file = first start = no outage.
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = ["alive_path", "stamp", "check_and_record"]


def alive_path(cfg) -> Path:
    return Path(cfg.db).parent / "keeper.alive"


def stamp(cfg) -> None:
    path = alive_path(cfg)
    try:
        path.touch()
    except OSError:
        pass


def check_and_record(store, system, cfg, now=None):
    """Record the outage since the last stamp (if any); returns the store result or None."""
    path = alive_path(cfg)
    now = store.now() if now is None else float(now)
    try:
        last = path.stat().st_mtime
    except OSError:
        stamp(cfg)
        return None
    gap = now - last
    limit = 2 * float(getattr(cfg, "reap_interval", 60)) + 30
    result = None
    if gap > limit:
        result = store.record_outage(system, int(gap), last, now)
    stamp(cfg)
    return result
