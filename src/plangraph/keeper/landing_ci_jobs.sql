CREATE TABLE IF NOT EXISTS landing_ci_jobs (
    repo          TEXT NOT NULL,
    batch_key     TEXT NOT NULL,
    run_id        BIGINT NOT NULL,
    attempt       BIGINT NOT NULL,
    job_id        BIGINT NOT NULL,
    head          TEXT NOT NULL,
    name          TEXT NOT NULL,
    conclusion    TEXT NOT NULL,
    run_conclusion TEXT NOT NULL,
    started_at_ms BIGINT NOT NULL,
    ended_at_ms   BIGINT NOT NULL CHECK (ended_at_ms >= started_at_ms),
    PRIMARY KEY (repo, batch_key, run_id, attempt, job_id)
);
