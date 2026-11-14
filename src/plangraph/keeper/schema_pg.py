"""Keeper Postgres schema: versioned migrations, same logical schema as SQLite.

Migration 1 is the logical equivalent of SQLite schema v9 (``schema.py``): the same
tables with the same columns in the same order, the same indexes and the same
locking triggers (plpgsql functions + row triggers, ``ERRCODE 23000`` so the store
sees an integrity error exactly as it does for SQLite ``RAISE(ABORT)``).

Dialect choices: every TEXT column is ``COLLATE "C"`` (SQLite BINARY ordering, so
``ORDER BY key`` is identical); JSON columns stay TEXT (the event hash is computed
in Python over canonical JSON); ``events.seq`` and ``run_tokens.id`` carry no
sequence (the store assigns them, so a rolled-back write never leaves a gap).

Migration 2 adds the design-3 tables (cards, attempts, decisions, questions, answers,
budget) and the append-only triggers on attempts, decisions and answers.

A later change appends a new ``Migration`` to ``MIGRATIONS``; never edit one that
shipped.  ``db_pg.migrate`` applies the pending ones under an advisory lock.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..projector.ddl import PG as PROJECTOR_PG
from .schema import EXIT_CLASSES, JEV_KINDS, JEV_STATUSES, WINDOW_CLASSES
from .messages_schema import PG as MESSAGES_DDL
from .schema_pg_spawner import PG as SPAWNER_DDL
from .schema_pg_baseline import BASELINE as _V1
from .placement_rules_schema import PG as PLACEMENT_RULES_DDL
__all__ = ["Migration", "MIGRATIONS", "LATEST", "parse_tables", "table_bodies",
           "BOOTSTRAP"]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def _in(values):
    return ",".join(f"'{v}'" for v in values)


# Design-3 preserves SQLite column order; ids are assigned by the
# store (MAX(id)+1 inside the write lock), like ``run_tokens``.
_V2 = f"""
CREATE TABLE cards (
    key             TEXT COLLATE "C" PRIMARY KEY REFERENCES items(key),
    outcome         TEXT COLLATE "C" NOT NULL,
    read_set        TEXT COLLATE "C" NOT NULL DEFAULT '[]',
    write_set       TEXT COLLATE "C" NOT NULL DEFAULT '[]',
    exports         TEXT COLLATE "C" NOT NULL DEFAULT '[]',
    forbid_paths    TEXT COLLATE "C" NOT NULL DEFAULT '[]',
    check_cmd       TEXT COLLATE "C",
    smoke_checks    TEXT COLLATE "C" NOT NULL DEFAULT '[]',
    heldout_check   TEXT COLLATE "C",
    check_kind     TEXT COLLATE "C" NOT NULL DEFAULT 'change',
    check_reads    TEXT COLLATE "C" NOT NULL DEFAULT '[]',
    window_class    TEXT COLLATE "C" CHECK (window_class IN ({_in(WINDOW_CLASSES)})),
    risk_tags       TEXT COLLATE "C" NOT NULL DEFAULT '[]',
    estimate_tokens BIGINT,
    updated_at      TEXT COLLATE "C"
);

CREATE TABLE attempts (
    id           BIGINT PRIMARY KEY,
    key          TEXT COLLATE "C" NOT NULL REFERENCES items(key),
    n            BIGINT NOT NULL,
    harness      TEXT COLLATE "C",
    model        TEXT COLLATE "C",
    combo        TEXT COLLATE "C",
    class        TEXT COLLATE "C",
    started_at   TEXT COLLATE "C",
    ended_at     TEXT COLLATE "C",
    exit_class   TEXT COLLATE "C" NOT NULL CHECK (exit_class IN ({_in(EXIT_CLASSES)})),
    tokens_in    BIGINT,
    tokens_out   BIGINT,
    cost         DOUBLE PRECISION,
    run_id       TEXT COLLATE "C",
    workflow_id  TEXT COLLATE "C",
    log_ref      TEXT COLLATE "C",
    UNIQUE (key, n)
);
CREATE INDEX idx_attempts_key ON attempts(key);

CREATE TABLE decisions (
    id      TEXT COLLATE "C" PRIMARY KEY,
    ts      TEXT COLLATE "C" NOT NULL,
    actor   TEXT COLLATE "C",
    kind    TEXT COLLATE "C" NOT NULL CHECK (kind IN ('operator','judge')),
    text    TEXT COLLATE "C" NOT NULL,
    closes  TEXT COLLATE "C" NOT NULL DEFAULT '[]',
    waives  TEXT COLLATE "C" NOT NULL DEFAULT '[]'
);

