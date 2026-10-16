# RT-P1-KEEPER-PG cutover — done note (build-L1 gen-10, 2026-10-16)
Judge server-L1-routing ACCEPT (record a6eb49f6 / head e5898f70, tree b3d71096). Seats: verdict-alibaba (qwen3.8-flash via qodercli) pass, TG no;
verdict-xiaomi (mimo-v2.6-flash via mimo provider) pass, 0 blockers — judge-countable, TG n/a for live-record review (no source diff; run1 TG no).
Judge minors, resolved for the record:
1. Grant tuple (20,20,0) in keeper-rt-privileges.txt = (tables in schema keeper, tables with ANY of S/I/U/D [has_table_privilege with a
   list is any-of], tables with TRUNCATE|TRIGGER|REFERENCES). Exact per privilege (evidence/keeper-rt-grants-per-privilege.txt, queried as
   keeper_rt): 20 tables, SELECT 20, INSERT 20, UPDATE 20, DELETE 20, TRUNCATE|TRIGGER|REFERENCES 0. Runtime DOES need UPDATE (item state,
   leases, epochs) and holds it; DELETE is granted by the default-privileges FOR ROLE keeper (prox D-1505). Live writes seq 6330..6339 work.
   No DDL (4 probes refused, schema CREATE false).
2. Backup keeper.db sha256 dc7fb396 != frozen live 6e6e9609 by design: backup made with sqlite .backup (page repack); proven equivalent by
   backup integrity_check ok + max seq 6329 (frozen-sqlite.txt). Backup-dir shm mtime 16:49 = that read-only dry check.
3. hba rule `host keeper keeper_rt 192.168.178.73/32 scram-sha-256` is non-TLS on the LAN: known; hostssl hardening is a listed follow-up.
Step 4 (delete-or-archive sqlite + backup) HELD until routing GO >= 2026-10-18 16:43Z.
