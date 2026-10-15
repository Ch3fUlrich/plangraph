"""Backup retention for the keeper data dir (PG-BACKUP-RETENTION).

Three layers, from pure to effectful:

* :func:`plan` - a pure planner (no I/O, no clock): which backup entries to
  keep and which to delete.
* :func:`is_backup_name` / :func:`scan` - the only filesystem read.  A name is
  a backup only if it fully matches one of :data:`BACKUP_PATTERNS` and none of
  the deny-list rules (:func:`is_denied`) applies.  The deny-list is checked
  first and always wins.
* :func:`prune` - scan, plan, report, and (only with ``apply=True``) unlink.

There is deliberately NO default directory anywhere in this module: the caller
names the directory every time, and ``/``, an empty string or a missing
directory is refused.  Dry-run is the default (``apply=False``).
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple

__all__ = ["BACKUP_PATTERNS", "Entry", "Plan", "PruneResult", "is_backup_name",
           "is_denied", "plan", "scan", "prune"]

_TAIL = r"[A-Za-z0-9._-]+"

# The one allow-list.  Anchored, full-match only (re.fullmatch).
BACKUP_PATTERNS = (
    re.compile(r"backup-pre-" + _TAIL + r"\.db"),
    re.compile(r"keeper\.db\.pre-" + _TAIL),
    re.compile(r"keeper\.db\.bak-" + _TAIL),
    re.compile(r"pre-" + _TAIL + r"\.db"),
    re.compile(r"keeper-pre-" + _TAIL + r"\.db"),
)

_DENY_EXACT = frozenset({"keeper.db", "keeper.json"})
_DENY_PREFIX = ("keeper.json.", "plan.snapshot", "md-sync")
_DENY_SUFFIX = (".token", "-wal", "-shm", ".lock", ".pid", ".alive", ".log")


class Entry(NamedTuple):
    name: str
    mtime: float
    size: int


@dataclass
class Plan:
    keep: list = field(default_factory=list)     # newest first
    delete: list = field(default_factory=list)   # sorted by name

    @property
    def bytes_to_free(self) -> int:
        return sum(e[2] for e in self.delete)


@dataclass
class PruneResult:
    plan: Plan
    applied: bool
    deleted: list = field(default_factory=list)   # names actually unlinked
    failed: list = field(default_factory=list)    # (name, reason)
    du_before: int = 0
    du_after: int = 0                             # projected when not applied

    @property
    def ok(self) -> bool:
        return not self.failed


# ------------------------------------------------------------------ names
def is_denied(name: str) -> bool:
    """True for every file the keeper itself needs; checked before the allow-list."""
    return (name in _DENY_EXACT
            or name.startswith(_DENY_PREFIX)
            or name.endswith(_DENY_SUFFIX))


def is_backup_name(name: str) -> bool:
    """True iff *name* is a known ad-hoc backup file name (deny-list wins)."""
    if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
        return False
    if is_denied(name):
        return False
    return any(p.fullmatch(name) for p in BACKUP_PATTERNS)


# ---------------------------------------------------------------- planner
def plan(entries, keep, daily_days, now, tz=timezone.utc) -> Plan:
    """Pure retention plan over ``(name, mtime_epoch_s, size_bytes)`` entries.

    Kept: the newest *keep* files by mtime (ties broken by name, ascending),
    plus, for each of the last *daily_days* calendar days in *tz* (UTC by
    default), the newest file of that day.  The day window is
    ``today - (daily_days - 1) .. today`` where ``today`` is the day of *now*;
    a file's day is the day of its mtime.  ``daily_days=0`` disables the daily
    buckets, ``keep=0`` disables the newest-N rule.  A file whose mtime is
    after *now* is always kept, and counts toward neither rule.  Everything
    else is deleted.
    """
    if keep < 0:
        raise ValueError("keep must be >= 0")
    if daily_days < 0:
        raise ValueError("daily_days must be >= 0")
    items = [Entry(*e) for e in entries]
    future = [e for e in items if e.mtime > now]
    past = sorted((e for e in items if e.mtime <= now),
                  key=lambda e: (-e.mtime, e.name))
    kept = {e.name for e in past[:keep]}
    if daily_days:
        today = datetime.fromtimestamp(now, tz).date()
        first = today - timedelta(days=daily_days - 1)
        seen = set()
        for e in past:                      # newest first: first hit per day wins
            day = datetime.fromtimestamp(e.mtime, tz).date()
            if first <= day <= today and day not in seen:
                seen.add(day)
                kept.add(e.name)
    kept_entries = sorted(future + [e for e in past if e.name in kept],
                          key=lambda e: (-e.mtime, e.name))
    doomed = sorted((e for e in past if e.name not in kept), key=lambda e: e.name)
    return Plan(keep=kept_entries, delete=doomed)


# --------------------------------------------------------------------- I/O
def _check_dir(dir) -> str:
    if dir is None or not str(dir).strip():
        raise ValueError("backup dir is required (empty)")
    path = os.fspath(dir)
    if os.path.realpath(path) == os.path.realpath(os.sep):
        raise ValueError("refusing to operate on the filesystem root")
    if not os.path.isdir(path):
        raise ValueError(f"backup dir does not exist: {path}")
    return path


def scan(dir) -> list:
    """Backup entries directly inside *dir* (the only I/O in the read path).

    Non-recursive on purpose: sub-directories (for example a ``backups/``
    sub-directory) are never entered or listed - recursion is out of scope.
    Symlinks, non-regular files and names that are not backups are skipped.
    """
    path = _check_dir(dir)
    out = []
    for name in sorted(os.listdir(path)):
        if not is_backup_name(name):
            continue
        try:
            st = os.lstat(os.path.join(path, name))
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            continue
        out.append(Entry(name, st.st_mtime, st.st_size))
    return out


def _du(path: str) -> int:
    total = 0
    for name in os.listdir(path):
        try:
            st = os.lstat(os.path.join(path, name))
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
    return total


def _unlink_checked(path: str, name: str) -> str | None:
    """Unlink one planned file after re-checks; returns a failure reason or None."""
    full = os.path.join(path, name)
    if not is_backup_name(name):
        return "not a backup name"
    try:
        st = os.lstat(full)
    except FileNotFoundError:
        return None                          # already gone: nothing to do
    except OSError as exc:
        return str(exc)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if os.path.realpath(os.path.dirname(full)) != os.path.realpath(path):
        return "outside the backup dir"
    try:
        os.unlink(full)
    except OSError as exc:
        return str(exc)
    return None


def prune(dir, keep, daily_days, apply=False, now=None,
          out: Callable[[str], None] = print) -> PruneResult:
    """Scan *dir*, plan, report; delete the plan only when ``apply`` is true."""
    path = _check_dir(dir)
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    entries = scan(path)
    pl = plan(entries, keep, daily_days, now)
    du_before = _du(path)
    free = pl.bytes_to_free
    out(("APPLY" if apply else "DRY RUN (no files deleted; pass --apply)"))
    out(f"policy: keep newest {keep} + newest of each of the last "
        f"{daily_days} UTC days")
    out(f"kept {len(pl.keep)}, delete {len(pl.delete)}, bytes to free {free}")
    for e in pl.delete:
        out(f"  delete {e.name} ({e.size} bytes)")
    res = PruneResult(plan=pl, applied=apply, du_before=du_before)
    if apply:
        for e in pl.delete:
            reason = _unlink_checked(path, e.name)
            if reason is None:
                res.deleted.append(e.name)
            else:
                res.failed.append((e.name, reason))
                out(f"  FAILED {e.name}: {reason}")
        res.du_after = _du(path)
    else:
        res.du_after = du_before - free
    out(f"du before: {res.du_before} bytes")
    out(f"du after: {res.du_after} bytes" + ("" if apply else " (projected)"))
    if apply:
        out(f"deleted {len(res.deleted)}, failed {len(res.failed)}")
    return res
