# KEEPER-PG cutover: execution record (2026-10-16)

Plan-Item: plangraph:RT-P1-KEEPER-PG. Runbook: `keeper-pg-cutover.md` (7f5a35a). Code tree b3d71096 (main), unchanged by the cutover.
Target: the PG-DEPLOY cluster (server:RT-P1-PG-DEPLOY, D-1499), database `keeper`, schema `keeper`.
Roles (D-1500, least privilege): `keeper` owns the schema (copy, migrations); `keeper_rt` is the runtime role (DML only, default privileges from `keeper`), LAN rule D-1505.
DSN files by name only, mode 0600, outside git: `~/.config/plangraph/pg-keeper-migrate.dsn` (keeper), `~/.config/plangraph/pg-keeper.dsn` (keeper_rt).

## Timeline (UTC)
| step | GO | time | result |
|---|---|---|---|
| rehearsal 1 (precondition 6) | — | 14:11 | copy/verify/rowhash PASS; runtime FAIL: pg_hba had no keeper_rt rule -> D-1505 |
| rehearsal 2 | — | 14:40 | PASS: 19 tables / 64189 rows, DIFFS 0; daemon under keeper_rt starts with no DDL, DML (heartbeat) ok, `/v1/alarms` `/inbox` `/v1/snapshot` 200; scratch schema dropped |
| 1+2 freeze, backup, copy, verify | D-1506 | 16:42:30-16:42:44 | PASS (below) |
| 3 switch | D-1506b | 16:43:16 | PASS (below) |
| 4 delete-or-archive sqlite | pending | not before 2026-10-18 16:43 | router GO |

## Step 1+2 gates
- Boundary (sqlite head at freeze): seq 6329, hash `ac87778fa37f1531be6c232f439b2589f0037eb0743a78144d79db2fe503ada0`.
- Frozen sqlite: WAL empty, chmod 0400, sha256 `6e6e96092361809f718feb690b61d534f09e79d880ded6ca956783412fbe36d5`.
- Backup dir `backups/pg-cutover-b3d71096-20261016T164230Z` (keeper.db sha256 `dc7fb396...`, counts.json, keeper.json, snapshot + sig).
- `admin copy-to-pg --apply`: "copied and verified"; source = target per table:

| table | rows | | table | rows |
|---|---|---|---|---|
| events | 6329 | | audit_log | 52688 |
| items | 1743 | | audit_findings | 36 |
| evidence | 1079 | | principals / aliases | 24 / 12 |
| edges | 2569 | | 11 other tables | 0 |

  total 64480.
- `admin verify` on PG: "chain and cache integrity ok". `admin verify-snapshot`: "snapshot signature ok". `rowhash.py` (all 19 tables, PK order): DIFFS 0. PG head seq 6329, same hash.

## Step 3 switch
- Unit dir backed up as `plangraph-keeper.service.d.pre-pg-20261016T164313Z`; drop-in `zz-pg-backend.conf` (sorts after `live-sync-*`):
  `EnvironmentFile=` the keeper_rt DSN file, `PG_KEEPER_BACKEND=postgres`, `PG_SCHEMA=keeper`, `ExecStart=` a venv python (system-site + psycopg 3.3.6; system python3 has no psycopg) with the same daemon args.
- After start: `admin verify` on PG ok; loss boundary recorded before any timer or replay: seq 6329 hash `ac87778f...` (= freeze head, no writes yet); whoami, ready, percent rc0; `/v1/alarms` 200, `/v1/snapshot` 200 (0.82 s); md-sync path/timer and render timer restarted.
- Router verified (whoami/show/snapshot 200) and sent KEEPER WRITES OPEN.

## Rollback (runbook section 7)
Exact path only while `max(seq)` on PG = 6329; after the first write the after-writes path applies (D-1500: list `seq > 6329` from PG and re-apply by hand through the client verbs). The frozen file and the backup stay until the step-4 GO.

## Lessons
- A runtime role needs its own pg_hba line; the rehearsal caught it before the freeze (keep the runtime half in every rehearsal).
- systemd drop-ins apply in lexical order: name an override so it sorts last and check the MainPID environment after start.
