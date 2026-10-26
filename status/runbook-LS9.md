# LIVE-SYNC-9 runbook (OS-KEEPER-LS9-MIGRATE, D-2907) · build-L1 gen-37 · 2026-10-26 04:4xZ
Live now: 2d61e281 (drop-in live-sync-z7-2d61e281.conf), PG schema 4, backend postgres (zz-pg-backend.conf), MainPID 2025234, /v1/health ok, snapshot head_seq 11134.
Target: main a26573c35701025f9cae0239d78e1c61ed707bd3 (incl. #220 f50982d6 lost-PG-session exit 75 + singleton-lock watchdog). PG migrations 5..8 (merge_locks, jev_shadow, items exec_* columns, oversight tokens/ops) + presence-based cards.check_kind/check_reads. All additive (CREATE TABLE / ADD COLUMN IF NOT EXISTS), run in ONE transaction under an advisory lock (db_pg.migrate).
Staged (inert, nothing live touched): worktree /home/s/code/plangraph/.claude/worktrees/live-a26573c3 (detached at the target sha); venv-pg imports daemon OK, schema_pg.LATEST 8.
LS8 trap (D-2345) checked: strict request.item_key is on main (f54c18b0); 43/43 AutoOS run records of the last 24 h carry item_key. Plangraph spawner seats post via reviewer-<family> principal tokens, not run tokens.
No new required config keys (forgejo_url/forgejo_token_file are optional). No dependency change in the runtime venv (only CI playwright pins).

L = /home/s/code/plangraph/.claude/worktrees/live-a26573c3 ; P = ~/.local/share/plangraph ; B = $P/backups/pre-go-live-sync-a26573c3-20261026T043853Z

## 0. Pre-checks (L1, read-only)
- `systemctl --user is-active plangraph-keeper` = active; `curl -s 127.0.0.1:8787/v1/health` = {"status": "ok"}; df / >= 3 GB free (now 31 GB).
- No other live GO window open.

## 1. Backup
1a. DONE by L1 (read-only file copies): $B holds plan.snapshot.json (4863935 B), keeper.json, the unit + all 9 drop-ins, SHA256SUMS.
1b. PG dump. OPERATOR: `bash $J/ls9-1b.sh` (postgres:17-alpine; server is 17.11, D-2918). Dry run first: `bash $J/ls9-1b.sh --dry-run` (no secret value read).
    pg_dump -Fc -n keeper -f keeper-schema.dump <keeper db>   (on the PG host, as before)
    copy it to $B/keeper-schema.dump; `sha256sum keeper-schema.dump > keeper-schema.dump.sha256`
    size check: > 1403181 B (the LS7 dump) and `pg_restore -l $B/keeper-schema.dump | grep -c 'TABLE DATA'` >= 20.

## 2. Freeze writers (operator or L1 on GO)
    systemctl --user stop plangraph-md-sync.timer plangraph-md-sync.path plangraph-render.timer plangraph-stall-watchdog.timer
    systemctl --user stop plangraph-keeper
    systemctl --user is-active plangraph-keeper   # expect inactive; never `reset-failed`

## 3. Migrate (OPERATOR: `bash $J/ls9-3.sh`; tested dry run `--dry-run` first; reads pg-keeper-migrate.dsn, role keeper)
    bash -c 'set -a; . ~/.config/plangraph/pg-keeper-migrate.dsn; set +a; cd /tmp && PG_KEEPER_BACKEND=postgres PG_SCHEMA=keeper PYTHONPATH=/home/s/code/plangraph/.claude/worktrees/live-a26573c3/src ~/.local/share/plangraph/venv-pg/bin/python -m plangraph.keeper.cli --config ~/.local/share/plangraph/keeper.json admin migrate'
    expect "schema up to date"; on any error STOP and go to 6 (the transaction rolled back; schema still 4).
    (Check that the variable in the migrate DSN file has the name PG_DSN. Print variable names only, never values.)

## 4. Switch code + start (L1 on GO)
    printf '%s\n' '# LIVE-SYNC-9 GO <D-id>: keeper tree -> a26573c3 (PG migrations 5-8, #220). Overrides live-sync-z7-2d61e281.conf.' '# Rollback: remove this file, daemon-reload, restart (= 2d61e281; migrations are additive, old code accepts schema >= 4).' '[Service]' 'WorkingDirectory=%h/code/plangraph/.claude/worktrees/live-a26573c3' 'Environment=PYTHONPATH=%h/code/plangraph/.claude/worktrees/live-a26573c3/src' > ~/.config/systemd/user/plangraph-keeper.service.d/live-sync-z8-a26573c3.conf
    systemctl --user daemon-reload && systemctl --user start plangraph-keeper

## 5. Verify (L1; all must hold, else 6)
- PID=$(systemctl --user show -p MainPID --value plangraph-keeper); `readlink /proc/$PID/cwd` = L; PYTHONPATH by NAME + value check only: `tr '\0' '\n' < /proc/$PID/environ | grep '^PYTHONPATH='` = $L/src (prints no secret).
- `curl -s 127.0.0.1:8787/v1/health` = {"status": "ok"} within 30 s; `journalctl --user -u plangraph-keeper -n 50` has no Traceback / "schema is at migration".
- Snapshot: plan.snapshot.json head_seq >= 11134 (re-rendered by the keeper/render) and `ops/pg.sh show plangraph:ORCH-EFFICIENCY-ENFORCE` returns state claimed (read verb).
- Write smoke: `ops/pg.sh heartbeat plangraph:ORCH-EFFICIENCY-ENFORCE` ok (claim lease write path); `ops/pg.sh ready | head` ok.
- Restart the frozen units: `systemctl --user start plangraph-md-sync.path plangraph-md-sync.timer plangraph-render.timer plangraph-stall-watchdog.timer`.
- Watch 15 min: one seat_verdict post (reviewer token path) and one evidence commit+ci from the next merge succeed.

## 6. Rollback
A (code only, normal): `rm ~/.config/systemd/user/plangraph-keeper.service.d/live-sync-z8-a26573c3.conf && systemctl --user daemon-reload && systemctl --user restart plangraph-keeper`. That runs 2d61e281 on schema 8; the old code refuses only a schema BEHIND 4. Verify as in 5 (cwd = live-2d61e281).
   Caveat: if the new code has written events the old fold cannot replay (old journal shows an unknown event kind on start), do not loop. Roll forward instead (re-add z8, fix-forward), like LS6.
B (data, last resort, OPERATOR): stop the keeper; postgres:17-alpine `pg_restore --clean --if-exists -n keeper -d <keeper db> $B/keeper-schema.dump` with the migrate role; then A.
Window target: freeze-to-health <= 5 min.
