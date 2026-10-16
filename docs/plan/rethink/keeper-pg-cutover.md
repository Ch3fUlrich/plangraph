# KEEPER-PG cutover: SQLite -> Postgres, blue-green runbook (NOT executed)

Plan-Item: plangraph:RT-P1-KEEPER-PG-PREP (prep for plangraph:RT-P1-KEEPER-PG). Base 8165c22. Nothing here touched the live keeper or cloud.vm.
Rehearsal on a throwaway PG16 container from a copy of backup `live-sync-8165c22-20261016T082054Z` (db sha256 f82b3563...): log `keeper-pg-cutover.rehearsal-log.txt`.
`pg` below = `python3 -m plangraph.keeper.cli`; `$KJ` = `~/.local/share/plangraph/keeper.json`; `$LIVE` = `~/.local/share/plangraph`. DSNs and secret files are named, never printed.

## 0. Rehearsal result (copy of live backup, 62902 rows)
| table | sqlite | pg | | table | sqlite | pg |
|---|---|---|---|---|---|---|
| events | 6254 | 6254 | | audit_log | 51225 | 51225 |
| items | 1734 | 1734 | | audit_findings | 34 | 34 |
| evidence | 1060 | 1060 | | cards, attempts, decisions, questions, answers, budget, run_tokens, outbox_seen, projector_* (9 tables) | 0 | 0 |
| edges | 2559 | 2559 | | aliases / principals | 12 / 24 | 12 / 24 |
Chain head seq=6254 hash=0d6f6305...5eb9 equal on both. Extra gate (rehearsal only, `rowhash.py` = sha256 of every row in PK order, all 19 tables): 0 diffs.
Gates green on PG: `admin copy-to-pg --dry-run` rc0; `--apply` rc0 ("copied and verified"); 2nd `--apply` rc2 "target is not empty" (re-run safe); `admin verify` rc0; `admin verify-snapshot` rc0; `admin migrate` rc0.
Scratch keeper on PG (own port, never 8787): whoami, ready, show plangraph:RT-P1-KEEPER-PG, percent rc0; `/v1/alarms` `/inbox` `/v1/questions` HTTP 200; `admin verify` rc0 with daemon up.
Findings (no copy_to_pg defect): (a) **no read-only mode exists in the keeper** (see 3, router decision R1); (b) `admin verify-anchor` rc2 "anchors not configured" on this config (live keeper.json has no anchor keys; skip gate unless anchors get enabled);
(c) `verify-snapshot` needs the `cryptography` package (live python has it; a scratch venv did not); (d) `show` needs the project-qualified key.

## 1. Preconditions (all must hold before step 2)
1. `server:RT-P1-PG-DEPLOY` done and verified by router: PG16 reachable from the keeper host, database + schema exist, two roles: **migrator** (owns tables; needed because copy-to-pg toggles `ALTER TABLE ... DISABLE TRIGGER USER`) and **runtime** (no DDL rights, `open_db(create=False)` refuses a behind schema).
2. Two secret files by NAME (paths come from PG-DEPLOY, mode 0600, outside git): `<PG_MIGRATOR_DSN_FILE>`, `<PG_RUNTIME_DSN_FILE>`. A systemd `EnvironmentFile=` drop-in exports `PG_DSN` for the runtime role from the second; keeper.json carries NO dsn (config stays secret-free).
3. PG backup in place and restore-tested (PG-DEPLOY/PG-BACKUP-RETENTION): pgBackRest or `pg_dump` of the empty target schema before step 4.
4. Code: live tree contains the PG backend (live = 8165c22 includes copy_to_pg.py; D-1463). `pg_schema` fixed name (e.g. `keeper`), the SAME value for copy and runtime (default is `k_<sha1 of db path>`, so an unset value works only while the `db` path never changes). `db` path in keeper.json stays unchanged (it anchors the schema name, `keeper.alive`, config-relative files).
5. Free disk >= 3x keeper.db; `systemctl --user is-active plangraph-keeper` = active; no open GO window of another live step (live-sync etc.).
6. Rehearse once more on the router's PG host with a fresh backup copy (section 4 steps with scratch config) before the GO; compare counts to this doc.

