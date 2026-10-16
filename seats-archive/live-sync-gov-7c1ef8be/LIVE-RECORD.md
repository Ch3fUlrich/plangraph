# LIVE-SYNC-5 executed (GO D-1539) 2026-10-16 19:55Z, build-L1
Old MainPID 632371 (b3d71096) -> new MainPID 1288789 (3fd91bd6), NRestarts 0, PYTHONPATH live-3fd91bd6/src, backend postgres, schema keeper.
Backup /home/s/.local/share/plangraph/backups/live-sync-3fd91bd6-20261016T195514Z: owner dump keeper-schema.dump sha256 bbfa12b10427ffc9094301c26826ecfecd4bc7e4eb7a26c382089f721d48da56; keeper-pg.json + service.d copied.
migrate rc0 "schema up to date"; verify ok; GRANT rc0 (keeper_rt DELETE/INSERT/SELECT/UPDATE on metrics).
Gates: admin verify ok, verify-snapshot ok, /v1/snapshot 200, set_metrics router {"count":1}, GET metrics row ok, l2-coding token 403, /v1/alarms 200, whoami/ready/percent ok, md-sync timer+path active, render timer active.
Counts pre/post: events 6595/6595, items 1802/1802, evidence 1104/1104, edges 2645/2645, audit_log 53419/53424 (+5 audit rows), max seq 6595/6595, metrics 0->1 (probe row).
Drop-in ~/.config/systemd/user/plangraph-keeper.service.d/live-sync-z5-3fd91bd6.conf. Rollback = rm it, daemon-reload, restart (= b3d71096).
