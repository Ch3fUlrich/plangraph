# TEMPORAL-CHECK live log (GO D-1513, executor prox-L2-temporal-check)
- T1 PASS: home avail 12.6 GB; cloud MemAvailable 19531364 kB (BASE_MEMAVAIL_MB 19073); swap 52 MB free of 2 GB (note); PG healthy; DBs temporal + temporal_visibility; pg-net present.
- T2 PASS: dryrun Semaphore task 164707 @314c06d4, exactly 5 containers + temporal-net, postgres untouched.
- T3 BLOCKED: cloud $APPS_ROOT/temporal/tls missing (T0 operator, OS-208). prox L2 stopped; relaunch when certs exist.
- 2026-10-17 11:3xZ: OS-208 confirmed done (prox-L1-main read-only): cloud tls ca.crt/server.crt/server.key Oct 16 18:50, CN=cloud.vm SAN cloud.vm+192.168.178.159, to 2029-01-18. Routing asked to close OS-208 + relaunch prox-L2 for T3-T6+T9. WSL client dir unverified (needed for T9).
