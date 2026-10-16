# KEEPER-LIVE-SYNC-6 live record (GO D-1569, 2026-10-16 ~22:52Z, build-L1)
Packet main 418e6d7e docs/plan/rethink/live-sync-102e4885.md §2 steps 1-7. Live 3fd91bd6 (MainPID 1288789) -> 102e4885 (MainPID 1776044, NRestarts 0, active).
1 worktree ~/code/plangraph/.claude/worktrees/live-102e4885 @102e4885. 2 pre: active NRestarts 0, psycopg ok, df / 4.7G free (97%); counts.pre 7008/1809/1137/2650 (events/items/evidence/edges); reopen-check 0; max seq 7008.
3 backup ~/.local/share/plangraph/backups/live-sync-102e4885-20261016T225138Z/keeper-schema.dump sha256 e9b2ee552786a91dad97c5863752cde68941febecd5dced5a7031691e8a23543 (+keeper.json, service.d). Pre-GO dump 751c5b5e (pre-go-live-sync-102e4885-20261016T225104Z).
4 migrate (owner, T) "schema up to date"; verify "chain and cache integrity ok".
5 drop-in live-sync-z6-102e4885.conf (sorts after z5, before zz-pg-backend). 6 restart: env PYTHONPATH=.../live-102e4885/src, backend postgres, schema keeper.
7 verify rc0 "chain and cache integrity ok"; verify-snapshot rc0 "snapshot signature ok"; snapshot/whoami(router)/ready/show/alarms 200; POST /v1/reopen as l1 = 403; counts.post equal; reopen-check 0; max seq after 7008; render.timer, md-sync.timer, md-sync.path active; whoami/ready/percent CLI rc0.
Rollback (only while reopen-check = 0): rm drop-in z6, daemon-reload, restart (-> 3fd91bd6). After first item_reopened: ROLL FORWARD ONLY.
Note: first verify attempt rc2 = my shell quoting (argparse "invalid choice 'admin verify'"), not a keeper fault; rerun rc0.