## 2. Freeze (T0; writers pause, order matters)
Writers of the live keeper: md-sync (timer + path + service), L1/L2 clients (`pg claim|heartbeat|submit|evidence`, via the client outbox), the CI-evidence/reaper threads inside the daemon. render is read-only but stop it too so no tool reads half-states.
```
systemctl --user stop plangraph-md-sync.timer plangraph-md-sync.path plangraph-render.timer
systemctl --user stop plangraph-md-sync.service plangraph-render.service
systemctl --user is-active plangraph-md-sync.service plangraph-md-sync.timer plangraph-md-sync.path   # all inactive
systemctl --user stop plangraph-keeper            # explicit stop: Restart=on-failure does not undo it
systemctl --user is-active plangraph-keeper       # inactive
[ ! -e $LIVE/keeper.db.lock ] || fuser $LIVE/keeper.db.lock   # missing file or empty output = no holder
```
Clients that write while it is down queue in their own outbox (`PG_OUTBOX`) and replay AFTER the switch with `pg replay`; announce the window to L1s first (router message) so they do not claim mid-freeze. `plangraph-md-sync-halt.service` is a stopper only; leave it.
Backup of the stopped db (folds WAL): `mkdir -p -m 700 $LIVE/backups/pg-cutover-<sha>-<UTC> && python3 -c "import sqlite3;s=sqlite3.connect('file:$LIVE/keeper.db?mode=ro',uri=True);d=sqlite3.connect('$LIVE/backups/pg-cutover-<sha>-<UTC>/keeper.db');s.backup(d)"`, then `sha256sum` it, record `select count(*)` of all 19 tables from it into `counts.json` (as `counts-before.json` in earlier backups), copy `$KJ`, `plan.snapshot.json(.sig)` beside it, `chmod 0600`. All following steps read ONLY this backup (`--src`); the live file stays untouched.

## 3. Old keeper "read-only 48 h" (router decision R1 before GO)
The keeper has **no read-only flag** (grep of `src/plangraph/keeper`: none; adding one is a separate HIGH code change). Options:
- **R1a (recommended, no code):** old unit stays **stopped** for 48 h, `chmod 0400 $LIVE/keeper.db` and keep the backup; reads go to the new PG keeper; offline readers use the signed `plan.snapshot.json` fallback (`pg sync pull`, cli_local READ-ONLY fallback). Writes to the old keeper are impossible by construction.
- R1b: a read-only mode patch (HIGH lane) so the old keeper serves reads on a second port. Blocks the cutover on that lane.
Rollback (section 7) works with R1a only if nothing wrote to PG that matters (see 7).

## 4. Copy (T0+, after freeze and backup)
Config for the copy = a copy of `$KJ` with `"backend":"postgres","pg_schema":"keeper"` added, saved 0600 as `$LIVE/keeper-pg.json` (no dsn key; `db` unchanged). Export the MIGRATOR dsn from `<PG_MIGRATOR_DSN_FILE>` into `PG_DSN` in that shell only (never echo, never in argv).
```
B=$LIVE/backups/pg-cutover-<sha>-<UTC>/keeper.db
pg --config $LIVE/keeper-pg.json admin copy-to-pg --dry-run --src $B     # rc0, compare counts to section 0 style table + chain head
pg --config $LIVE/keeper-pg.json admin copy-to-pg --apply   --src $B     # rc0, "copied and verified"; rc2 = nothing committed (single transaction)
```
`--apply` refuses a non-empty target; to retry after a rollback just re-run; to redo a committed copy drop the schema (PG-DEPLOY owner) and `admin migrate`.

## 5. Verify gates (all must pass; any failure = abort to section 7, still no live impact)
1. Per-table counts: dry-run source column == `--apply` target column for all 19 tables (the tool compares and rolls back on diff); re-check independently: `psql "$PG_DSN" -Atc "select count(*) from keeper.events"` etc. vs `$LIVE/backups/pg-cutover-*/counts` (sqlite `select count(*)`).
1b. Optional row-level digest (rehearsal tool, no writes; the rehearsal ran it with a venv python holding psycopg): `<keeper python, must import psycopg> docs/plan/rethink/keeper-pg-cutover.rowhash.py $B "$PG_DSN" <src dir> ` -> 19 lines `ok`, `DIFFS 0` (edit the `search_path` line to the real schema name first; the rehearsal used `rehearsal`). Catches in-row differences that counts and head cannot.
2. Chain: `PG_KEEPER_BACKEND=postgres pg --config $LIVE/keeper-pg.json admin verify` -> `chain and cache integrity ok`; head seq/hash printed by copy-to-pg equals `select seq,hash from keeper.events order by seq desc limit 1`.
3. `pg --config $LIVE/keeper-pg.json admin verify-snapshot --snapshot $LIVE/backups/pg-cutover-*/plan.snapshot.json --pubkey ~/.config/plangraph/snapshot-signing.pub` -> `snapshot signature ok`.
4. Smoke on a scratch keeper on PG (runtime DSN, own port e.g. 18787, scratch token minted with `admin add-principal` **only if** it is done on a throwaway schema copy; otherwise use a reader token already in the copied principals): `pg whoami`, `ready`, `show plangraph:RT-P1-KEEPER-PG`, `percent` rc0; `curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $(cat <token file>)" http://127.0.0.1:18787/v1/alarms` = 200; `/inbox` = 200. Stop the scratch keeper.
5. Optional `admin verify-anchor` only if anchors are configured.
6. Router checks `ready` output from PG equals `ready` from the (stopped) sqlite backup via a scratch sqlite keeper (same top 10 lines).

