# Keeper snapshot: staleness contract (KEEPER-SNAPSHOT-PG)

`plan.snapshot.json` (+ `.sig`) is derived from the database. A write is committed before its HTTP
response. One automatic writer rebuilds the file from a separate committed read transaction. Each
attempt starts at the later of two deadlines:

- The earlier of `snapshot_debounce_s` (default 0.25) since the newest pending mark and
  `snapshot_max_delay_s` (default 2) since the oldest.
- Completion of the prior automatic attempt plus the greater of the debounce and that attempt's
  elapsed time. Failed attempts also set this gap.

The attempt retires only the generation it selected when capture started and only after publication
succeeds. Subsequent marks keep their original age. In healthy operation a pending mark can wait
for an in-progress build, a finite automatic gap, the maximum delay, and its own build. Failures
extend staleness until a build succeeds. The writer logs failures and raises one alarm per streak.

An explicit flush covers the generation present when it starts and bypasses the automatic gap.
Shutdown (SIGTERM/SIGINT) flushes within the daemon's shutdown lifecycle. Startup rebuilds from
committed state, including changes left pending by a crash or failed publication. With
`snapshot_debounce_s: 0`, a successful rebuild is visible before the response. A failed rebuild
leaves the database write committed and can leave the derived file stale.

Publication replaces the signature before the body with atomic renames. A reader in the gap
retries verification once. A bad or missing signature fails verification.

| Consumer | Reads | Needs |
|---|---|---|
| `GET /v1/snapshot`, md-sync, `md_sync_archive`, `pg render` | live DB via the API | none (always current) |
| CLI read verbs, local fallback (`cli_verbs.local_snapshot`) | file, keeper unreachable | any complete, verified file; `snapshot_pull.behind_mark` guards older copies |
| `snapshot_serve` / `snapshot_pull` (ssh pull, timer) | file + sig | verified pair with the rebuild and pull delays above |
| `pg admin verify-snapshot` | file + sig | verifies whenever the file is level; a reader in the sig/body gap retries once (unchanged) |
| reaper | marks a rebuild or publishes synchronously | when the reap revision changes or a synchronous publication needs retry |
| tests | file after a write | call `server.flush_snapshot()` or poll `head_seq` |

The automatic rebuild uses the `system` principal. Configured capture queues its read audits for
the existing audit writer after it closes the reader.

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

This historical sequential benchmark rarely hit the lock contention caused by a concurrent
rebuild. The configured rebuild now captures and publishes outside the request lock. See the
[Keeper lock timing evidence](keeper/LE-KEEPER-SNAPSHOT-REBUILD-HOLDS-THE-GLOBAL-LOCK-CLIENTS-TIME-OUT.md).