CREATE TABLE questions (
    id             BIGINT PRIMARY KEY,
    key            TEXT COLLATE "C" NOT NULL REFERENCES items(key),
    text           TEXT COLLATE "C" NOT NULL,
    default_answer TEXT COLLATE "C",
    deadline       TEXT COLLATE "C",
    risk_tags      TEXT COLLATE "C" NOT NULL DEFAULT '[]',
    state          TEXT COLLATE "C" NOT NULL DEFAULT 'open'
                   CHECK (state IN ('open','answered','timeout')),
    created_at     TEXT COLLATE "C" NOT NULL
);
CREATE INDEX idx_questions_key ON questions(key);

CREATE TABLE answers (
    question_id BIGINT PRIMARY KEY REFERENCES questions(id),
    ts          TEXT COLLATE "C" NOT NULL,
    actor       TEXT COLLATE "C",
    text        TEXT COLLATE "C" NOT NULL,
    source      TEXT COLLATE "C" NOT NULL CHECK (source IN ('answer','question_timeout'))
);

CREATE TABLE budget (
    provider TEXT COLLATE "C" NOT NULL,
    day      TEXT COLLATE "C" NOT NULL,
    tokens   BIGINT NOT NULL DEFAULT 0,
    cost     DOUBLE PRECISION NOT NULL DEFAULT 0,
    meter    DOUBLE PRECISION,
    PRIMARY KEY (provider, day)
);

-- attempts, decisions and answers are append-only.
CREATE TRIGGER trg_attempts_append_only_update BEFORE UPDATE ON attempts
    FOR EACH ROW EXECUTE FUNCTION pgk_reject('attempts are append-only');
CREATE TRIGGER trg_attempts_append_only_delete BEFORE DELETE ON attempts
    FOR EACH ROW EXECUTE FUNCTION pgk_reject('attempts are append-only');
CREATE TRIGGER trg_attempts_append_only_truncate BEFORE TRUNCATE ON attempts
    FOR EACH STATEMENT EXECUTE FUNCTION pgk_reject('attempts are append-only');
CREATE TRIGGER trg_decisions_append_only_update BEFORE UPDATE ON decisions
    FOR EACH ROW EXECUTE FUNCTION pgk_reject('decisions are append-only');
CREATE TRIGGER trg_decisions_append_only_delete BEFORE DELETE ON decisions
    FOR EACH ROW EXECUTE FUNCTION pgk_reject('decisions are append-only');
CREATE TRIGGER trg_decisions_append_only_truncate BEFORE TRUNCATE ON decisions
    FOR EACH STATEMENT EXECUTE FUNCTION pgk_reject('decisions are append-only');
CREATE TRIGGER trg_answers_append_only_update BEFORE UPDATE ON answers
    FOR EACH ROW EXECUTE FUNCTION pgk_reject('answers are append-only');
CREATE TRIGGER trg_answers_append_only_delete BEFORE DELETE ON answers
    FOR EACH ROW EXECUTE FUNCTION pgk_reject('answers are append-only');
CREATE TRIGGER trg_answers_append_only_truncate BEFORE TRUNCATE ON answers
    FOR EACH STATEMENT EXECUTE FUNCTION pgk_reject('answers are append-only');
