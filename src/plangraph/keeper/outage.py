"""M2B-OUTBOX: keeper liveness stamp + the start-up outage check.

The reaper touches ``<db dir>/keeper.alive`` every interval.  At start the daemon
compares its mtime with now: a gap longer than two reap intervals (+30 s) is an
outage, recorded by ``Store.record_outage`` (leases + live run tokens pushed out by
the gap, one ``outage_recorded`` event).  No file = first start = no outage.
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = ["alive_path", "stamp", "check_and_record", "OUTAGE_MAX_S"]

#: Never extend leases/tokens by more than this, whatever the stamp says (a forged or
#: ancient stamp, clock skew): the run-token hard cap is 24 h anyway.
OUTAGE_MAX_S = 86400


def alive_path(cfg) -> Path:
    return Path(cfg.db).parent / "keeper.alive"


def stamp(cfg) -> None:
    path = alive_path(cfg)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        os.utime(path)
    except OSError:
        pass


def _trusted(st) -> bool:
    """Only our own, not group/other-writable stamp counts (the data dir is 0700 too)."""
    return st.st_uid == os.getuid() and not (st.st_mode & 0o022)


def check_and_record(store, system, cfg, now=None):
    """Record the outage since the last stamp (if any); returns the store result or None."""
    path = alive_path(cfg)
    now = store.now() if now is None else float(now)
    try:
        st = path.stat()
    except OSError:
        stamp(cfg)
        return None
    last = st.st_mtime
    if not _trusted(st) or last > now:        # tampered or from the future: no grace at all
        stamp(cfg)
        return None
    gap = min(now - last, OUTAGE_MAX_S)
    limit = 2 * float(getattr(cfg, "reap_interval", 60)) + 30
    result = None
    if gap > limit:
        result = store.record_outage(system, int(gap), last, now)
    stamp(cfg)
    return result
