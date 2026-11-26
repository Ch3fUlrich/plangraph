# Keeper snapshot rebuild lock timing

Configured snapshot rebuilds capture the graph, evidence, aliases, head, and fleet host rows in
one committed transaction. They close the reader before projection and signed publication.
Configured message polls use fresh committed transactions. The merge refresh warms a detached
index and hands the completed bundle to the closing request.

## Local query timings

The unit is one successful authenticated HTTP request. Each arm seeded the same SQLite WAL
fixture with two items and their goal. It issued 30 rounds of `show/demo:A?fmt=json`,
`ready?fmt=json`, and `snapshot` in order with the same reader and without concurrent writes.
All 90 requests in each arm returned 200 and had a `request_timing` line.

The baseline used `62c76b1bee983aa6aba55edf691c453c361df8d3` from
`[2026-11-26T17:26:38.029949Z, 2026-11-26T17:26:39.152421Z)`. The implementation
fixture ran from `[2026-11-26T17:39:05.271586Z, 2026-11-26T17:39:06.140649Z)`.
For each verb, select its `query_ms` from the emitted `request_timing` lines. Sort the
values, take the median, and take the nearest-rank 95th percentile at index `ceil(n × .95) - 1`.
The request timer reports query, hold, and wait in separate fields.

| Verb | Before n | Before p50 / p95 (ms) | After n | After p50 / p95 (ms) |
| --- | ---: | ---: | ---: | ---: |
| show | 30 | 1 / 1 | 30 | 0 / 1 |
| ready | 30 | 1 / 1 | 30 | 0 / 1 |
| snapshot | 30 | 1 / 1 | 30 | 0 / 1 |

These small local sequential samples have millisecond rounding, no production traffic, and no
concurrent writer load. Cache and scheduler variation can account for a one-millisecond shift.
The comparison is inconclusive for a query latency improvement.

## Automatic writer and live verification

A separate run on the same fixture issued 30 serial marks with a 10 ms debounce, waiting for
the worker to retire each generation before making the next mark. From
`[2026-11-26T17:41:38.063197Z, 2026-11-26T17:41:39.130505Z)` it emitted 30
`snapshot_build mode=automatic outcome=ok` lines. Using the same percentile method, elapsed
p95 was **3 ms** and global lock hold p95 was **0 ms** against the **250 ms** target. The
configured worker runs outside that lock. The fixture did not measure production staleness,
writer elapsed under production load, or ring saturation at incident rates.

The lock history retains 512 completed outer holds. Wait reporting includes every shared-lock
auth acquisition, including refusals, and every wait longer than 2 seconds. Holder ties use the
reported integer milliseconds after summing nanosecond overlaps per group. Labelled holds use
the validated route names plus `auth`, `writer`, `snapshot`, `messages`, `merge_refresh`,
`changelog`, `reaper`, `host_samples`, and `read_audit`. Other raw holds use
`unclassified` and contribute zero labelled coverage.

Legacy message polls emit one `request_timing` summary per HTTP request. Each
database read still uses the `messages` lock label, so the lock logs can attribute
contention to individual reads.

Production evidence remains pending. Use a two-hour window with reviews active and at least
100 successful automatic builds. Report elapsed and hold p95, query timing, staleness, and
timeouts by observed primary holder or unknown. For each wait over 2 seconds require at least
9,000 basis points of labelled coverage and zero overflow. Treat missing logs or any overflow
as inconclusive unless a confirmed writer, snapshot, messages, or merge refresh breach already
fails the window. Count each equally large primary group on a tie. Cancel holds are tracked
under the separate cancel item.
