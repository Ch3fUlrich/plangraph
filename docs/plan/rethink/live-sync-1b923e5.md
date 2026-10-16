# KEEPER-LIVE-SYNC: live tree f3bac63 -> origin/main T=1b923e5

Plan-Item: plangraph:KEEPER-LIVE-SYNC. Dry run on copies only; live keeper.db/keeper.json/service untouched.
Live = branch worktree-proposal @ f3bac63 (= 7e5a61e + PR #24). T = 1b923e58ba598291aa1a19c6645a64306e452ebf. 116 commits, 171 files; keeper runtime = 51 src files.

## 1. Review f3bac63..T (keeper runtime)
- **Schema** SCHEMA_VERSION 9->10 is a constant only: no version table, no user_version. Migration = idempotent `db._upgrade` on every non-create `open_db`
  (CREATE ... IF NOT EXISTS, DML-free). New tables: cards, attempts, decisions, questions, answers, budget (`schema.DESIGN3_DDL`) and
  projector_applied, projector_watermark, projector_alarms (`projector.ddl.SQLITE`). All empty after migrate. attempts/decisions/answers append-only via triggers.
  No existing table altered; no ALTER on existing columns; `_ADDED_COLUMNS` unchanged.
- **Events** new type `state_set` (`store_cards.set_state`: design-3 states needs_input/waiting/failed/parked/running/ready). `events.append` now assigns `seq` itself (was AUTOINCREMENT): same numbering on SQLite. Hash chain format unchanged.
- **Daemon** `daemon.run` passes `cfg.db_options` to `lock_keeper/open_db`; optional `backup_prune` thread (default OFF). Backend default sqlite; Postgres only if `backend`=postgres in keeper.json or env PG_KEEPER_BACKEND. Postgres modules (db_pg, schema_pg, copy_to_pg) import lazily; system python3 starts fine without psycopg.
- **HTTP** new unauthenticated static `GET /inbox` (form page, 200 on copy); new authed doors /v1/questions, ask_question, answer_question, resignal_question (`inbox.routes`; signal to Temporal only via TEMPORAL_* env, failure never fails the answer).
- **CLI** new `pg admin migrate | copy-to-pg | prune-backups`; `pg admin init` unchanged.
- **Config keys** daemon now reads (all OPTIONAL, defaults safe): `backend`, `dsn`, `pg_schema`, `backup_prune{enabled,dir,keep,daily_days,interval_s}`.
  **New required key: none.** Live keeper.json keys (label, db, snapshot, bind, repos, runs_dir, decisions_file, batch_roots, signing_key, snapshot_pubkey, ci) load unchanged.
- **Env** new optional names: PG_KEEPER_BACKEND, PG_DSN, PG_SCHEMA, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE. Unit env (semaphore vars) unchanged and still read. Keep PG_KEEPER_BACKEND unset.
- **Unit** ExecStart `python3 -m plangraph.keeper.daemon --config ...keeper.json`: module path unchanged. Only the tree changes (WorkingDirectory/PYTHONPATH -> a checkout of T). build-L1 chooses the path.

## 2. Dry run on copy (commands + counts)
Scratch 0700 dir `.sessions/live-sync/`, files 0600: `sqlite3.Connection.backup` of keeper.db (ro URI) -> copyA.db, copyB.db; configs = live keeper.json with db/snapshot/bind changed (127.0.0.1:18787/18788); signing_key/snapshot_pubkey by path (read-only). integrity_check ok.
1. `pg admin verify` (T code, before): chain and cache integrity ok
2. `pg admin migrate`: schema up to date. `pg admin verify`: ok.
3. Row counts before == after for all 10 original tables: aliases 12, audit_findings 34, audit_log 50733, edges 2557, events 6227, evidence 1053, items 1732, outbox_seen 0, principals 24, run_tokens 0. +9 new tables, each 0.
4. Daemon (`python3 -m plangraph.keeper.daemon --config keeper-A.json`) up on 18787, `GET /inbox` 200, `/v1/snapshot` 401 without token.
5. Read verbs rc=0: whoami ready blocked waves critical percent alerts digest progress changelog(--since 0) audit-meta snapshot aliases render(--out) ; admin verify, verify-snapshot (signature ok after first write), prune-backups (dry), migrate (again), replay (no outbox), set-kind/evidence usage paths. `sync pull` needs a reader-config `pull` object (not keeper.json): not applicable. `verify-anchor`: "anchors not configured" (live has no anchor keys; same as before).
6. Write cycle IN THE COPY: define scratch goal `plangraph:LIVESYNC-DRY`, priority, claim (worker, 1h), heartbeat, release, cancel: all rc=0. Chain ok after. Deltas vs step 3: items +1, events +5, principals +2 (two dry principals), audit_log +60 (every read verb logs). Nothing else.
Not exercised: submit/evidence/verdict/waive/withdraw/close/legacy/rehome/delegate/token (--help parsed only), Postgres backend, Temporal signal, backup_prune thread.

## 3. Rollback rehearsal (copyB: migrated by T, then run with f3bac63)
- `git archive f3bac63 src` -> old tree; `admin verify` with OLD code on T-migrated copy: ok; OLD daemon starts and serves (whoami/ready/percent/alerts/snapshot/digest rc=0, `/inbox` 404 as expected); OLD `define` works; verify ok.
- Extra tables are ignored by old code. **But** once T writes a design-3 event (`set_state` -> `state_set`, item state `parked`), OLD `admin verify` reports `cache_mismatch field=state expected=open got=parked`. Therefore: rollback via code swap is safe only while T has written no design-3 state/cards/attempts/questions. After that, **rollback = stop keeper, restore the pre-restart backup db (and json), start f3bac63.** Take that backup before restart (sqlite .backup, not cp, WAL).

## 4. Seats
(see seats/ dir on branch lane/keeper-live-sync-seats results below)
