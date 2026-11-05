"""Migration 10: federation storage only; rollback requires a migrate connection."""

PG = """
CREATE OR REPLACE FUNCTION spawner_chunks_valid(chunks BYTEA[], total_size BIGINT)
RETURNS BOOLEAN LANGUAGE SQL IMMUTABLE AS $$
    SELECT COALESCE(array_ndims(chunks), 1) = 1
       AND NOT EXISTS (SELECT 1 FROM unnest(chunks) c
                       WHERE c IS NULL OR octet_length(c) > 4194304)
       AND COALESCE((SELECT sum(octet_length(c)) FROM unnest(chunks) c), 0)
           = total_size
$$;
CREATE TABLE spawner_blobs (
    blob_id TEXT COLLATE "C" PRIMARY KEY,
    sealed BOOLEAN NOT NULL DEFAULT FALSE,
    sha256 TEXT COLLATE "C" NOT NULL DEFAULT '',
    total_size BIGINT NOT NULL DEFAULT 0 CHECK (total_size BETWEEN 0 AND 67108864),
    chunks BYTEA[] NOT NULL DEFAULT '{}' CHECK (spawner_chunks_valid(chunks, total_size)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '7 days')
);
CREATE TABLE spawner_hosts (
    host_id TEXT COLLATE "C" PRIMARY KEY,
    probe JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE spawner_jobs (
    run_id TEXT COLLATE "C" NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    attempt_id TEXT COLLATE "C" NOT NULL UNIQUE,
    target_host TEXT COLLATE "C" NOT NULL,
    request JSONB NOT NULL DEFAULT '{}',
    payload_hash TEXT COLLATE "C" NOT NULL,
    status TEXT COLLATE "C" NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued','claimed','running','completed','failed','cancelled','withdrawn','cancelling','unknown')),
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    record_json JSONB NOT NULL DEFAULT '{}',
    result_blob_id TEXT COLLATE "C" DEFAULT NULL REFERENCES spawner_blobs(blob_id),
    salvage_blob_id TEXT COLLATE "C" DEFAULT NULL REFERENCES spawner_blobs(blob_id),
    PRIMARY KEY (run_id, attempt_no)
);
CREATE INDEX idx_spawner_jobs_target_status ON spawner_jobs(target_host, status);
CREATE UNIQUE INDEX idx_spawner_jobs_live ON spawner_jobs(run_id)
    WHERE status IN ('claimed','running','unknown','cancelling');
CREATE TABLE spawner_finalizations (
    run_id TEXT COLLATE "C" PRIMARY KEY,
    finalizer_id TEXT COLLATE "C" NOT NULL,
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    lease_expires_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expected_old TEXT COLLATE "C" NOT NULL DEFAULT '',
    result_blob_id TEXT COLLATE "C" DEFAULT NULL REFERENCES spawner_blobs(blob_id),
    publication_state TEXT COLLATE "C" NOT NULL DEFAULT 'not_started' CHECK (publication_state IN
        ('not_started','pending','publishing','published','finalize_conflict')),
    step_log JSONB NOT NULL DEFAULT '[]'
);
CREATE OR REPLACE FUNCTION spawner_require_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM spawner_jobs WHERE run_id = NEW.run_id) THEN
        RAISE EXCEPTION 'spawner_unknown_run' USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER spawner_finalizations_run BEFORE INSERT
    ON spawner_finalizations FOR EACH ROW EXECUTE FUNCTION spawner_require_run();
CREATE TRIGGER spawner_finalizations_run_update BEFORE UPDATE OF run_id
    ON spawner_finalizations FOR EACH ROW EXECUTE FUNCTION spawner_require_run();
"""

TABLES = ("spawner_blobs", "spawner_hosts", "spawner_jobs", "spawner_finalizations")


class SpawnerRollbackRefused(RuntimeError):
    """Rollback cannot safely retire migration 10."""


def rollback(conn):
    """Atomically retire all four tables, preserving rows, constraints and indexes.

    Accepts a raw psycopg migrate-role connection with the keeper search_path set.
    Retired tables deliberately prevent another rollback, even after re-migrate.
    """
    from .db_pg import _MIGRATE_LOCK

    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(current_schema()))",
                     (_MIGRATE_LOCK,))
        retired = [t + "_retired_v10" for t in TABLES if conn.execute(
            "SELECT to_regclass(%s)", (t + "_retired_v10",)).fetchone()[0]]
        if retired:
            raise SpawnerRollbackRefused("spawner_retired_tables_exist: " + ", ".join(retired))
        conn.execute("LOCK TABLE " + ", ".join(TABLES) + " IN ACCESS EXCLUSIVE MODE")
        if conn.execute("SELECT 1 FROM spawner_jobs WHERE status NOT IN "
                        "('completed','failed','cancelled','withdrawn') LIMIT 1").fetchone():
            raise SpawnerRollbackRefused("spawner_non_terminal_jobs")
        # Index names are schema-global: free them without losing the retired indexes.
        for table in TABLES:
            indexes = conn.execute("SELECT indexname FROM pg_indexes WHERE "
                                   "schemaname=current_schema() AND tablename=%s", (table,))
            for (index,) in indexes:
                conn.execute(f'ALTER INDEX "{index}" RENAME TO "{index}_retired_v10"')
            conn.execute(f'ALTER TABLE {table} RENAME TO {table}_retired_v10')
        conn.execute("DELETE FROM schema_migrations WHERE version=10")
