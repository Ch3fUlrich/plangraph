"""``pg admin copy-to-pg``: copy a SQLite keeper snapshot into an empty Postgres keeper (W3).

The source is opened read-only and never written.  ``--dry-run`` only reads it (row counts,
chain head, chain check).  ``--apply`` copies every table verbatim, in primary-key order, in
ONE transaction on the target, then compares per-table row counts and the events chain head
with the source; any mismatch rolls the whole transaction back.  The target must be empty.
Neither the DSN nor a filesystem path is ever printed.  See docs/keeper-postgres.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

from . import db as kdb, db_errors, events

__all__ = ["run", "TABLES", "CopyError"]

#: dependency-safe copy order: the chain first, then the cache and its satellites, then design 3,
#: then the projector tables (migration 3; no foreign keys into the tables above).
TABLES = ("events", "items", "evidence", "edges", "principals", "aliases", "run_tokens",
          "outbox_seen", "audit_log", "audit_findings",
          "cards", "attempts", "decisions", "questions", "answers", "budget", "metrics",
          "projector_applied", "projector_watermark", "projector_alarms", "merge_locks",
          "jev_shadow", "messages", "message_nonces", "message_envelopes")
_BATCH = 500


class CopyError(Exception):
    """A refusal with a message that is safe to print (no DSN, no path, no row data)."""


# ------------------------------------------------------------------ source
def _open_source(src):
    if not src or not os.path.isfile(src):
        raise CopyError("source database not found")
    uri = "file:" + quote(str(Path(src).resolve())) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    except sqlite3.Error:
        raise CopyError("source is not a readable SQLite database") from None
    if "events" not in names or "items" not in names:
        conn.close()
        raise CopyError("source is not a keeper database")
    unknown = sorted(set(names) - set(TABLES))
    if unknown:
        conn.close()
        raise CopyError("source has tables this tool does not know: " + ", ".join(unknown))
    return conn, [t for t in TABLES if t in names]


def _head(conn):
    row = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return (row[0], row[1]) if row else (0, "")


def _columns(src, table):
    info = list(src.execute(f"PRAGMA table_info({table})"))
    cols = [r["name"] for r in info]
    pk = [r["name"] for r in sorted((r for r in info if r["pk"]), key=lambda r: r["pk"])]
    return cols, pk or ["rowid"]


# ------------------------------------------------------------------ target
def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _check_columns(conn, table, cols):
    have = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = ?", (table,))}
    if not have:
        raise CopyError(f"target has no table {table}")
    missing = [c for c in cols if c not in have]
    if missing:
        raise CopyError(f"target table {table} lacks column(s): " + ", ".join(missing))


def _copy_table(src, conn, table):
    cols, order = _columns(src, table)
    _check_columns(conn, table, cols)
    quoted_cols = [f'"{c}"' for c in cols]
    quoted_order = [f'"{o}"' for o in order]
    sql = f'INSERT INTO "{table}"({",".join(quoted_cols)}) VALUES({",".join("?" * len(cols))})'
    cur = src.execute(f'SELECT {",".join(quoted_cols)} FROM "{table}" ORDER BY {",".join(quoted_order)}')
    while True:
        batch = cur.fetchmany(_BATCH)
        if not batch:
            return
        for row in batch:
            conn.execute(sql, tuple(row))


def _verify_messages_chain(conn) -> list[dict]:
    problems = []
    try:
        rows = conn.execute(
            'SELECT seq, "from", "to", body, trace_id, key, run_id, attempt_n, '
            'ack_state, ts, prev_hash, hash, posted_by FROM messages ORDER BY seq'
        ).fetchall()
    except db_errors.NO_TABLE as exc:
        if isinstance(exc, sqlite3.OperationalError) and "no such table" not in str(exc).lower():
            raise
        return []
    prev_hash = "0" * 64
    for r in rows:
        row = dict(r) if hasattr(r, "keys") else {
            "seq": r[0], "from": r[1], "to": r[2], "body": r[3],
            "trace_id": r[4], "key": r[5], "run_id": r[6], "attempt_n": r[7],
            "ack_state": r[8], "ts": r[9], "prev_hash": r[10], "hash": r[11],
            "posted_by": r[12],
        }
        stored_hash = row.pop("hash")
        stored_prev = row["prev_hash"]
        if stored_prev != prev_hash:
            problems.append({"seq": row["seq"], "code": "prev_hash_mismatch",
                             "msg": f"expected prev_hash {prev_hash!r}, stored {stored_prev!r}"})
        if isinstance(row["body"], str):
            try:
                row["body"] = json.loads(row["body"])
            except Exception:
                pass
        payload = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        want = hashlib.sha256((stored_prev + payload).encode()).hexdigest()
        if stored_hash != want:
            problems.append({"seq": row["seq"], "code": "hash_mismatch",
                             "msg": f"stored {stored_hash!r}, recomputed {want!r}"})
        prev_hash = stored_hash
    return problems


def _triggers(conn, tables, enable):
    """User triggers (the key-lock guards) would refuse the copy: events are loaded before
    the items/evidence they lock.  Toggled inside the copy transaction only (DDL is
    transactional in Postgres) through the raw connection; the adapter refuses DDL."""
    word = "ENABLE" if enable else "DISABLE"
    for table in tables:
        conn._raw.execute(f'ALTER TABLE "{table}" {word} TRIGGER USER')


def _triggers_all_on(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
        " WHERE c.relnamespace = current_schema()::regnamespace"
        " AND NOT t.tgisinternal AND t.tgenabled <> 'O'").fetchone()[0] == 0


def _advance_identities(conn, tables):
    """Explicit ids do not move an identity sequence; the next keeper write would collide."""
    for table in tables:
        for col in [r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = ?"
                " AND (is_identity = 'YES' OR column_default LIKE 'nextval%')", (table,))]:
            top = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]
            if top is not None:
                conn.execute("SELECT setval(pg_get_serial_sequence(?, ?), ?)",
                             (table, col, top))


def _apply(src, tables, source_counts, source_head, cfg_db, opts):
    conn = kdb.open_db(cfg_db, create=True, **opts)       # creates / migrates the schema
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            filled = [t for t in tables if _count(conn, t)]
            if filled:
                raise CopyError("target is not empty (" + ", ".join(filled)
                                + "): copy-to-pg only fills an empty keeper")
            _triggers(conn, tables, enable=False)
            for table in tables:
                _copy_table(src, conn, table)
            _triggers(conn, tables, enable=True)
            if not _triggers_all_on(conn):
                raise CopyError("target triggers could not be restored")
            target_counts = {t: _count(conn, t) for t in tables}
            if target_counts != source_counts:
                raise CopyError("row counts differ between source and target: "
                                + ", ".join(t for t in tables
                                            if target_counts[t] != source_counts[t]))
            if _head(conn) != source_head:
                raise CopyError("events chain head differs between source and target")
            if events.verify_chain(conn):
                raise CopyError("copied events chain does not verify")
            if "messages" in tables and _verify_messages_chain(conn):
                raise CopyError("copied messages chain does not verify")
            _advance_identities(conn, tables)
            conn.execute("COMMIT")
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    return target_counts


# ------------------------------------------------------------------ report
def _report(mode, tables, source_counts, head, target_counts=None):
    lines = [f"copy-to-pg {mode}",
             f"source events head: seq={head[0]} hash={head[1] or '-'}",
             "table            source" + ("    target" if target_counts else "")]
    for t in tables:
        tail = f"  {target_counts[t]:>8}" if target_counts else ""
        lines.append(f"{t:<15} {source_counts[t]:>8}{tail}")
    total = sum(source_counts.values())
    lines.append(f"{'total':<15} {total:>8}")
    return lines


def run(cfg, *, src=None, apply=False, out=None, err=None) -> int:
    """Exit code: 0 ok, 2 refused (broken source chain, non-empty target, mismatch, no DSN)."""
    out, err = out or sys.stdout, err or sys.stderr
    opts = dict(cfg.db_options)
    opts["backend"] = "postgres"
    try:
        if apply and not (opts.get("dsn") or os.environ.get("PG_DSN")):
            raise CopyError("--apply needs a target DSN (keeper.json 'dsn' or env PG_DSN)")
        source, tables = _open_source(src or str(cfg.db))
        try:
            counts = {t: _count(source, t) for t in tables}
            head = _head(source)
            problems = events.verify_chain(source)
            if problems:
                for p in problems[:10]:
                    print(f"source chain: seq {p['seq']} {p['code']}", file=err)
                raise CopyError(f"source events chain is broken ({len(problems)} problem(s)); "
                                "nothing copied")
            if "messages" in tables:
                msg_problems = _verify_messages_chain(source)
                if msg_problems:
                    for p in msg_problems[:10]:
                        print(f"source messages chain: seq {p['seq']} {p['code']}", file=err)
                    raise CopyError(f"source messages chain is broken ({len(msg_problems)} problem(s)); "
                                    "nothing copied")
            if not apply:
                print("\n".join(_report("dry-run (target untouched)", tables, counts, head)),
                      file=out)
                print("source chain ok", file=out)
                return 0
            target = _apply(source, tables, counts, head, cfg.db, opts)
        finally:
            source.close()
    except CopyError as exc:
        print(f"copy-to-pg: {exc}", file=err)
        return 2
    except (db_errors.INTEGRITY + db_errors.OPERATIONAL + db_errors.PROGRAMMING
            + db_errors.DENIED) as exc:
        print(f"copy-to-pg: copy failed ({type(exc).__name__}); target unchanged", file=err)
        return 2
    print("\n".join(_report("apply", tables, counts, head, target)), file=out)
    print("copied and verified", file=out)
    return 0
