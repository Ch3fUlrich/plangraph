Keeper: add PG-only host sample ingest, ingest-only per-host tokens, latest and
range reads, server-clock freshness/gap/restart reporting, partition maintenance,
weighted minute/hour rollups and retention in the keeper daemon (migration 12).
Provision with `pg token --host host-a`; revoke with `pg token --revoke-host ID`.
