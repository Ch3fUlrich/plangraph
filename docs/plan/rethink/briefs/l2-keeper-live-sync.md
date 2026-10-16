# L2 brief: build-L2-live-sync — plangraph:KEEPER-LIVE-SYNC (D-1433/1434 catch-up ACCEPTED, prio 2, HIGH; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding). TTL 3 h. Never merge, no crons,
NO live step: never restart/stop plangraph-keeper.service, never write /home/s/.local/share/plangraph/{keeper.db,keeper.json}. build-L1 does
backup + restart after router GO.
Host: CODING.VM (needs the live keeper.db; it never leaves this host). Live tree /home/s/code/plangraph/.claude/worktrees/proposal
= branch worktree-proposal @ f3bac63 (= 7e5a61e + PR #24 diff); main is ~116 commits ahead (schema.py/db.py +166, db_pg, store_cards, ...).
Done means (keeper item, you deliver all but the restart):
1. Target = a named origin/main sha T. Review f3bac63..T for keeper runtime (src/plangraph/keeper, daemon, schema, migrations, config keys
   the daemon now reads vs live keeper.json — list any new required key; names only). Write docs/plan/rethink/live-sync-T.md.
2. Dry run on a COPY: sqlite3 .backup of keeper.db into a 0700 scratch dir under /home/s/code/plangraph-worktrees/.sessions/live-sync/
   (chmod 600, delete at end); run T's code against the copy (daemon or the migration entrypoint, alternate bind port, alternate config
   copy pointing at the copy, signing/snapshot keys by path read-only): migrations apply, per-table row counts before/after equal (or
   explained), every CLI verb smoke-tested read-only + one claim/heartbeat/release on a scratch item IN THE COPY. Record commands + counts.
3. Rollback rehearsed on a second copy: T migrated copy -> does f3bac63 code still start on it? If not, rollback = restore backup (say so).
4. Seats: 2 non-Claude families (gpt-oss + qwen via workst-L1-autoos) on the review doc + dry-run log (no DB contents, no key values).
Write set: docs/plan/rethink/live-sync-T.md + scratch. No code change; if T needs a fix to migrate, STOP and report BLOCKED with the evidence.
Report MERGE-READY-equivalent: "SYNC-READY T=<sha> doc=<path@sha> seats=<branch>" to build-L1.