## 6. Switch (needs GO, section 8)
1. Back up the unit config: `cp -a ~/.config/systemd/user/plangraph-keeper.service.d{,.pre-pg}`.
2. Add drop-in `~/.config/systemd/user/plangraph-keeper.service.d/pg-backend.conf`:
```
[Service]
# EnvironmentFile exports PG_DSN (runtime role); systemd allows comments only on their own line
EnvironmentFile=<PG_RUNTIME_DSN_FILE>
Environment=PG_KEEPER_BACKEND=postgres PG_SCHEMA=keeper
```
(or `backend`/`pg_schema` keys in `$KJ`; `select_backend()` in `db.py` takes the explicit config value first, else `PG_KEEPER_BACKEND`.) `$KJ` stays the same file otherwise; keep a dated copy of it.
3. `systemctl --user daemon-reload && systemctl --user start plangraph-keeper`; wait for `GET http://127.0.0.1:8787/inbox` = 200 (the keeper takes a PG session advisory lock; second start elsewhere fails with KeeperLocked, as intended).
4. Re-run gates 5.2 (admin verify on PG), `pg whoami`, `ready`, `percent`, `/v1/alarms`; `systemctl --user start plangraph-md-sync.timer plangraph-md-sync.path plangraph-render.timer`; clients run `pg replay`.
5. Record in the lane card: start time, head seq, counts, sha of the tree.

## 7. 48 h window and rollback
- During 48 h: old sqlite file untouched (chmod 0400), backup dir kept, PG backups running. After 48 h with no incident and a second `admin verify`: router GO to retire (delete nothing automatically; archive the file).
- Rollback BEFORE any PG write that matters (nothing since switch except reads/heartbeats): `systemctl --user stop plangraph-keeper`; remove drop-in `pg-backend.conf` (or restore `plangraph-keeper.service.d.pre-pg`); `chmod 0600 $LIVE/keeper.db`; `rm -f $LIVE/keeper.db-wal $LIVE/keeper.db-shm`; `daemon-reload`; `start`; then in a clean shell (`unset PG_DSN PG_KEEPER_BACKEND PG_SCHEMA`, so the CLI hits the sqlite config) `pg --config $KJ admin verify`, `whoami`, `ready` rc0; re-run md-sync/render timers. The sqlite file is the pre-freeze state, so this is exact.
- Rollback AFTER PG took writes: the sqlite file does NOT contain them. Either accept the loss window (list writes since the switch from PG `select seq,type,key from keeper.events where seq>6254`), or re-import them by hand through the client verbs. Decide with the router before the GO; document the head seq at switch time as the loss boundary.
- Abort in steps 4-5 needs only: scratch cleanup and un-freeze (start timers, `systemctl --user start plangraph-keeper` on the unchanged sqlite config).

## 8. GO the router must give (D-825 form)
Separate explicit GO per step, each naming sha and scope; ACCEPT of the PR is not a GO:
`GO (D-825 form) KEEPER-PG-CUTOVER step 1+2 freeze+backup+copy+verify (sections 2,4,5) naming lane/<merged-sha> tree <live worktree sha>, backup dir pg-cutover-<sha>-<UTC>, PG target schema keeper on <PG-DEPLOY item verified>; abort = start old unit unchanged.`
`GO (D-825 form) KEEPER-PG-CUTOVER step 3 switch (section 6) naming head seq <N> hash <h> verified, drop-in pg-backend.conf; rollback = section 7.`
`GO ... step 4 retire sqlite after 48 h` (after a green second verify). Router decisions needed first: R1 (read-only option, section 3), loss-window policy (section 7), which timeslot (all writers paused ~15 min estimated: copy of 62.9k rows took seconds in rehearsal).

## 9. Hygiene
Hermetic tests (D-852) for any wrapper script that automates sections 2-6: stub `systemctl`/`psql` on PATH plus a guard test that fails when the real ones are reachable. This PR adds no apply/deploy tooling: only this doc, the log and the read-only digest script `keeper-pg-cutover.rowhash.py` (opens sqlite `mode=ro`, PG select only).
