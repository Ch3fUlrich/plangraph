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

## Bench (tools/bench_keeper_snapshot.py; local postgres:16, 127.0.0.1, 60 sequential writes/size)

Before (main e790e34 + set-based off): PG write p50 95/353/871/1710 ms and SQLite 10/22/59/108 ms at 100/400/1000/2000 items; PG GET snapshot 83/332/870/1661 ms.

After:

| backend | items | write p50 ms | write p95 ms | write max ms | GET snapshot p50 ms | snapshot bytes |
|---|---|---|---|---|---|---|
| sqlite | 100 | 4 | 5 | 15 | 6 | 130412 |
| sqlite | 400 | 5 | 11 | 15 | 14 | 518776 |
| sqlite | 1000 | 4 | 6 | 12 | 48 | 1296579 |
| sqlite | 2000 | 5 | 6 | 6 | 63 | 2597919 |
| backend | items | write p50 ms | write p95 ms | write max ms | GET snapshot p50 ms | snapshot bytes |
|---|---|---|---|---|---|---|
| postgres | 100 | 8 | 16 | 28 | 9 | 130412 |
| postgres | 400 | 8 | 10 | 11 | 18 | 518776 |
| postgres | 1000 | 8 | 11 | 14 | 39 | 1296579 |
| postgres | 2000 | 10 | 16 | 16 | 83 | 2597919 |

Not shown: a write that lands while a rebuild holds the server lock waits up to one build (~ the GET snapshot time at that size, once per max_delay window under a steady stream); sequential runs of ~1 s rarely hit it, so p95/max understate that tail.
