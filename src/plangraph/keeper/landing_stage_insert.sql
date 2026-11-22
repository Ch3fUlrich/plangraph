INSERT INTO landing_stages(repo, batch_key, stage, attempt, head, started_at_ms, ended_at_ms)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (repo, batch_key, stage, attempt) DO NOTHING
