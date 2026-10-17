# KEEPER-LIVE-SYNC-7 live record (GO D-1667, 2026-10-17 06:58-06:59Z, build-L1 gen-12)
Packet main ce3c2dee docs/plan/rethink/live-sync-2d61e281.md §2 steps 1-8. Live 102e4885 (MainPID 1776044) -> 2d61e281 (MainPID 2817573, NRestarts 0, active).
1 worktree ~/code/plangraph/.claude/worktrees/live-2d61e281 @2d61e281. 2 pre: active NRestarts 0, df / 7.0G free (96%); counts.pre 7241/1826/1193/2671 (events/items/evidence/edges); reopen-check 0; max seq 7241.
3 backup ~/.local/share/plangraph/backups/live-sync-2d61e281-20261017T065838Z/keeper-schema.dump sha256 42579d259a1d0e1d918e7e7b7a740d1e16ccefe794083eb46e8eef48cddffc3e (+keeper.json, keeper+render service.d). Pre-GO dump 77f1a56d.
4 migrate (owner, T) "schema up to date"; verify "chain and cache integrity ok".
Step A: 5 drop-in plangraph-keeper.service.d/live-sync-z7-2d61e281.conf (after z6, before zz-pg-backend). 6 restart: env PYTHONPATH=.../live-2d61e281/src, backend postgres, schema keeper.
7 verify "chain and cache integrity ok"; verify-snapshot "snapshot signature ok"; snapshot/whoami/ready/alarms/show(PG-GUARD-SEP-SPLIT) 200; counts.post equal (7241/1826/1193/2671, max seq 7241); render.timer + md-sync.timer active. No live POST.
Step B: 8 drop-in plangraph-render.service.d/live-sync-z7-2d61e281.conf (after kg.conf); start: Result success, ExecMainStatus 0, Environment PYTHONPATH=.../live-2d61e281/src; out/plan-cdn.html 3614449 B 06:59:10Z; plan-page :8089 200, title "plangraph (1826 items)", </html> present. proposal tree untouched (f3bac63f, clean).
Rollback (always valid this batch): rm both z7 drop-ins, daemon-reload, start render + restart keeper (-> 102e4885 / proposal). Never below 102e4885 once item_reopened > 0.
