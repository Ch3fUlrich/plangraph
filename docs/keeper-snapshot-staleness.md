# Keeper snapshot: staleness contract (KEEPER-SNAPSHOT-PG)

`plan.snapshot.json` (+ `.sig`) is derived from the database. A write is committed before its HTTP
response; the file is rebuilt by one writer thread (`keeper/snapshot_writer.py`) after writes go quiet
for `snapshot_debounce_s` (default 0.25), never later than `snapshot_max_delay_s` (default 2) after
the first unwritten change, plus one build. `snapshot_debounce_s: 0` = old behaviour (rewrite on the
request thread). Shutdown (SIGTERM/SIGINT) flushes; startup rebuilds, so a crash loses no committed
write and leaves no stale file. Replace is atomic (temp + rename; signature first, as before).

| Consumer | Reads | Needs |
|---|---|---|
| `GET /v1/snapshot`, md-sync, `md_sync_archive`, `pg render` | live DB via the API | none (always current) |
| CLI read verbs, local fallback (`cli_verbs.local_snapshot`) | file, keeper unreachable | any complete, verified file; `snapshot_pull.behind_mark` guards older copies |
| `snapshot_serve` / `snapshot_pull` (ssh pull, timer) | file + sig | verified pair; lag <= max_delay + pull interval is fine |
| `pg admin verify-snapshot` | file + sig | verifies whenever the file is level; a reader in the sig/body gap retries once (unchanged) |
| reaper | writes the file every `reap_interval` | unchanged |
| tests | file after a write | call `server.flush_snapshot()` or poll `head_seq` |

Audit: the snapshot rebuild's `audit_log` row is now written by the `system` principal once per
rebuild, not by the writing principal once per write.
