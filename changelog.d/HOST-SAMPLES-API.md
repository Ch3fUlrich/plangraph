Keeper: add PG-only host sample ingest, ingest-only per-host tokens, latest and
range reads, server-clock freshness/gap/restart reporting, partition maintenance,
weighted minute/hour rollups and retention in the keeper daemon (migration 12).
Provision with `pg token --host host-a`; revoke with `pg token --revoke-host ID`.
Seat fixes: refuse non-object ingest at the store boundary, bound incremental rollups,
validate/cap range reads (24 h raw, 30 d minute, 10000 rows), and refuse unknown
revocation ids while keeping repeat revocation idempotent. HTTP JSON null remains
blocked by the API dispatch sentinel; its strict expected-failure regression records it.
