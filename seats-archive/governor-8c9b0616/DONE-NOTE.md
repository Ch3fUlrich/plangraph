# RT-P1-GOVERNOR done (build-L1 gen-10, 2026-10-16)
PR #37 head 7caa2d7f merged main 3fd91bd6; CI 38956827289 green. Judge server-L1-routing ACCEPT (merge-tree f42e7add, 251 pass).
Seats on 8c9b0616 (7caa2d7f test-only after): qwen3.8-flash/qodercli pass (keeper verdict alibaba), gpt-oss-120b/ovh pass (409 locked: item already done).
LIVE-SYNC GO CONDITIONS (judge + routing): live keeper runs as keeper_rt (no DDL). Packet must (1) run `pg admin migrate` (migration 4) as owner role keeper,
(2) GRANT SELECT, INSERT, UPDATE ON keeper.metrics TO keeper_rt (DML only, D-1500; ON CONFLICT upsert needs UPDATE), (3) rehearse on a restored copy,
prove set_metrics as keeper_rt, then GO request to routing with rehearsal evidence. Else live keeper refuses start ("schema at migration N, code needs 4").
Follow-ups: AutoOS spawner wiring (python -m plangraph.governor check / planner.start.guard_start); RT-P1-WORKER leg check.
