# Keeper host samples (HOST-SAMPLES-API, D-5038)

Postgres-only store for fleet host telemetry (design: `docs/plans/2026-10-16-orchestration-setup.md` 12.1, keeper side).
SQLite refuses these verbs with `unavailable`. Code: `store_host_samples.py` (verbs, validation, host principal) and
`host_samples_rollup.py` (rollups, retention); schema = migration 12 in `schema_pg.py`.

- **Tables:** `host_samples_raw` (range-partitioned by `received_at`, daily partitions, two future days kept ready by
  `host_samples_partitions()`, an owner-installed `SECURITY DEFINER` function, so `keeper_rt` needs no DDL),
  `host_samples_1m`, `host_samples_1h` (rollups), `host_sample_tokens` (sha256 only).
- **Host token:** `pg token --host <host_id>` (router/l1) mints a `pgh1_` token shown once; `pg token --revoke-host <id>`
  revokes it (unknown ids refuse with `not_found`; repeat revocation is idempotent). A host token authenticates as a host principal that may only call `ingest_host_sample` for its own
  `host_id`: no reads, no plan verbs, no other host. Run, oversight and normal tokens cannot ingest.
- **Ingest:** `POST /v1/ingest_host_sample`. The payload is numbers only (host_id, seq, agent_started_at, CPU %,
  memory available, swap-in rate, disk free, GPUs, process counts of the four agent CLIs, numeric overrides of the
  allowed rule keys); any other field or a string where a number belongs is refused and nothing is stored.
  `received_at` is the keeper clock. A `seq` jump is stored as `seq_gap`; a new `agent_started_at` as `restarted`.
- **Read (normal principals):** `GET /v1/host_samples` = latest row per host with `state` fresh / stale (no sample for
  3 x `PG_HOST_SAMPLE_S`, default 60 s) / down (10 x); `GET /v1/host_samples_range` with host, start, end
  (half-open) and resolution `raw|1m|1h`. Invalid bounds or reversed ranges refuse with HTTP 400.
  Raw spans are capped at 24 h, 1 m spans at 30 d; 1 h has no span cap within retention.
  Results are ordered and limited to 10000 rows: below the cap the response is a list;
  when more than 10000 rows match it is `{ "rows": [first 10000], "truncated": true }`.
- **Maintenance:** the keeper daemon's own thread (`keeper-host-samples`, every 60 s, no cron) creates partitions,
  writes sample-count-weighted 1 m and 1 h rollups of closed buckets and deletes raw rows after 14 d, 1 m after 90 d and
  1 h after 730 d. Re-running it is idempotent. Subsequent ticks reaggregate only from the latest
  target bucket minus one bucket, preserving recent late samples without rescanning retained history.
