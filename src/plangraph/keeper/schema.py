"""Keeper SQLite schema: tables + locking triggers as plain SQL strings.

The triggers are the *prevention* half of the locking model in
``docs/KEEPER.md``.  They key off the append-only ``review_passed`` event (never
``items.state``) so a raw SQL writer cannot fake a lock by setting a column;
``cancelled`` is treated as locked too.  ``db.open_db`` installs this schema via
its ``create=True`` path and then denies all DDL to the keeper connection.

Every UPDATE trigger tests **both** the old and the new key: a statement that
moves a row *onto* a locked key is as forbidden as one that moves it off.
"""
from __future__ import annotations

__all__ = ["SCHEMA_VERSION", "SCHEMA", "ALIASES_DDL", "TABLES", "TRIGGERS",
           "EVENT_TYPES", "ROLES", "RUN_TOKENS_DDL", "OUTBOX_DDL", "DESIGN3_DDL",
           "STATES", "EXIT_CLASSES", "WINDOW_CLASSES", "RISK_TAGS"]

SCHEMA_VERSION = 10

#: Every valid ``items.state``.  ``needs_input``, ``waiting``, ``failed``, ``parked``,
#: ``running`` and ``ready`` are the design-3 additions (set through ``set_state``).
STATES = ("open", "ready", "claimed", "in_review", "done", "cancelled", "running",
          "needs_input", "waiting", "failed", "parked")

#: design 4.5: how an attempt ended.
EXIT_CLASSES = ("ok", "check_fail", "flaky_check", "usage_limit", "outage",
                "lease_lost", "worker_crash", "stall", "timeout", "rebase_conflict",
                "merge_refused", "probe_fail", "contract_defect", "needs_input",
                "cancelled")

WINDOW_CLASSES = ("128k", "256k", "1m")
RISK_TAGS = ("auth", "secrets", "money", "migration", "public-contract", "live-step")

ROLES = ("router", "l1", "worker", "reviewer", "system", "reader", "spawner")

EVENT_TYPES = (
    "item_created", "item_defined", "claimed", "delegated", "released",
    "lease_expired", "evidence_added", "submitted", "changes_requested",
    "review_verdict", "review_passed", "closed_on_decision", "cancelled",
    "priority_set", "heartbeat",
    "waiver_set", "legacy_state_set", "run_recorded", "answered",
    "token_minted", "outage_recorded", "review_withdrawn", "kind_set",
    "state_set",
)

#: MODEL-HOST: ``old_key -> new_key`` after a ``rehome``; append-only.  Kept as
#: separate ``IF NOT EXISTS`` statements so ``db._upgrade`` can install it on a
#: database created before it existed.
ALIASES_DDL = """
CREATE TABLE IF NOT EXISTS aliases (
    old_key TEXT PRIMARY KEY,
    new_key TEXT NOT NULL CHECK (new_key <> old_key),
    ts      TEXT,
    actor   TEXT,
    reason  TEXT
);
CREATE TRIGGER IF NOT EXISTS trg_aliases_append_only_update
BEFORE UPDATE ON aliases
BEGIN SELECT RAISE(ABORT, 'aliases are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_aliases_append_only_delete
BEFORE DELETE ON aliases
BEGIN SELECT RAISE(ABORT, 'aliases are append-only'); END;
"""

#: M2B-RUN-TOKENS: keeper-issued run tokens.  Only the sha256 of a token is
#: stored.  Not item cache: ``events.fold`` and ``verify_integrity`` ignore it
#: (the ``token_minted`` event carries no token value).  ``IF NOT EXISTS`` so
#: ``db._upgrade`` can install it at open on an older database.
RUN_TOKENS_DDL = """
CREATE TABLE IF NOT EXISTS run_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_sha256 TEXT NOT NULL UNIQUE,
    key          TEXT NOT NULL,
    project      TEXT NOT NULL,
    epoch        INTEGER NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('writer','reviewer')),
    run_id       TEXT NOT NULL,
    family       TEXT,
    minted_by    TEXT,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_tokens_key ON run_tokens(key);
"""

