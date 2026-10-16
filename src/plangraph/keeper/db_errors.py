"""Driver-neutral exception groups for the keeper's two storage backends.

The store catches these tuples instead of ``sqlite3`` classes, so the same
``except`` clause covers SQLite and (when ``psycopg`` is importable) Postgres.
SQLite-only deployments never need ``psycopg``.
"""
from __future__ import annotations

import sqlite3

from .db_pg import DdlDenied

__all__ = ["INTEGRITY", "OPERATIONAL", "PROGRAMMING", "NO_TABLE", "DENIED",
           "have_psycopg"]

INTEGRITY: tuple = (sqlite3.IntegrityError,)
#: busy / lock-timeout style failures: a lost race, not a crash.
OPERATIONAL: tuple = (sqlite3.OperationalError,)
#: a request the database could not even bind or store (wrong type, NUL, overflow).
PROGRAMMING: tuple = (sqlite3.ProgrammingError,)
NO_TABLE: tuple = (sqlite3.OperationalError,)
DENIED: tuple = (sqlite3.DatabaseError, DdlDenied)

try:                                          # pragma: no cover - depends on the env
    import psycopg
    from psycopg import errors as _pge
except ImportError:                           # pragma: no cover
    psycopg = None
else:
    INTEGRITY += (psycopg.IntegrityError,)
    OPERATIONAL += (psycopg.OperationalError,)
    # DataError: 22xxx (invalid text for an integer, NUL in text, out of range).
    PROGRAMMING += (psycopg.ProgrammingError, psycopg.DataError)
    NO_TABLE += (_pge.UndefinedTable,)


def have_psycopg() -> bool:
    return psycopg is not None
