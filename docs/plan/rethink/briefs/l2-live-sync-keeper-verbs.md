# L2 brief: build-L2-live-sync-verbs — batched live-sync packet keeper 3fd91bd6 -> main T (keeper verbs #39-#44, D-1542) (HIGH; parent build-L1; TTL 4 h)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding). Never merge, no crons, effort medium.
D-1533: writers/seats non-Claude first (mimo via mimo provider, qwen via qodercli), Sonnet last. D-437/D-1554: on coding.vm ONLY tool-less writers/seats.
NO LIVE STEP: never restart/stop plangraph-keeper.service, never touch the live PG schema `keeper` on cloud.vm, never write 127.0.0.1:8787.
build-L1 runs the live steps after routing GO (D-825).
Live now: keeper 3fd91bd6 on Postgres (LIVE-SYNC-5, record seats-archive/live-sync-gov-7c1ef8be/LIVE-RECORD.md), drop-ins
~/.config/systemd/user/plangraph-keeper.service.d/{live-sync-z5-3fd91bd6.conf, zz-pg-backend.conf}; DSN files ~/.config/plangraph/pg-keeper.dsn
(runtime) / pg-keeper-migrate.dsn (owner) — names only, never print contents. Target T = origin/main 102e4885 (#44 merged).
Batch: #39 PG-ROUTER-REOPEN (new event type item_reopened), #40 PG-L1-CREATE-WITH-EDGE, #41 PG-FOLD-UNKNOWN-TYPE, #42 PG-ESTIMATE-SRC-EDIT,
#43 PG-LE-DEPS-OPS, #44 PG-LE-GUARD-KEY. L2 claim: code-deploy only, no schema/PG migration — VERIFY it (diff 3fd91bd6..T: migrations dir, schema, config keys, roles/grants, MCP tool list).
Router/judge conditions: packet MERGED ON MAIN before GO (build-L1 merges your docs PR); pre-GO dump sha256 in packet; ROLL FORWARD ONLY after the first new-type event (item_reopened): 3fd91bd6 cannot fold it. Rollback to 3fd91bd6 is valid
only while no item_reopened event exists (check query named in packet); after that, rollback = fix forward. FOLD (#41) ships in this batch.
Deliver (branch lane/live-sync-verbs from origin/main, docs only, draft PR, head trailer `Plan-Item: plangraph:PG-ROUTER-REOPEN`):
1. Packet docs/plan/rethink/live-sync-102e4885.md: backup (pg_dump schema keeper as owner to backups/, sha256), worktree at T, drop-in
   `live-sync-z6-102e4885.conf` (sorts after z5; check MainPID environ PYTHONPATH), restart, verify gates (admin verify, verify-snapshot, snapshot 200,
   one reopen-free smoke via API with a scratch-safe read, counts equal), rollback (drop-in removal + restart 3fd91bd6, ONLY before first item_reopened;
   the check query). Name every command exactly.
2. Rehearsal: throwaway local Postgres 17 container on coding.vm (`--memory 1g`, random port, one container, removed at end, MemAvailable >= 1 GB),
   restore a fresh pg_dump of schema keeper (read-only dump via runtime DSN ok) with roles keeper + keeper_rt recreated; run T's code on a scratch
   keeper (free port, scratch token): verify gates, counts equal, exercise reopen + create-with-edge + deps ops + estimate_src edit as keeper_rt;
   prove 3fd91bd6 starts and verifies on the pre-reopen copy (rollback window), and show it fails/flags after an item_reopened (roll-forward-only proof).
   Log commands + rc in docs/plan/rethink/live-sync-102e4885.rehearsal-log.txt (no secrets/DSN).
Seats: 2 non-Claude tool-less (qwen/qodercli ops/qseat.py + gpt-oss or mimo), vectors: no-migration claim, rollback window validity,
grant completeness for keeper_rt on any touched table, secrets hygiene. MERGE-READY HIGH <sha> + PR + rehearsal summary + seat dirs to build-L1.
