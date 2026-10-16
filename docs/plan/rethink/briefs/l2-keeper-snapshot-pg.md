# L2 brief: plangraph:KEEPER-SNAPSHOT-PG (D-1477, prio 1, HIGH; parent build-L1) — build-L2-snapshot-pg (coding.vm, sonnet medium)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding) + skill unattended-orchestration. TTL 4 h. Effort medium.
Hard limits: NO push to main, NO merge, NO crons/beat; stop on any harness/classifier denial and report it verbatim; report ONLY to build-L1 (BLOCKED/MERGE-READY/DONE); no live keeper / systemd / ~/.local/share/plangraph touch.
Item: `ops/pg.sh show plangraph:KEEPER-SNAPSHOT-PG` (full desc + done criteria; build-L1 holds the claim). Gates RT-P1-KEEPER-PG cutover.
Cause (measured by build-L2-pytest-speed, lane/ci-pg-contract-timeout 3d4f2e8 has the latency instrumentation): src/plangraph/keeper/api.py:168 `_snapshot`
-> snapshot_io.write_snapshot -> store.snapshot rebuilds the FULL snapshot after every committed write, before the response; O(items), one PG round trip per small query.
PG per-write p50 99 ms (<100 items) -> 387 ms (300-400); SQLite 15-27 ms; GET /v1/snapshot @400 PG 505 ms vs SQLite 18 ms.
Done means (keeper item, binding): per-write p50 on PG at 1000 items <= 50 ms and flat as item count grows (bench table PG vs SQLite at 100/400/1000/2000);
GET /v1/snapshot <= 100 ms on PG at 1000 items; snapshot bytes identical to the old path for the same state (test); crash/shutdown never loses a committed write or leaves a torn snapshot (test);
pytest + pytest-postgres green, no timeouts, 3 consecutive CI runs; 2 seats incl. non-Claude; judge (build-L1 sends).
Approach (your call, justify with numbers): set-based queries in store.snapshot (both backends) and/or snapshot off the request path (debounced/async writer, bounded staleness, flush on shutdown, atomic rename).
Before choosing: grep every consumer of the snapshot file and /v1/snapshot (md-sync, render timer, admin verify-snapshot + signature, projector, tests) and state the staleness contract each needs (R-orch-11).
Write set: src/plangraph/keeper/** (engine: store*, snapshot_io, api, db_pg/db), tests/**, a bench script under tools/ or tests/bench. Not: schema/DDL migrations without stopping to report first; .github/workflows (ask build-L1).
Test ladder: unit with fakes -> local postgres:16 docker (bench at 100/400/1000/2000 items, PG and SQLite) -> CI. Failing-first tests for byte-identity and crash/shutdown.
L3 writers via the autoos-agent spawner, free-first (mimo-v2.6-flash / muse-spark / OpenRouter free; Sonnet last); one file + exact spec per worker; you review every diff.
Seats: 2, incl. >=1 non-Claude (gpt-oss via ops/seat.sh, Qwen via qodercli), vectors per-file:function (store.snapshot, snapshot_io.write_snapshot, api._snapshot, shutdown path):
byte identity, lost-write/torn-snapshot, staleness vs consumers, PG/SQLite parity, bench honesty. Branch seats/snapshot-pg-<sha>; seat_verdict dry-run rc 0.
Branch lane/keeper-snapshot-pg from origin/main (e790e34 or later); draft PR; every commit trailer `Plan-Item: plangraph:KEEPER-SNAPSHOT-PG`.
CI: workflow_dispatch tests.yml on workst/fleet-ci or ci.vm only, max 1 pytest-postgres run per lane, never cancel a run on the head sha (each dispatch = own check suite; a red/cancelled suite blocks evidence).
MERGE-READY <sha> + bench table + 3 CI run ids + seat branch + consumer staleness list to build-L1. Card <= 40 lines at .sessions/snapshot-pg/card.md; RECYCLE-READY <card> before 230k.