"""

#: Logical twin of ``schema.METRICS_DDL`` (SQLite schema v11).
_V4 = """
CREATE TABLE metrics (
    day         TEXT COLLATE "C" NOT NULL,
    metric      TEXT COLLATE "C" NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    source      TEXT COLLATE "C" NOT NULL DEFAULT '',
    computed_at TEXT COLLATE "C" NOT NULL,
    PRIMARY KEY (day, metric)
);
"""

#: Logical twin of ``schema.LOCKS_DDL`` (SQLite schema v12).  ``repo`` carries no
#: NOT NULL: SQLite PRAGMA reports notnull=0 for a non-INTEGER TEXT PRIMARY KEY,
#: and the parity test compares against that.
_V5 = """
CREATE TABLE merge_locks (
    repo       TEXT COLLATE "C" PRIMARY KEY,
    token      BIGINT NOT NULL,
    owner      TEXT COLLATE "C" NOT NULL DEFAULT '',
    expires_at BIGINT NOT NULL DEFAULT 0
);
"""

#: Logical twin of ``schema.JEV_DDL`` (SQLite schema v13).  ``id BIGINT PRIMARY KEY``
#: is NOT NULL on both backends (unlike a TEXT primary key); ``key`` carries no
#: foreign key.  ``outcome`` is the one column filled in later, first write wins.
_V6 = f"""
CREATE TABLE jev_shadow (
    id                BIGINT PRIMARY KEY,
    key               TEXT COLLATE "C" NOT NULL,
    attempt           BIGINT,
    decision_id       TEXT COLLATE "C",
    input_hash        TEXT COLLATE "C",
    kind              TEXT COLLATE "C" NOT NULL CHECK (kind IN ({_in(JEV_KINDS)})),
    rule_answer       TEXT COLLATE "C" NOT NULL,
    jev_answer        TEXT COLLATE "C",
    jev_probabilities TEXT COLLATE "C",
    jev_confidence    DOUBLE PRECISION,
    status            TEXT COLLATE "C" NOT NULL CHECK (status IN ({_in(JEV_STATUSES)})),
    reason            TEXT COLLATE "C" NOT NULL DEFAULT '',
    outcome           TEXT COLLATE "C",
    jev_model         TEXT COLLATE "C" NOT NULL DEFAULT '',
    latency_ms        BIGINT,
    created_at        TEXT COLLATE "C" NOT NULL,
    outcome_at        TEXT COLLATE "C"
);
CREATE INDEX idx_jev_shadow_key ON jev_shadow(key);
"""

#: RT-P1-EXEC-OWNERSHIP: exec-ownership columns on ``items``.  These land as PG
#: migration 7 because migration 6 already shipped with the JEV shadow table
#: (#96, merged first).  The store records the columns when a set-state move
#: carries a ``(workflow_id, run_id, attempt)`` run; see ``store_cards._exec_check``.
#: sq5: ``exec_principal`` binds the owner to the worker principal that recorded
#: it (V2-1) and ``exec_retired_runs`` is the retired-run fence every owner clear
#: keeps (V1-1) -- both are additive parts of the SAME migration 7 and of the
#: SQLite v13 upgrade path, so ``LATEST`` stays 7.
_V7 = """
ALTER TABLE items ADD COLUMN IF NOT EXISTS exec_workflow_id TEXT COLLATE "C";
ALTER TABLE items ADD COLUMN IF NOT EXISTS exec_run_id TEXT COLLATE "C";
ALTER TABLE items ADD COLUMN IF NOT EXISTS exec_attempt BIGINT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS exec_principal TEXT COLLATE "C";
ALTER TABLE items ADD COLUMN IF NOT EXISTS exec_retired_runs TEXT COLLATE "C";
"""

_V8 = """
CREATE TABLE oversight_tokens (
    id           BIGINT PRIMARY KEY,
    token_sha256 TEXT COLLATE "C" NOT NULL UNIQUE,
    principal    TEXT COLLATE "C" NOT NULL,
    generation   BIGINT NOT NULL,
    scope        TEXT COLLATE "C" NOT NULL DEFAULT 'plan-write',
    minted_by    TEXT COLLATE "C",
    go_decision  TEXT COLLATE "C",
    mint_reason  TEXT COLLATE "C",
    created_at   TEXT COLLATE "C" NOT NULL,
    expires_at   TEXT COLLATE "C" NOT NULL,
    revoked_at   TEXT COLLATE "C"
);
CREATE INDEX idx_oversight_tokens_live ON oversight_tokens(principal, expires_at);
CREATE TRIGGER trg_oversight_tokens_revoke_only_update BEFORE UPDATE ON oversight_tokens
    FOR EACH ROW WHEN (NOT (
        OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL
        AND OLD.id IS NOT DISTINCT FROM NEW.id
        AND OLD.token_sha256 IS NOT DISTINCT FROM NEW.token_sha256
        AND OLD.principal IS NOT DISTINCT FROM NEW.principal
        AND OLD.generation IS NOT DISTINCT FROM NEW.generation
        AND OLD.scope IS NOT DISTINCT FROM NEW.scope
        AND OLD.expires_at IS NOT DISTINCT FROM NEW.expires_at
        AND OLD.created_at IS NOT DISTINCT FROM NEW.created_at
        AND OLD.minted_by IS NOT DISTINCT FROM NEW.minted_by
        AND OLD.go_decision IS NOT DISTINCT FROM NEW.go_decision
        AND OLD.mint_reason IS NOT DISTINCT FROM NEW.mint_reason))
    EXECUTE FUNCTION pgk_reject('oversight tokens are revoke-only');
CREATE TRIGGER trg_oversight_tokens_append_only_delete BEFORE DELETE ON oversight_tokens
    FOR EACH ROW EXECUTE FUNCTION pgk_reject('oversight tokens are revoke-only');