#: M2B-OUTBOX: replayed offline entries, deduplicated by the client-generated id.
OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS outbox_seen (
    entry_id   TEXT PRIMARY KEY,
    token_id   INTEGER NOT NULL,
    verb       TEXT NOT NULL,
    key        TEXT,
    result     TEXT NOT NULL,
    seen_at    TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_outbox_seen_append_only_update
BEFORE UPDATE ON outbox_seen
BEGIN SELECT RAISE(ABORT, 'outbox_seen is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_outbox_seen_append_only_delete
BEFORE DELETE ON outbox_seen
BEGIN SELECT RAISE(ABORT, 'outbox_seen is append-only'); END;
"""

def _in(values):
    return ",".join(f"'{v}'" for v in values)


#: design 3 (W2): cards, attempts, decisions, questions/answers, budget.  Only
#: TEXT/INTEGER/REAL columns and portable SQL (``schema_pg`` Migration 2 is the
#: logical twin).  List-valued columns hold canonical JSON text.  ``attempts``,
#: ``decisions`` and ``answers`` are append-only.  ``IF NOT EXISTS`` so
#: ``db._upgrade`` can install it at open on a v9 database.
DESIGN3_DDL = f"""
CREATE TABLE IF NOT EXISTS cards (
    key             TEXT PRIMARY KEY REFERENCES items(key),
    outcome         TEXT NOT NULL,
    read_set        TEXT NOT NULL DEFAULT '[]',
    write_set       TEXT NOT NULL DEFAULT '[]',
    exports         TEXT NOT NULL DEFAULT '[]',
    forbid_paths    TEXT NOT NULL DEFAULT '[]',
    check_cmd       TEXT,
    smoke_checks    TEXT NOT NULL DEFAULT '[]',
    heldout_check   TEXT,
    window_class    TEXT CHECK (window_class IN ({_in(WINDOW_CLASSES)})),
    risk_tags       TEXT NOT NULL DEFAULT '[]',
    estimate_tokens INTEGER,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL REFERENCES items(key),
    n            INTEGER NOT NULL,
    harness      TEXT,
    model        TEXT,
    combo        TEXT,
    class        TEXT,
    started_at   TEXT,
    ended_at     TEXT,
    exit_class   TEXT NOT NULL CHECK (exit_class IN ({_in(EXIT_CLASSES)})),
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    cost         REAL,
    run_id       TEXT,
    workflow_id  TEXT,
    log_ref      TEXT,
    UNIQUE (key, n)
);
CREATE INDEX IF NOT EXISTS idx_attempts_key ON attempts(key);
CREATE TRIGGER IF NOT EXISTS trg_attempts_append_only_update
BEFORE UPDATE ON attempts
BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_attempts_append_only_delete
BEFORE DELETE ON attempts
BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END;

CREATE TABLE IF NOT EXISTS decisions (
    id      TEXT PRIMARY KEY,
    ts      TEXT NOT NULL,
    actor   TEXT,
    kind    TEXT NOT NULL CHECK (kind IN ('operator','judge')),
    text    TEXT NOT NULL,
    closes  TEXT NOT NULL DEFAULT '[]',
    waives  TEXT NOT NULL DEFAULT '[]'
);
CREATE TRIGGER IF NOT EXISTS trg_decisions_append_only_update
BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_decisions_append_only_delete
BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;

CREATE TABLE IF NOT EXISTS questions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    key            TEXT NOT NULL REFERENCES items(key),
    text           TEXT NOT NULL,
    default_answer TEXT,
    deadline       TEXT,
    risk_tags      TEXT NOT NULL DEFAULT '[]',
    state          TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','answered','timeout')),
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_questions_key ON questions(key);

CREATE TABLE IF NOT EXISTS answers (
    question_id INTEGER PRIMARY KEY REFERENCES questions(id),
    ts          TEXT NOT NULL,
    actor       TEXT,
    text        TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('answer','question_timeout'))
);
CREATE TRIGGER IF NOT EXISTS trg_answers_append_only_update
BEFORE UPDATE ON answers
BEGIN SELECT RAISE(ABORT, 'answers are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_answers_append_only_delete
BEFORE DELETE ON answers
BEGIN SELECT RAISE(ABORT, 'answers are append-only'); END;

CREATE TABLE IF NOT EXISTS budget (
    provider TEXT NOT NULL,
    day      TEXT NOT NULL,
    tokens   INTEGER NOT NULL DEFAULT 0,
    cost     REAL NOT NULL DEFAULT 0,
    meter    REAL,
    PRIMARY KEY (provider, day)
);
"""

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
    description    TEXT,
    host           TEXT,
    wait           TEXT,
    layer          TEXT,
    role           TEXT,
    agent_id       TEXT,
    phase_kind     TEXT,
    fixes          TEXT,
    choices        TEXT,
    kg_refs        TEXT,
    kind           TEXT,
    track          TEXT,
    owner          TEXT,
    owner_family   TEXT,
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
    round          INTEGER NOT NULL DEFAULT 0,
    claimed_by     TEXT,
    delegate       TEXT,
    lease_until    TEXT,
    reviewer       TEXT,
    model          TEXT,
    review_tier    TEXT NOT NULL DEFAULT 'standard'
                   CHECK (review_tier IN ('standard','high','none')),
    review_waiver  TEXT,
    writer_model TEXT,
    seat_models  TEXT,
    run_ids      TEXT,
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
# Only ``review_passed`` and ``closed_on_decision`` lock: a ``review_verdict``
# keeps the item in review until the second, independent passing verdict emits
# ``review_passed``, and a ``waiver_set`` (D-217) is an ordinary non-locking
# event.  A router ``closed_on_decision`` (X7b, tier ``none``) and an ``answered``
# (MK-ANSWER, question/decision items) are final the same
# way, so every trigger that froze a reviewed item freezes that one too.
_LOCKED = (
    "EXISTS (SELECT 1 FROM events e WHERE e.key = {k}"
    " AND e.type IN ('review_passed','closed_on_decision','answered'))"
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

-- Both, because UPDATE can move a row *onto* a locked key.
CREATE TRIGGER trg_evidence_locked_update
BEFORE UPDATE ON evidence
WHEN {_LOCKED.format(k='OLD.key')} OR {_LOCKED.format(k='NEW.key')}
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
-- INSERT OR REPLACE, which deletes the old row and re-inserts it.  The UPDATE
-- guard checks the new key too, so a rename onto a locked key is refused.
CREATE TRIGGER trg_items_locked_insert
BEFORE INSERT ON items
WHEN {_LOCKED.format(k='NEW.key')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

CREATE TRIGGER trg_items_locked_update
BEFORE UPDATE ON items
WHEN {_LOCKED.format(k='OLD.key')} OR {_LOCKED.format(k='NEW.key')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

CREATE TRIGGER trg_items_locked_delete
BEFORE DELETE ON items
WHEN {_LOCKED.format(k='OLD.key')}
BEGIN SELECT RAISE(ABORT, 'key is locked'); END;

-- The audit trail is append-only too.
CREATE TRIGGER trg_audit_log_append_only_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER trg_audit_log_append_only_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER trg_audit_findings_append_only_update
BEFORE UPDATE ON audit_findings
BEGIN SELECT RAISE(ABORT, 'audit_findings are append-only'); END;

CREATE TRIGGER trg_audit_findings_append_only_delete
BEFORE DELETE ON audit_findings
BEGIN SELECT RAISE(ABORT, 'audit_findings are append-only'); END;

-- Principals change only through auth.  The single permitted update is a bare
-- revoke: ``active`` 1 -> 0 with every other column identical to OLD.  INSERT
-- (a new principal) stays allowed; any other UPDATE or any DELETE aborts.
CREATE TRIGGER trg_principals_revoke_only_update
BEFORE UPDATE ON principals
WHEN NOT (
        OLD.active = 1 AND NEW.active = 0
        AND OLD.name IS NEW.name AND OLD.role IS NEW.role
        AND OLD.projects IS NEW.projects AND OLD.family IS NEW.family
        AND OLD.token_sha256 IS NEW.token_sha256
     )
BEGIN SELECT RAISE(ABORT, 'principals are managed by auth'); END;

CREATE TRIGGER trg_principals_no_delete
BEFORE DELETE ON principals
BEGIN SELECT RAISE(ABORT, 'principals are managed by auth'); END;

CREATE INDEX idx_events_key ON events(key);
CREATE INDEX idx_events_type ON events(type);
CREATE INDEX idx_evidence_key ON evidence(key);
CREATE INDEX idx_edges_dst ON edges(dst);
CREATE INDEX idx_audit_log_key ON audit_log(key);
"""

SCHEMA = TABLES + TRIGGERS + ALIASES_DDL + RUN_TOKENS_DDL + OUTBOX_DDL + DESIGN3_DDL
