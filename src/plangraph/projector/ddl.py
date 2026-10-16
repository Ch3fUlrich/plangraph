"""Projector tables (keeper migration 3): idempotency ledger, watermarks, drift alarms.

``SQLITE`` is installed by ``keeper.db._upgrade`` (``IF NOT EXISTS``, safe for
``executescript``); ``PG`` is migration 3 in ``keeper.schema_pg`` (same logical tables,
``COLLATE "C"``, BIGINT).  ``projector_alarms.id`` is assigned by the projector
(MAX(id)+1 inside the keeper write lock) on both backends, like ``run_tokens.id``.
No imports here: ``schema_pg`` loads this module.
"""
from __future__ import annotations

__all__ = ["SQLITE", "PG", "LATEST_PROJECTOR_MIGRATION"]

LATEST_PROJECTOR_MIGRATION = 3

SQLITE = """
CREATE TABLE IF NOT EXISTS projector_applied (
    workflow_id TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    activity_id TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('attempt','state')),
    key         TEXT NOT NULL,
    n           INTEGER,
    applied_at  TEXT NOT NULL,
    PRIMARY KEY (workflow_id, run_id, activity_id, attempt, kind)
);

CREATE TABLE IF NOT EXISTS projector_watermark (
    workflow_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL DEFAULT '',
    run_ord     INTEGER NOT NULL DEFAULT 0,
    event_id    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT '',
    plan_key    TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projector_alarms (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    workflow_id TEXT NOT NULL DEFAULT '',
    key         TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    raised_at   TEXT NOT NULL,
    cleared_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projector_alarms_open
    ON projector_alarms(kind, workflow_id, key) WHERE cleared_at IS NULL;
"""

PG = """
CREATE TABLE projector_applied (
    workflow_id TEXT COLLATE "C" NOT NULL,
    run_id      TEXT COLLATE "C" NOT NULL,
    activity_id TEXT COLLATE "C" NOT NULL,
    attempt     BIGINT NOT NULL,
    kind        TEXT COLLATE "C" NOT NULL CHECK (kind IN ('attempt','state')),
    key         TEXT COLLATE "C" NOT NULL,
    n           BIGINT,
    applied_at  TEXT COLLATE "C" NOT NULL,
    PRIMARY KEY (workflow_id, run_id, activity_id, attempt, kind)
);

CREATE TABLE projector_watermark (
    workflow_id TEXT COLLATE "C" PRIMARY KEY,
    run_id      TEXT COLLATE "C" NOT NULL DEFAULT '',
    run_ord     BIGINT NOT NULL DEFAULT 0,
    event_id    BIGINT NOT NULL DEFAULT 0,
    status      TEXT COLLATE "C" NOT NULL DEFAULT '',
    plan_key    TEXT COLLATE "C" NOT NULL DEFAULT '',
    updated_at  TEXT COLLATE "C" NOT NULL
);

CREATE TABLE projector_alarms (
    id          BIGINT PRIMARY KEY,
    kind        TEXT COLLATE "C" NOT NULL,
    workflow_id TEXT COLLATE "C" NOT NULL DEFAULT '',
    key         TEXT COLLATE "C" NOT NULL DEFAULT '',
    detail      TEXT COLLATE "C" NOT NULL DEFAULT '',
    raised_at   TEXT COLLATE "C" NOT NULL,
    cleared_at  TEXT COLLATE "C"
);
CREATE UNIQUE INDEX idx_projector_alarms_open
    ON projector_alarms(kind, workflow_id, key) WHERE cleared_at IS NULL;
"""
