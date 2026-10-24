"""Postgres half of the keeper's database seam (see ``db.py`` and docs/keeper-postgres.md).

``PgConnection`` presents the small part of the ``sqlite3.Connection`` API the store
uses, so the store's portable SQL (``?`` placeholders, ``conn.execute``) runs
unchanged.  ``psycopg`` is imported only when a Postgres connection is opened.
The DSN comes from config/env and never appears in a message.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import schema_pg
from .errors import KeeperLocked

__all__ = ["PgConnection", "PgRow", "PgKeeperLock", "open_pg", "lock_pg", "migrate",
           "translate", "translate_control", "adapt_params", "guard", "DdlDenied",
           "derive_schema", "check_schema", "redact"]

#: first key of the two-int advisory locks; the second is ``hashtext(schema)``.
_WRITE_LOCK, _MIGRATE_LOCK, _SINGLETON_LOCK = 0x706701, 0x706702, 0x706703


class DdlDenied(Exception):
    """A keeper connection tried to run DDL or alter the session (the authorizer analogue)."""


# ---------------------------------------------------------------- SQL helpers
def translate(sql: str, has_params: bool = True) -> str:
    """``?`` -> ``%s`` outside quoted literals; ``%`` doubled when parameters follow."""
    if not has_params:
        return sql
    out, quote = [], None
    for ch in sql:
        if quote:
            out.append("%%" if ch == "%" else ch)
            if ch == quote:
                quote = None          # a doubled '' re-enters on the next char
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


_CONTROL = {"begin": "begin", "begin immediate": "begin", "commit": "commit",
            "end": "commit", "rollback": "rollback"}


def translate_control(sql: str):
    """``begin``/``commit``/``rollback`` for a transaction-control statement, else ``None``."""
    return _CONTROL.get(sql.strip().rstrip(";").strip().lower())


def adapt_params(params) -> tuple:
    """SQLite binds ``bool`` as 0/1; no keeper column is boolean, so do the same."""
    return tuple(int(p) if isinstance(p, bool) else p for p in (params or ()))


_DDL_START = re.compile(
    r"^\s*(create|drop|alter|truncate|grant|revoke|copy|set|reset|attach|detach|vacuum|"
    r"reindex|cluster|comment|do|load|refresh|security|import|call|listen|notify|unlisten|"
    r"discard|prepare|execute|deallocate|declare|fetch|move|close|lock|savepoint|release)\b",
    re.I)


def _strip_literals(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", "''", sql)


def guard(sql: str) -> None:
    """Refuse DDL, session changes and stacked statements on a keeper connection.

    The real boundary is the runtime role (no DDL rights); this keeps SQL injection of
    ``; DROP ...`` and trigger tampering from working even with an over-privileged role.
    """
    bare = _strip_literals(sql)
    if _DDL_START.match(bare) or ";" in bare.rstrip().rstrip(";"):
        raise DdlDenied("statement not authorized on a keeper connection")


# ------------------------------------------------------------------- names
def check_schema(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) \
            or name.startswith("pg_"):
        raise ValueError("invalid postgres schema name (lowercase letters, digits, _; "
                         "must not start with pg_)")
    return name


def derive_schema(path) -> str:
    """A stable schema name for a keeper whose ``db`` path is *path*."""
    digest = hashlib.sha1(str(Path(path).resolve()).encode("utf-8")).hexdigest()
    return "k_" + digest[:20]


_URL = re.compile(r"postgres(?:ql)?://\S+", re.I)
_KV = re.compile(r"\b(password|passfile|host|hostaddr|user|dbname|service|sslkey|sslcert)"
                 r"\s*=\s*\S+", re.I)


def redact(text) -> str:
    """Remove anything DSN-shaped from *text*."""
    return _KV.sub(r"\1=<redacted>", _URL.sub("<dsn>", str(text)))


# -------------------------------------------------------------------- rows
class PgRow:
    """A result row addressable by index and by column name, like ``sqlite3.Row``."""

    __slots__ = ("_keys", "_vals")

    def __init__(self, keys, vals):
        self._keys, self._vals = tuple(keys), tuple(vals)

    def __getitem__(self, item):
        if isinstance(item, str):
            try:
                return self._vals[self._keys.index(item)]
            except ValueError:
                raise KeyError(item) from None
        return self._vals[item]

    def keys(self):
        return list(self._keys)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __eq__(self, other):
        if isinstance(other, PgRow):
            return self._keys == other._keys and self._vals == other._vals
        if isinstance(other, tuple):
            return self._vals == other
        return NotImplemented

    def __hash__(self):
        return hash(self._vals)

    def __repr__(self):
        return f"PgRow({dict(zip(self._keys, self._vals))!r})"


class PgCursor:
    """Rows fetched eagerly (keeper result sets are small) plus ``rowcount``."""

    def __init__(self, rows, rowcount):
        self._rows, self._pos, self.rowcount = rows, 0, rowcount

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        self._pos += 1
        return self._rows[self._pos - 1]

    def fetchall(self):
        rest, self._pos = self._rows[self._pos:], len(self._rows)
        return rest

    def fetchmany(self, size=1):
        part = self._rows[self._pos:self._pos + size]
        self._pos += len(part)
        return part

    def __iter__(self):
        return iter(self.fetchall())


# --------------------------------------------------------------- connection
class PgConnection:
    """One psycopg connection (autocommit; the store issues its own BEGIN/COMMIT)."""

    backend = "postgres"

    def __init__(self, raw, schema: str):
        self._raw = raw
        self.schema = schema
        self._open = True

    # -- sqlite3.Connection surface used by the store
    def execute(self, sql, params=()):
        control = translate_control(sql)
        if control is not None:
            return self._control(control)
        guard(sql)
        return self._run(sql, params)

    def _run(self, sql, params=()):
        args = adapt_params(params)
        with self._raw.cursor() as cur:
            cur.execute(translate(sql, bool(args)), args or None)
            if cur.description is None:
                return PgCursor([], cur.rowcount)
            keys = [col.name for col in cur.description]
            rows = [PgRow(keys, vals) for vals in cur.fetchall()]
            return PgCursor(rows, cur.rowcount)

    def _control(self, control):
        if control == "begin":
            # SQLite's BEGIN IMMEDIATE: one writer per keeper schema, queued behind the lock.
            self._raw.execute("BEGIN")
            try:
                self._raw.execute("SELECT pg_advisory_xact_lock(%s, hashtext(current_schema()))",
                                  (_WRITE_LOCK,))
            except BaseException:
                self._raw.execute("ROLLBACK")      # a failed BEGIN IMMEDIATE leaves no transaction
                raise
        else:
            self._raw.execute(control.upper())
        return PgCursor([], 0)

    @property
    def in_transaction(self) -> bool:
        return int(self._raw.info.transaction_status) != 0     # pq.TransactionStatus.IDLE

    def commit(self):
        """``sqlite3.Connection.commit``: end an open transaction, else nothing."""
        if self.in_transaction:
            self._raw.execute("COMMIT")

    def rollback(self):
        if self.in_transaction:
            self._raw.execute("ROLLBACK")

    def close(self):
        if self._open:
            self._open = False
            self._raw.close()


# ----------------------------------------------------------------- opening
def _psycopg():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("the postgres backend needs psycopg: pip install 'psycopg[binary]'") \
            from exc
    return psycopg


def _connect(dsn, busy_ms):
    psycopg = _psycopg()
    if not dsn:
        raise RuntimeError("postgres backend selected but no DSN (keeper.json 'dsn' or PG_DSN)")
    try:
        raw = psycopg.connect(dsn, autocommit=True, prepare_threshold=None,
                              connect_timeout=max(1, int(busy_ms / 1000)))
    except psycopg.Error as exc:
        raise RuntimeError("cannot connect to the keeper database: "
                           + redact(type(exc).__name__)) from None
    raw.execute(f"SET lock_timeout = {int(busy_ms)}")
    return raw


def migrate(raw, schema: str) -> list:
    """Create the schema and apply pending migrations under an advisory lock; returns versions run."""
    check_schema(schema)
    ran = []
    raw.execute("BEGIN")
    try:
        raw.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_MIGRATE_LOCK, schema))
        raw.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        raw.execute(f'SET search_path TO "{schema}"')
        raw.execute(schema_pg.BOOTSTRAP)
        have = {row[0] for row in raw.execute("SELECT version FROM schema_migrations")}
        for step in schema_pg.MIGRATIONS:
            if step.version in have:
                continue
            raw.execute(step.sql)
            raw.execute("INSERT INTO schema_migrations(version,name,applied_at)"
                        " VALUES(%s,%s,to_char(now() AT TIME ZONE 'UTC',"
                        "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'))", (step.version, step.name))
            ran.append(step.version)
        # Presence-based additions must also run on already-current databases.
        raw.execute("ALTER TABLE cards ADD COLUMN IF NOT EXISTS"
                    " check_kind TEXT COLLATE \"C\" NOT NULL DEFAULT 'change'")
        raw.execute("ALTER TABLE cards ADD COLUMN IF NOT EXISTS"
                    " check_reads TEXT COLLATE \"C\" NOT NULL DEFAULT '[]'")
        raw.execute("COMMIT")
    except BaseException:
        raw.execute("ROLLBACK")
        raise
    return ran


def _installed(raw) -> int:
    """Latest applied migration, or 0 when this schema holds no keeper."""
    row = raw.execute("SELECT to_regclass('schema_migrations') IS NOT NULL,"
                      " to_regclass('items') IS NOT NULL").fetchone()
    if not (row[0] and row[1]):
        return 0
    return raw.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]


def open_pg(path, *, create=False, busy_timeout=5000, dsn=None, schema=None) -> PgConnection:
    """Open the keeper schema; ``create=True`` also migrates (admin/init only)."""
    name = check_schema(schema or derive_schema(path))
    raw = _connect(dsn, busy_timeout)
    try:
        if create:
            migrate(raw, name)
        else:
            raw.execute(f'SET search_path TO "{name}"')
            have = _installed(raw)
            if have == 0:
                raise RuntimeError(f"{path} is not a keeper database (postgres schema missing); "
                                   "create it with open_db(..., create=True) / pg admin init")
            if have < schema_pg.LATEST:
                raise RuntimeError(f"keeper schema is at migration {have}, code needs "
                                   f"{schema_pg.LATEST}: run pg admin migrate")
    except BaseException:
        raw.close()
        raise
    return PgConnection(raw, name)


class PgKeeperLock:
    """Session advisory lock on a dedicated connection; ``release`` closes it."""

    def __init__(self, raw):
        self._raw = raw

    def release(self):
        if self._raw is not None:
            try:
                self._raw.close()           # closing the session drops the lock
            finally:
                self._raw = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def lock_pg(path, *, blocking=False, dsn=None, schema=None) -> PgKeeperLock:
    """One keeper writer process per schema, across hosts (``lock_keeper``'s Postgres twin)."""
    name = check_schema(schema or derive_schema(path))
    raw = _connect(dsn, 5000)
    try:
        if blocking:
            raw.execute("SET lock_timeout = 0")
            raw.execute("SELECT pg_advisory_lock(%s, hashtext(%s))", (_SINGLETON_LOCK, name))
        else:
            got = raw.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s))",
                              (_SINGLETON_LOCK, name)).fetchone()[0]
            if not got:
                raise KeeperLocked(f"keeper already locked: postgres schema {name}")
    except BaseException:
        raw.close()
        raise
    return PgKeeperLock(raw)