CREATE TABLE oversight_ops (
    id          BIGINT PRIMARY KEY,
    ts          TEXT COLLATE "C" NOT NULL,
    token_id    BIGINT NOT NULL,
    principal   TEXT COLLATE "C" NOT NULL,
    generation  BIGINT NOT NULL,
    batch_ref   TEXT COLLATE "C" NOT NULL,
    op_index    BIGINT NOT NULL,
    op          TEXT COLLATE "C" NOT NULL,
    key         TEXT COLLATE "C",
    finding_id  TEXT COLLATE "C" NOT NULL DEFAULT '',
    reason      TEXT COLLATE "C" NOT NULL DEFAULT '',
    outcome     TEXT COLLATE "C" NOT NULL CHECK (outcome IN ('applied','denied')),
    code        TEXT COLLATE "C",
    event_seq   BIGINT
);
CREATE INDEX idx_oversight_ops_ts ON oversight_ops(principal, ts);
CREATE INDEX idx_oversight_ops_key ON oversight_ops(key);
CREATE TRIGGER trg_oversight_ops_append_only_update BEFORE UPDATE ON oversight_ops
    FOR EACH ROW EXECUTE FUNCTION pgk_reject('oversight_ops is append-only');
CREATE TRIGGER trg_oversight_ops_append_only_delete BEFORE DELETE ON oversight_ops
    FOR EACH ROW EXECUTE FUNCTION pgk_reject('oversight_ops is append-only');
CREATE TRIGGER trg_oversight_ops_append_only_truncate BEFORE TRUNCATE ON oversight_ops
    FOR EACH STATEMENT EXECUTE FUNCTION pgk_reject('oversight_ops is append-only');
CREATE TRIGGER trg_oversight_tokens_append_only_truncate BEFORE TRUNCATE ON oversight_tokens
    FOR EACH STATEMENT EXECUTE FUNCTION pgk_reject('oversight tokens are revoke-only');
"""

from .host_samples_rollup import DDL as HOST_SAMPLES_DDL

MIGRATIONS = (Migration(1, "baseline: sqlite schema v9", _V1),
              Migration(2, "design 3: cards, attempts, decisions, questions, answers, budget", _V2),
              Migration(3, "projector: applied, watermark, alarms", PROJECTOR_PG),
              Migration(4, "governor: metrics", _V4),
              Migration(5, "locks: merge_locks", _V5),
              Migration(6, "jev: jev_shadow", _V6),
              Migration(7, "exec ownership: items exec_workflow_id/exec_run_id/exec_attempt"
                           "/exec_principal/exec_retired_runs", _V7),
              Migration(8, "oversight: tokens, ops audit", _V8),
              Migration(9, "bus: durable messages", MESSAGES_DDL), Migration(10, "federation jobs", SPAWNER_DDL),
              Migration(11, "placement rules", PLACEMENT_RULES_DDL),
              Migration(12, "host samples", HOST_SAMPLES_DDL),
              Migration(13, "operator ownership risk and parking",
                        "ALTER TABLE items ADD COLUMN IF NOT EXISTS risk_tag TEXT;"
                        "ALTER TABLE items ADD COLUMN IF NOT EXISTS until TEXT;"))
LATEST = MIGRATIONS[-1].version

_TABLE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \(\n(.*?)\n\)(?: PARTITION BY RANGE \([^)]*\))?;", re.S)
_COLUMN = re.compile(r'^    "?(\w+)"?\s+\S')
_NOT_COLUMNS = frozenset({"PRIMARY", "CHECK", "UNIQUE", "CONSTRAINT", "FOREIGN"})


def table_bodies(ddl: str) -> dict:
    """``{table: body text}`` of every ``CREATE TABLE`` in *ddl*."""
    return {m.group(1): m.group(2) for m in _TABLE.finditer(ddl)}


def parse_tables(ddl: str) -> dict:
    """``{table: [(column, declared NOT NULL)]}`` in declaration order (for the parity test)."""
    out = {}
    for name, body in table_bodies(ddl).items():
        cols = []
        for line in body.splitlines():
            m = _COLUMN.match(line)
            if m and m.group(1) not in _NOT_COLUMNS:
                cols.append((m.group(1), "NOT NULL" in line))
        out[name] = cols
    for match in re.finditer(
            r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+) ([^;]+);", ddl):
        table, column, declaration = match.groups()
        out.setdefault(table, []).append((column, "NOT NULL" in declaration))
    return out
