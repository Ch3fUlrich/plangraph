# L2 brief: build-L2-inbox — plangraph:RT-P1-INBOX-CODE (standard; coding.vm; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding). TTL 4 h.
Done means (keeper item): keeper HTTP endpoints for questions/answers (default + deadline value stored; risk-tagged questions —
auth, secrets, money, live step, public release — have NO default); minimal answer form page; answer emits a Temporal signal
(tested against a local `temporal server start-dev`); timer that fires a deadline lives only in Temporal (design §2, §4.8);
tests on SQLite + Postgres CI (main 1779d2d); 1 cross-family seat PASS. Contract: endpoints added to the frozen contract suite
(RT-P1-CONTRACT on main) as new cases, existing cases unchanged.
Coordinate: keeper-pg W3 in flight — write set = src/plangraph/inbox/* (new), new routes registered in the daemon via one small hook,
tests, one new migration if needed; other edits to store*.py/schema*.py/daemon.py -> ask build-L1 first.
Worktree /home/s/code/plangraph-worktrees/inbox-code, branch lane/rt-p1-inbox-code from origin/main; draft PR; MERGE-READY <sha> to build-L1.
