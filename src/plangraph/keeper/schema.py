"""Keeper SQLite schema: tables + locking triggers as plain SQL strings.

The triggers are the *prevention* half of the locking model in
``docs/KEEPER.md``.  They key off the append-only ``review_passed`` event (never
``items.state``) so a raw SQL writer cannot fake a lock by setting a column;
``cancelled`` is treated as locked too.  ``db.open_db`` installs this schema via
its ``create=True`` path and then denies all DDL to the keeper connection.
"""
from __future__ import annotations

__all__ = ["SCHEMA_VERSION", "SCHEMA", "TABLES", "TRIGGERS", "EVENT_TYPES",
           "ROLES"]

SCHEMA_VERSION = 1

ROLES = ("router", "l1", "worker", "reviewer", "system", "reader")

EVENT_TYPES = (
    "item_created", "item_defined", "claimed", "delegated", "released",
    "lease_expired", "evidence_added", "submitted", "changes_requested",
    "review_passed", "cancelled", "priority_set",
)

TABLES = """
CREATE TABLE principals (
    name         TEXT PRIMARY KEY,
    role         TEXT NOT NULL,
    projects     TEXT NOT NULL DEFAULT '[]',
    family       TEXT,
    token_sha256 TEXT,
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE items (
    key            TEXT PRIMARY KEY,
    project        TEXT NOT NULL,
    id             TEXT NOT NULL,
    title          TEXT,
    kind           TEXT,
    track          TEXT,
    owner          TEXT,
    estimate_h     REAL,
    estimate_src   TEXT,
    done_criteria  TEXT,
    evidence_kinds TEXT NOT NULL DEFAULT '[]',
    operator       INTEGER NOT NULL DEFAULT 0,
    priority       INTEGER NOT NULL DEFAULT 0,
    source         TEXT,
    note           TEXT,
    corrects       TEXT,
    state          TEXT NOT NULL DEFAULT 'open',
    epoch          INTEGER NOT NULL DEFAULT 0,
    claimed_by     TEXT,
    delegate       TEXT,
    lease_until    TEXT,
    reviewer       TEXT,
    model          TEXT,
    created_at     TEXT,
    updated_at     TEXT
);

CREATE TABLE edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    rel TEXT NOT NULL DEFAULT 'depends_on' CHECK (rel IN ('depends_on')),
    PRIMARY KEY (src, dst)
);

CREATE TABLE events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT,
    role      TEXT,
    model     TEXT,
    key       TEXT,
    type      TEXT NOT NULL,
    payload   TEXT NOT NULL DEFAULT '{}',
    prev_hash TEXT NOT NULL DEFAULT '',
    hash      TEXT NOT NULL
);

CREATE TABLE evidence (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    key      TEXT NOT NULL,
    epoch    INTEGER,
    kind     TEXT NOT NULL,
    ref      TEXT NOT NULL DEFAULT '{}',
    verified INTEGER NOT NULL DEFAULT 0,
    detail   TEXT,
    at       TEXT
);

CREATE TABLE audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT,
    actor  TEXT,
    role   TEXT,
    action TEXT,
    key    TEXT,
    ok     INTEGER NOT NULL DEFAULT 0,
    code   TEXT,
    msg    TEXT
);

CREATE TABLE audit_findings (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT,
    key  TEXT,
    code TEXT,
    msg  TEXT
);
"""

# The reviewed/cancelled test, inlined because SQLite triggers cannot call
# Python helpers.  `:k` is replaced with the key expression of each trigger.
_LOCKED = (
    "EXISTS (SELECT 1 FROM events e WHERE e.key = {k} AND e.type = 'review_passed')"
    " OR EXISTS (SELECT 1 FROM items i WHERE i.key = {k} AND i.state = 'cancelled')"
)

TRIGGERS = f"""
-- Events are append-only, full stop.
CREATE TRIGGER trg_events_append_only_update
BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE TRIGGER trg_events_append_only_delete
BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

-- Neither events nor evidence may be written for a locked key.
CREATE TRIGGER trg_events_locked_insert
BEFORE INSERT ON events
WHEN {_LOCKED.format(k='NEW.key')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

CREATE TRIGGER trg_evidence_locked_insert
BEFORE INSERT ON evidence
WHEN {_LOCKED.format(k='NEW.key')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

CREATE TRIGGER trg_evidence_locked_update
BEFORE UPDATE ON evidence
WHEN {_LOCKED.format(k='OLD.key')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

CREATE TRIGGER trg_evidence_locked_delete
BEFORE DELETE ON evidence
WHEN {_LOCKED.format(k='OLD.key')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

-- Edges are frozen when their `src` (the dependent item) is locked.  An edge
-- *into* a DONE item is fine: new items may depend on DONE work.
CREATE TRIGGER trg_edges_locked_insert
BEFORE INSERT ON edges
WHEN {_LOCKED.format(k='NEW.src')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

CREATE TRIGGER trg_edges_locked_update
BEFORE UPDATE ON edges
WHEN {_LOCKED.format(k='OLD.src')} OR {_LOCKED.format(k='NEW.src')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

CREATE TRIGGER trg_edges_locked_delete
BEFORE DELETE ON edges
WHEN {_LOCKED.format(k='OLD.src')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

-- DONE/cancelled item rows are frozen.  The BEFORE INSERT guard also fires for
-- INSERT OR REPLACE, which deletes the old row and re-inserts it.
CREATE TRIGGER trg_items_locked_insert
BEFORE INSERT ON items
WHEN {_LOCKED.format(k='NEW.key')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

CREATE TRIGGER trg_items_locked_update
BEFORE UPDATE ON items
WHEN {_LOCKED.format(k='OLD.key')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

CREATE TRIGGER trg_items_locked_delete
BEFORE DELETE ON items
WHEN {_LOCKED.format(k='OLD.key')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

CREATE INDEX idx_events_key ON events(key);
CREATE INDEX idx_events_type ON events(type);
CREATE INDEX idx_evidence_key ON evidence(key);
CREATE INDEX idx_edges_dst ON edges(dst);
CREATE INDEX idx_audit_log_key ON audit_log(key);
"""

SCHEMA = TABLES + TRIGGERS
