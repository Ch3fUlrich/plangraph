# KEEPER-LIVE-SYNC-7: live keeper 102e4885 -> origin/main T=2d61e281 (guard #55/#56 + UI batch #50-#54), D-1569

Plan-Item: plangraph:PG-GUARD-SEP-SPLIT. Docs only; nothing here was run against the live keeper or the live schema (rehearsal used a throwaway container; the only live contact was one read-only `pg_dump -n keeper` as the runtime role).
Batch (git log 102e4885..T): #45 packet 102e4885 (docs), #50 PG-UI-TABICON (tab icon/title `plangraph`), #51 L2 small-fix rule (docs/skill text), #52 UI-TABLE (facet filters, column chooser, URL filters), #53 PG-NEEDS-YOU (one Needs-you table), #54 UI-TABLE fix (sticky header, aria-sort, keyboard), #55 PG-GUARD-PATH-EXEMPT (D-1613 narrow path exemption), #56 PG-GUARD-SEP-SPLIT (path token split at `,;`, only `[\w./-]`).
Live now: 102e4885 (LIVE-SYNC-6; record `seats-archive/live-sync-6-102e4885/LIVE-RECORD.md` in build-L1), keeper drop-in `live-sync-z6-102e4885.conf` + `zz-pg-backend.conf` in `~/.config/systemd/user/plangraph-keeper.service.d/`, DSN files `~/.config/plangraph/pg-keeper.dsn` (runtime) and `pg-keeper-migrate.dsn` (owner), each `PG_DSN=`; names only, never print contents.

