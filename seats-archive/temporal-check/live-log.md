# TEMPORAL-CHECK live log (GO D-1513, executor prox-L2-temporal-check)
- T1 PASS: home avail 12.6 GB; cloud MemAvailable 19531364 kB (BASE_MEMAVAIL_MB 19073); swap 52 MB free of 2 GB (note); PG healthy; DBs temporal + temporal_visibility; pg-net present.
- T2 PASS: dryrun Semaphore task 164707 @314c06d4, exactly 5 containers + temporal-net, postgres untouched.
- T3 BLOCKED: cloud $APPS_ROOT/temporal/tls missing (T0 operator, OS-208). prox L2 stopped; relaunch when certs exist.
- 2026-10-17 11:3xZ: OS-208 confirmed done (prox-L1-main read-only): cloud tls ca.crt/server.crt/server.key Oct 16 18:50, CN=cloud.vm SAN cloud.vm+192.168.178.159, to 2029-01-18. Routing asked to close OS-208 + relaunch prox-L2 for T3-T6+T9. WSL client dir unverified (needed for T9).
- 2026-10-17: prox-L2-temporal-check [4d1de438] relaunched T3-T6+T9 (D-1719). WSL client dir PRESENT (workst-L1-autoos: ca.crt 631, client.crt 648, client.key 241, all 600) -> T9 prerequisite met. Plangraph workst runners removed (D-1718).
- 2026-10-17 11:28Z T3 FAIL (prox-L2 [4d1de438], Semaphore task 166875): temporal crash-loop "no usable database connection found"; schema/namespace one-shots never ran; rolled back via down (task 166877); DBs untouched; no keeper verify needed. BASE_MEMAVAIL_MB=16142. Details -> server-28.
  detail (prox-L1): "sql schema version compatibility check failed ... no usable database connection found"; temporal-schema + temporal-namespace stayed Created; auto-rollback incomplete ("previous spec re-apply rc=1"); down 166877 clean. Read-only diagnosis lane on home ordered (build-L1).
- T3 diag (temporal-check-t3-diag): root cause = admin-tools 1.32 image has no /bin/bash (OCI exec error, task 166875), NOT the DB (role/DBs/pg-net fine). Crash-loop came from the failed rollback's `up --no-deps` starting temporal without the schema one-shot. Fix lane/temporal-dbfix @531fae72 (/bin/sh entrypoints + setup.sh; stack-deploy rolls back only to a spec that ran). Re-run needs a NEW router GO (D-1724; D-1513 used up); server-28 files the GO-REQ for T2+T3.

## Re-run GO D-1744 (ref 89e8ebd8, fix 310c98e2), executor prox-L2-temporal-check [7a539b08], launched per prox-L1-main
