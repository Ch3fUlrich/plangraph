"""Existing keeper SQLite guards, independent of schema assembly."""
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

OVERSIGHT_DDL = """
CREATE TABLE IF NOT EXISTS oversight_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_sha256 TEXT NOT NULL UNIQUE,
    principal    TEXT NOT NULL,
    generation   INTEGER NOT NULL,
    scope        TEXT NOT NULL DEFAULT 'plan-write',
    minted_by    TEXT,
    go_decision  TEXT,
    mint_reason  TEXT,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_oversight_tokens_live ON oversight_tokens(principal, expires_at);
CREATE TRIGGER IF NOT EXISTS trg_oversight_tokens_revoke_only_update
BEFORE UPDATE ON oversight_tokens
WHEN NOT (
        OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL
        AND OLD.id IS NEW.id AND OLD.token_sha256 IS NEW.token_sha256
        AND OLD.principal IS NEW.principal
        AND OLD.generation IS NEW.generation
        AND OLD.scope IS NEW.scope
        AND OLD.expires_at IS NEW.expires_at
        AND OLD.created_at IS NEW.created_at
        AND OLD.minted_by IS NEW.minted_by
        AND OLD.go_decision IS NEW.go_decision
        AND OLD.mint_reason IS NEW.mint_reason
     )
BEGIN SELECT RAISE(ABORT, 'oversight tokens are revoke-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_oversight_tokens_append_only_delete
BEFORE DELETE ON oversight_tokens
BEGIN SELECT RAISE(ABORT, 'oversight tokens are revoke-only'); END;

CREATE TABLE IF NOT EXISTS oversight_ops (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    token_id    INTEGER NOT NULL,
    principal   TEXT NOT NULL,
    generation  INTEGER NOT NULL,
    batch_ref   TEXT NOT NULL,
    op_index    INTEGER NOT NULL,
    op          TEXT NOT NULL,
    key         TEXT,
    finding_id  TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    outcome     TEXT NOT NULL CHECK (outcome IN ('applied','denied')),
    code        TEXT,
    event_seq   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_oversight_ops_ts ON oversight_ops(principal, ts);
CREATE INDEX IF NOT EXISTS idx_oversight_ops_key ON oversight_ops(key);
CREATE TRIGGER IF NOT EXISTS trg_oversight_ops_append_only_update
BEFORE UPDATE ON oversight_ops
BEGIN SELECT RAISE(ABORT, 'oversight_ops is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_oversight_ops_append_only_delete
BEFORE DELETE ON oversight_ops
BEGIN SELECT RAISE(ABORT, 'oversight_ops is append-only'); END;
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