## 0. Finding that shapes the packet: two separate code trees
- **Keeper daemon** (`plangraph-keeper.service`) runs from `live-102e4885` (drop-in z6). It serves no page.
- **Page render** (`plangraph-render.service`, timer every 5 min, writes `out/plan-cdn.html` for nginx `plan-page`) and `plangraph-md-sync.service` run from the OLD `.claude/worktrees/proposal/src` (HEAD f3bac63f, own PYTHONPATH). A keeper restart does NOT change the page. The UI batch (#50-#54) reaches the page only through step B (render drop-in). The md-sync unit is untouched by this packet. `proposal` is never edited (read as a path only).

## 1. Review 102e4885..T (verified, not assumed)
**Claim: code-deploy only; keeper change = credential guard only. Verified.**
- `git diff --stat 102e4885 T -- src/plangraph/keeper docs/contract` lists only `credential_guard.py`. `schema_pg.py`, `db_pg.py`, `config.py`, `mcp.py`, `schema.py`, `cli.py`, `api.py`, migrations: empty diff. `schema_pg.LATEST` stays 4, no DDL, no new config key/env var, no role or grant change (every rehearsal write ran as `keeper_rt`: zero permission errors).
- MCP tool list and HTTP routes unchanged. `schema.EVENT_TYPES` identical in 102e4885 and T (md5 equal); rehearsal copy has 0 event types unknown to 102e4885. T wrote only `item_created`, `item_defined`.
- Behaviour change (the only one): guard. 102e4885 refuses every `word/secretish` shape near a credential word, no exemption (D-388). T (D-1613, PL-33) exempts a token that is path-like (>= 2 `/`, only `[\w./-]`, last segment with a 1-5 char extension), tokens split at whitespace, `,` and `;`. Bypasses refused in T: `login admin/hunter2secret` joined to `docs/x/y.md` with `,` `;` `|` `&`. Accepted trade-off (module docstring): `admin/swordfish/docs.md`-style paths are exempt. Judge ACCEPT on #56 (build-L1).
- Render side (step B only): `render.py`, `page_*.js/css/html` (Needs-you table, table controls, tab icon/title). `pg render` masks with the same guard set; no data-format change in the snapshot.
- **Rollback is always valid for this batch.** No new event type, no schema, no state T writes that 102e4885 cannot fold: T writes the same events through the same fold. Proven in rehearsal (section 3) on the T-written copy. Roll-forward rule: **no ROLL-FORWARD-ONLY condition exists in this batch**; rollback = drop-in removal (keeper) and drop-in removal (render). The caveat of LIVE-SYNC-6 (`item_reopened`) already holds on 102e4885 and is unchanged: do not roll back below 102e4885 once any `item_reopened` exists (check query in step 2); this packet never rolls below 102e4885.
- Guard side effect to know: items whose text is only path-with-extension near "login"/"password" are now creatable (previously refused). Stored items are unchanged; render masks the same set it refuses.

## 2. Live runbook (build-L1, after routing GO D-825 on the merged packet; exact commands)
Vars: `W=~/code/plangraph/.claude/worktrees`, `D=~/.local/share/plangraph`, `TS=$(date -u +%Y%m%dT%H%M%SZ)`, `B=$D/backups/live-sync-2d61e281-$TS`, `T=$W/live-2d61e281`, `OLD=$W/live-102e4885`, `PGENV='PG_KEEPER_BACKEND=postgres PG_SCHEMA=keeper'`.

1. **Worktree for T**: `git -C ~/code/plangraph fetch origin && git -C ~/code/plangraph worktree add --detach $T 2d61e2813ff077ec42bac00e7aede534bc181110`; check `git -C $T log -1 --format=%h` = `2d61e281`.
2. **Pre-checks** (read-only): `systemctl --user show plangraph-keeper -p MainPID -p NRestarts -p ActiveState`; `df -h $D` (>= 1 GB). `mkdir -p $B && chmod 700 $B`. Counts `$B/counts.pre` and reopen count, both with `docker run --rm --network host --env-file ~/.config/plangraph/pg-keeper.dsn postgres:17-alpine sh -c 'psql "$PG_DSN" -Atc "select count(*) from keeper.events union all select count(*) from keeper.items union all select count(*) from keeper.evidence union all select count(*) from keeper.edges"' > $B/counts.pre`; reopen: same with `select count(*) from keeper.events where type=\$\$item_reopened\$\$`.
3. **Backup** (no stop, no schema change): `docker run --rm --network host --env-file ~/.config/plangraph/pg-keeper-migrate.dsn -v $B:/b postgres:17-alpine sh -c 'pg_dump -d "$PG_DSN" -n keeper -Fc -f /b/keeper-schema.dump'`; `sha256sum $B/keeper-schema.dump | tee $B/keeper-schema.dump.sha256`; `cp $D/keeper.json $B/`; `cp -a ~/.config/systemd/user/plangraph-keeper.service.d ~/.config/systemd/user/plangraph-render.service.d $B/`. (Rehearsal dump: sha256 `ec7be9fc09d23743672c7b07d9f1a819e28611361817b280aa5db409994eb014`, 7241 events, 1826 items, migration 4, 0 `item_reopened`, taken read-only as runtime role 2026-10-17; the GO dump is fresh with its own sha256.)
4. **Migration check (expected no-op, owner)**: `set -a; . ~/.config/plangraph/pg-keeper-migrate.dsn; set +a; env PYTHONPATH=$T/src $PGENV $D/venv-pg/bin/python -m plangraph.keeper.cli --config $D/keeper.json admin migrate` = `schema up to date`; then `... admin verify` = `chain and cache integrity ok`; `unset PG_DSN`. Anything else: STOP, the claim is false.

### Step A: keeper (guard)
5. Drop-in `~/.config/systemd/user/plangraph-keeper.service.d/live-sync-z7-2d61e281.conf` (sorts after z6, before zz):
   ```
   # LIVE-SYNC-7 GO <decision id>: keeper tree -> 2d61e281 (credential guard #55/#56). Overrides live-sync-z6-102e4885.conf.
   # Rollback: remove this file (back to 102e4885). No one-way writes in this batch.
   [Service]
   WorkingDirectory=%h/code/plangraph/.claude/worktrees/live-2d61e281
   Environment=PYTHONPATH=%h/code/plangraph/.claude/worktrees/live-2d61e281/src
   ```
   `systemctl --user daemon-reload`.
6. `systemctl --user restart plangraph-keeper`; show MainPID/NRestarts/ActiveState (active, NRestarts 0). **Env check**: `tr '\0' '\n' </proc/$(systemctl --user show plangraph-keeper -p MainPID --value)/environ | grep -E '^(PYTHONPATH|PG_KEEPER_BACKEND|PG_SCHEMA)='` shows `live-2d61e281/src`, backend postgres, schema keeper. Never print `PG_DSN`.
7. **Verify gates** (runtime DSN sourced in a subshell; `env PYTHONPATH=$T/src $PGENV $D/venv-pg/bin/python -m plangraph.keeper.cli --config $D/keeper.json ...`): `admin verify` = `chain and cache integrity ok`; `admin verify-snapshot` = `snapshot signature ok`; `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $(cat ~/.config/plangraph/router.token)" http://127.0.0.1:8787/v1/snapshot` = 200; `GET /v1/whoami` 200 (router), `/v1/ready` 200, `/v1/alarms` 200, `/v1/show?key=plangraph:PG-GUARD-SEP-SPLIT` 200. Counts rerun to `$B/counts.post`: restart-only, each >= pre. md-sync and render timers still active. **No POST in the live smoke** (guard behaviour is proven in rehearsal; a live refusal probe would write an audit row, not an item: optional, only a non-credential-shaped sentinel is acceptable).

### Step B: page render (UI #50-#54), only after Step A gates are green
8. Drop-in `~/.config/systemd/user/plangraph-render.service.d/live-sync-z7-2d61e281.conf` (after `kg.conf`):
   ```
   # LIVE-SYNC-7 GO <decision id>: page render tree -> 2d61e281 (UI #50-#54). Rollback: remove this file.
   [Service]
   Environment=PYTHONPATH=%h/code/plangraph/.claude/worktrees/live-2d61e281/src
   ```
   `systemctl --user daemon-reload && systemctl --user start plangraph-render.service` (the unit builds to a temp file and copies into `out/plan-cdn.html` only if non-empty and containing `</html>`; a failed render leaves the served page untouched). Verify: `systemctl --user show plangraph-render -p Result -p ExecMainStatus` (success, 0); `curl -s http://127.0.0.1:<plan-page port>/ | grep -o '<title>[^<]*</title>'` = `plangraph (N items)`; page served 200. `proposal` tree is not modified.

### Rollback (any gate fails)
9. Step B: `rm ~/.config/systemd/user/plangraph-render.service.d/live-sync-z7-2d61e281.conf && systemctl --user daemon-reload && systemctl --user start plangraph-render.service` (page back from the `proposal` tree). Step A: `rm ~/.config/systemd/user/plangraph-keeper.service.d/live-sync-z7-2d61e281.conf && systemctl --user daemon-reload && systemctl --user restart plangraph-keeper`; repeat env check (PYTHONPATH `live-102e4885`) and `admin verify` with `PYTHONPATH=$OLD/src`. Roll back B with A: T-render against the 102e4885 keeper was not rehearsed. No dump restore (schema untouched; dump kept as insurance). Writes made by T meanwhile are ordinary events 102e4885 folds (rehearsal).

## 3. Rehearsal summary (log: `live-sync-2d61e281.rehearsal-log.txt`)
Throwaway `postgres:17-alpine` (`--memory 1g`, loopback random port, trust auth, one container, removed at end, MemAvailable ~9.8 GB). Roles `keeper` (owner, non-superuser) and `keeper_rt` recreated; restored the fresh read-only live dump (7241 events, 1826 items, migration 4, 0 `item_reopened`).
- T code, owner DSN: `admin verify` ok, `admin migrate` = `schema up to date`.
- T daemon as `keeper_rt` on scratch config/port/token/signing key: health/whoami/snapshot 200; PYTHONPATH of the daemon = T tree. Create with `under` and a comma-separated legit path list near "login": 200. The four bypass shapes (`,` `;` `|` `&`) on `note`: 400 `validation`, message names key + field, masked `ad*** (len 19)`, no value echo. `ops/runbooks/login-flow.md` near "login": 200. T `admin verify` + `verify-snapshot` ok after.
- Rendered page: `keeper.cli render --cdn` with T code against the scratch daemon: rc 0, 3.4 MB, `</html>` present, title `plangraph (1828 items)`, Needs-you and table-control markers present; served over a scratch `http.server`: 200. (The one literal `hunter2secret` in the page is existing plan text naming the guard test vector, outside credential-word range of a shape; not a leak.)
- Rollback proof (same copy, T-written; 2 `item_created` + 2 `item_defined` after the dump, `item_reopened` = 0, no event type unknown to 102e4885): 102e4885 `admin verify` ok, `verify-snapshot` ok, daemon health/whoami/snapshot 200, an old-code write 200, `admin verify` ok under both 102e4885 and T afterwards. 102e4885 refuses the bypass shape too (no exemption there), so rollback loses no guard coverage on that shape; it only drops the path exemption.

## 4. Seats / review
Packet seats (no-migration claim, rollback validity, secrets hygiene): see PR body. #55/#56 guard code already passed 2 non-Claude seats + judge (build-L1).
