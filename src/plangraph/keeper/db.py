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
from .schema_guards import RECEIPTS_DDL
from .placement_rules_schema import SQLITE as PLACEMENT_RULES_DDL
from .schema import (ALERTS_DDL, ALIASES_DDL, DESIGN3_DDL, EVENT_AUDIENCE_DDL, GRANTS_DDL, INCIDENTS_DDL, JEV_DDL,
                     LANDING_JOBS_DDL, LANDING_STAGES_DDL, LANDING_TRAINS_DDL,
                     LOCKS_DDL, METRICS_DDL, OUTBOX_DDL, OVERSIGHT_DDL, RUN_TOKENS_DDL,
                     SCHEMA, TRIGGERS)

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
                  ("items", "kg_refs", "TEXT"),
                  ("items", "exec_workflow_id", "TEXT"),
                  ("items", "exec_run_id", "TEXT"),
                  ("items", "exec_attempt", "INTEGER"),
                  # RT-P1 sq5: the owner's principal (V2-1) and the retired-run
                  # fence kept across owner clears (V1-1), SQLite v13 path.
                  ("items", "exec_principal", "TEXT"),
                  ("items", "exec_retired_runs", "TEXT"),
                  ("cards", "check_kind", "TEXT NOT NULL DEFAULT 'change'"),
                  ("cards", "check_reads", "TEXT NOT NULL DEFAULT '[]'"))


_LOCK_TRIGGER = re.compile(r"CREATE TRIGGER (trg_\w+_locked_\w+).*?END;", re.S)

#: The oversight guard triggers (``OVERSIGHT_DDL``): a presence-based install
#: never REPAIRS one, so ``_repair_oversight_triggers`` drops them on every open
#: and the DDL below recreates the current body -- a weakened or older guard
#: cannot survive a reopen (D-2458).  Derived directly from OVERSIGHT_DDL.
_OVERSIGHT_TRIGGERS = tuple(
    re.findall(r"CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", OVERSIGHT_DDL)
)


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


def _first_sql_word(stmt: str) -> str:
    """Return the first keyword of a SQL statement, skipping comments/whitespace."""
    clean = re.sub(r"^(?:\s*--[^\n]*(?:\n|$|\r)|\s*/\*.*?\*/\s*)+", "", stmt, flags=re.S).strip()
    m = re.match(r"^([A-Za-z]+)", clean)
    return m.group(1).upper() if m else ""


def _split_statements(
    sql: str,
    *,
    disallow: tuple[str, ...] | frozenset[str] = ("PRAGMA", "VACUUM"),
) -> list[str]:
    """Split a SQL script into individual statements using sqlite3.complete_statement.

    Splits on a character/semicolon basis, respecting trigger BEGIN...END bodies.
    Statements whose leading keyword is in ``disallow`` are refused with a ValueError.
    """
    statements = []
    buffer = ""
    for ch in sql:
        buffer += ch
        if ch == ";" and sqlite3.complete_statement(buffer):
            stmt = buffer.strip()
            if stmt:
                word = _first_sql_word(stmt)
                if disallow and word in disallow:
                    raise ValueError(f"disallowed statement in transaction: {word}")
                statements.append(stmt)
            buffer = ""
    trailing = buffer.strip()
    if trailing:
        clean = re.sub(r"^(?:\s*--[^\n]*(?:\n|$|\r)|\s*/\*.*?\*/\s*)+", "", trailing, flags=re.S).strip()
        if clean:
            word = _first_sql_word(trailing)
            if disallow and word in disallow:
                raise ValueError(f"disallowed statement in transaction: {word}")
            statements.append(trailing)
    return statements


