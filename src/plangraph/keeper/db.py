"""The one way the keeper opens its database.

``open_db`` owns the pragmas, the schema install (only with ``create=True``) and
the sqlite3 authorizer that denies DDL afterwards.  ``lock_keeper`` is the
singleton flock helper the API/server uses to keep one writer per host.

Two backends sit behind it: SQLite (default) and Postgres (``db_pg``).  The
backend is chosen explicitly (``backend=`` / ``PG_KEEPER_BACKEND``), never by the
mere presence of a DSN.  See docs/keeper-postgres.md.
"""
from __future__ import annotations

import fcntl
import os
import re
import sqlite3
from pathlib import Path

from ..projector.ddl import SQLITE as PROJECTOR_SQLITE
from .errors import KeeperLocked
from .schema import ALIASES_DDL, DESIGN3_DDL, OUTBOX_DDL, RUN_TOKENS_DDL, SCHEMA, TRIGGERS

__all__ = ["open_db", "lock_keeper", "KeeperLock", "select_backend"]

_DEFAULT_BUSY_MS = 5000
_BACKENDS = ("sqlite", "postgres")


def select_backend(explicit=None) -> str:
    """``explicit`` if given, else env ``PG_KEEPER_BACKEND``, else ``sqlite``."""
    choice = explicit if explicit is not None else (
        os.environ.get("PG_KEEPER_BACKEND") or "sqlite")
    if choice not in _BACKENDS:
        raise ValueError(f"unknown keeper backend {choice!r} (sqlite or postgres)")
    return choice


def _pg_dsn(dsn):
    return dsn or os.environ.get("PG_DSN")


def _pg_schema(schema):
    return schema or os.environ.get("PG_SCHEMA") or None


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

# Pragmas that can weaken the triggers/authorizer itself.  A *write* (arg2 is a
# value) is denied; the read form (``PRAGMA foreign_keys``) is still allowed.
_WRITE_PRAGMAS = frozenset({
    "recursive_triggers", "foreign_keys", "trusted_schema",
    "ignore_check_constraints", "journal_mode", "query_only",
    "schema_version", "writable_schema",
})


def _authorizer(action, arg1, arg2, dbname, source):
    """Deny DDL, the write pragmas, ATTACH and ``load_extension``.

    Returning ``SQLITE_OK`` for everything else keeps normal DML/SELECT working.
    """
    if action in _DENY:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        if name == "load_extension" or (name in _WRITE_PRAGMAS and arg2 is not None):
            return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _enable_defensive(conn):
    """Turn on SQLITE_DBCONFIG_DEFENSIVE when the build supports it."""
    if not hasattr(conn, "setconfig") or not hasattr(
            sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
        return
    try:
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    except (sqlite3.Error, AttributeError, TypeError, ValueError):
        pass


#: Columns added after a schema shipped: ``(table, column, DDL type)``.  An older
#: database gains them when opened (before the authorizer denies ALTER).
_ADDED_COLUMNS = (("items", "description", "TEXT"), ("items", "host", "TEXT"),
                  ("items", "wait", "TEXT"),
                  ("items", "writer_model", "TEXT"),
                  ("items", "seat_models", "TEXT"),
                  ("items", "run_ids", "TEXT"),
                  ("items", "layer", "TEXT"), ("items", "role", "TEXT"),
                  ("items", "agent_id", "TEXT"), ("items", "phase_kind", "TEXT"),
                  ("items", "fixes", "TEXT"), ("items", "choices", "TEXT"),
                  ("items", "kg_refs", "TEXT"))


_LOCK_TRIGGER = re.compile(r"CREATE TRIGGER (trg_\w+_locked_\w+).*?END;", re.S)


def _upgrade_lock_triggers(conn):
    """MK-ANSWER: re-create the key-lock triggers if they predate ``answered``.

    The lock list lives inside each trigger body, so an older database only
    freezes a key on ``review_passed``/``closed_on_decision``; drop and recreate
    them in one transaction (idempotent: a no-op once ``'answered'`` is there).
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger'"
                       " AND name='trg_events_locked_insert'").fetchone()
    if row is None or "'answered'" in row[0]:
        return
    found = list(_LOCK_TRIGGER.finditer(TRIGGERS))
    script = "BEGIN IMMEDIATE;\n" + "".join(
        f"DROP TRIGGER IF EXISTS {m.group(1)};\n" for m in found)
    script += "\n".join(m.group(0) for m in found) + "\nCOMMIT;"
    conn.executescript(script)


def _upgrade(conn):
    """Add any column/table/trigger an older database lacks; idempotent, DML-free."""
    for table, column, ddl in _ADDED_COLUMNS:
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.executescript(ALIASES_DDL)
    conn.executescript(RUN_TOKENS_DDL)
    conn.executescript(OUTBOX_DDL)
    conn.executescript(DESIGN3_DDL)
    conn.executescript(PROJECTOR_SQLITE)
    _upgrade_lock_triggers(conn)


def open_db(path, *, create: bool = False,
            busy_timeout: int = _DEFAULT_BUSY_MS, backend=None, dsn=None, schema=None):
    """Open the keeper DB and lock it down.

    ``create=True`` installs the schema first, then applies the authorizer (the
    schema's own CREATE statements must not be denied).  ``create=False``
    verifies the schema exists and applies the authorizer immediately.

    With the Postgres backend *path* only names the keeper (it derives the schema
    name unless *schema* is given); the connection is a ``db_pg.PgConnection``.
    """
    if select_backend(backend) == "postgres":
        from . import db_pg
        return db_pg.open_pg(path, create=create, busy_timeout=busy_timeout,
                             dsn=_pg_dsn(dsn), schema=_pg_schema(schema))
    path = Path(path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    # ``check_same_thread=False`` because the threaded API owns one connection
    # behind one lock; all access is serialised there, never concurrent.
    conn = sqlite3.connect(str(path), timeout=busy_timeout / 1000.0,
                           isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout)}")
    conn.execute("PRAGMA trusted_schema=OFF")
    conn.execute("PRAGMA journal_mode=WAL")
    if create:
        conn.executescript(SCHEMA + PROJECTOR_SQLITE)
    else:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'"
        ).fetchone()
        if present is None:
            conn.close()
            raise RuntimeError(
                f"{path} is not a keeper database (schema missing); "
                "create it with open_db(..., create=True)")
        _upgrade(conn)
    # Defensive mode hardens shadow tables and blocks several bypasses; enable
    # it after setup (it must not interfere with the CREATE statements above)
    # and before the authorizer, which then covers the rest.
    _enable_defensive(conn)
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


def lock_keeper(path, *, blocking: bool = False, backend=None, dsn=None, schema=None):
    """Take the exclusive keeper lock for ``path`` or raise ``KeeperLocked``.

    The lock lives on ``<path>.lock`` so it survives checkpointing of the DB.
    On Postgres it is a session advisory lock on a dedicated connection.
    """
    if select_backend(backend) == "postgres":
        from . import db_pg
        return db_pg.lock_pg(path, blocking=blocking, dsn=_pg_dsn(dsn),
                             schema=_pg_schema(schema))
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
