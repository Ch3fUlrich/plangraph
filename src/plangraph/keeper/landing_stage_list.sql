SELECT repo, batch_key, stage, attempt, head, started_at_ms, ended_at_ms, host, result, detail
FROM landing_stages
WHERE repo = ? AND batch_key = ?
ORDER BY started_at_ms, stage, attempt
