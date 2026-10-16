# card build-L2-snapshot-pg (KEEPER-SNAPSHOT-PG)
branch lane/keeper-snapshot-pg, PR #32 draft. base de11496.
done: set-based _snapshot (2 queries) + SnapshotWriter debounced thread (api/daemon/config), tests, bench, docs/keeper-snapshot-staleness.md
bench PG write p50 @1000: 871 -> 8 ms; flat 100..2000 (8-10); GET snapshot @1000 39 ms
local: sqlite 3174 pass, PG 2584 pass (before final test fix)
seats: gpt-oss pass, qwen pass (no VERDICT line; text pass) in job tmp seat/
CI: all green; .alarm is an in-memory SnapshotWriter attribute (no flag file)
todo: push fix, 3 green CI runs, seat branch, MERGE-READY to build-L1
