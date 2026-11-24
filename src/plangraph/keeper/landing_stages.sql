CREATE TABLE IF NOT EXISTS landing_stages (
    repo          TEXT NOT NULL CHECK (length(repo) > 0),
    batch_key     TEXT NOT NULL CHECK (length(batch_key) > 0),
    stage         TEXT NOT NULL CHECK (stage IN (
                      'writing', 'review', 'fast_checks', 'push', 'ci',
                      'delta_review', 'go', 'merge', 'host_sync')),
    attempt       BIGINT NOT NULL CHECK (attempt > 0),
    head          TEXT NOT NULL CHECK (length(head) IN (40, 64)),
    started_at_ms BIGINT NOT NULL,
    ended_at_ms   BIGINT NOT NULL CHECK (ended_at_ms >= started_at_ms),
    host          TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT '',
    detail        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (repo, batch_key, stage, attempt)
);
CREATE INDEX IF NOT EXISTS idx_landing_stages_timeline
    ON landing_stages(repo, batch_key, started_at_ms, stage, attempt);
