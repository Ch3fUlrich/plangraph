# L2 brief: build-L2-live-sync-gov — live-sync packet for keeper -> main 3fd91bd6 (RT-P1-GOVERNOR, migration 4) (HIGH; parent build-L1; TTL 4 h)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding). Never merge, no crons, effort medium.
D-1533: writers/seats non-Claude first (mimo via mimo provider, qwen via qodercli), Sonnet last.
NO LIVE STEP: never restart/stop plangraph-keeper.service, never touch the live PG schema `keeper` on cloud.vm, never write 127.0.0.1:8787.
build-L1 runs the live steps after routing GO (D-825).
Live now: keeper b3d71096 on Postgres (cloud.vm schema keeper, owner role keeper, runtime role keeper_rt = DML only, D-1500), drop-ins
~/.config/systemd/user/plangraph-keeper.service.d/{live-sync-z4-b3d71096.conf, zz-pg-backend.conf}; DSN files ~/.config/plangraph/pg-keeper.dsn
(runtime) / pg-keeper-migrate.dsn (owner) — names only, never print contents. Target T = origin/main 3fd91bd6.
Judge conditions (server-L1-routing ACCEPT, seats-archive/governor-8c9b0616/DONE-NOTE.md): (1) `pg admin migrate` (migration 4) as owner keeper;
(2) GRANT SELECT, INSERT, UPDATE ON keeper.metrics TO keeper_rt; (3) rehearse on a restored copy and prove set_metrics as keeper_rt before the switch.
Deliver (branch lane/live-sync-3fd91bd6 from origin/main, docs only, draft PR, head trailer `Plan-Item: plangraph:RT-P1-GOVERNOR`):
1. Review b3d71096..3fd91bd6 keeper runtime: migrations, new config keys, new verbs/roles; list. Packet docs/plan/rethink/live-sync-3fd91bd6.md:
   backup (pg_dump schema keeper as owner to backups/, sha256), migrate as owner, GRANT, worktree/drop-in `live-sync-z5-3fd91bd6.conf` (sorts after z4;
   check MainPID environ PYTHONPATH), restart, verify gates (admin verify, verify-snapshot, snapshot 200, set_metrics as keeper_rt via API with a
   role-allowed token, counts equal), rollback (drop-in removal + restart b3d71096; migration 4 additive -> code b3d71096 must still start on schema 4:
   PROVE it in rehearsal, else rollback = restore dump). Name every command exactly.
2. Rehearsal: throwaway local Postgres 16 container on coding.vm (`--memory 1g`, random port, one container, removed at end, MemAvailable >= 1 GB),
   restore a fresh pg_dump of schema keeper (read-only dump via runtime DSN is fine) with roles keeper + keeper_rt recreated; run steps 1..verify
   with T's code on a scratch keeper (free port, scratch token); prove set_metrics as keeper_rt; prove b3d71096 starts on schema 4. Log commands + rc
   in docs/plan/rethink/live-sync-3fd91bd6.rehearsal-log.txt (no secrets/DSN).
Seats: 2 non-Claude (qwen/qodercli ops/qseat.py + mimo ops/seat.sh or gpt-oss), vectors: grant completeness (sequence privileges if metrics has one),
rollback validity, migration ordering, secrets hygiene. MERGE-READY HIGH <sha> + PR + rehearsal summary + seat dirs to build-L1.
