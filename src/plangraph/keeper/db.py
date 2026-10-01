"""The one way the keeper opens its database.

``open_db`` owns the pragmas, the schema install (only with ``create=True``) and
the sqlite3 authorizer that denies DDL afterwards.  ``lock_keeper`` is the
singleton flock helper the API/server uses to keep one writer per host.
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
from pathlib import Path

from .errors import KeeperLocked
from .schema import SCHEMA

__all__ = ["open_db", "lock_keeper", "KeeperLock"]

_DEFAULT_BUSY_MS = 5000


def _denied_actions() -> set:
    names = [
        "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
        "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW", "SQLITE_ALTER_TABLE",
        "SQLITE_ATTACH", "SQLITE_DETACH",
    ]
    return {getattr(sqlite3, n) for n in names if hasattr(sqlite3, n)}


_DENY = _denied_actions()


def _authorizer(action, arg1, arg2, dbname, source):
    """Deny DDL, ``writable_schema``, ATTACH and ``load_extension``.

    Returning ``SQLITE_OK`` for everything else keeps normal DML/SELECT working.
    """
    if action in _DENY:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in (
            "writable_schema", "load_extension"):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def open_db(path, *, create: bool = False,
            busy_timeout: int = _DEFAULT_BUSY_MS) -> sqlite3.Connection:
    """Open the keeper DB and lock it down.

    ``create=True`` installs the schema first, then applies the authorizer (the
    schema's own CREATE statements must not be denied).  ``create=False``
    verifies the schema exists and applies the authorizer immediately.
    """
    path = Path(path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=busy_timeout / 1000.0,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout)}")
    conn.execute("PRAGMA trusted_schema=OFF")
    conn.execute("PRAGMA journal_mode=WAL")
    if create:
        conn.executescript(SCHEMA)
    else:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'"
        ).fetchone()
        if present is None:
            conn.close()
            raise RuntimeError(
                f"{path} is not a keeper database (schema missing); "
                "create it with open_db(..., create=True)")
    conn.set_authorizer(_authorizer)
    return conn


class KeeperLock:
    """Holds the singleton flock until ``release`` (or the context exits)."""

    def __init__(self, fileobj):
        self._file = fileobj

    def release(self):
        if self._file is not None:
            try:
                fcntl.flock(self._file, fcntl.LOCK_UN)
            finally:
                self._file.close()
                self._file = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def lock_keeper(path, *, blocking: bool = False) -> KeeperLock:
    """Take the exclusive keeper lock for ``path`` or raise ``KeeperLocked``.

    The lock lives on ``<path>.lock`` so it survives checkpointing of the DB.
    """
    lock_path = str(path) + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fileobj = os.fdopen(fd, "w")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(fileobj, flags)
    except OSError as exc:
        fileobj.close()
        raise KeeperLocked(f"keeper already locked: {lock_path}") from exc
    return KeeperLock(fileobj)
