# L2 brief: build-L2-planner-kit — plangraph:RT-P1-PLANNER-KIT-CODE (standard; coding.vm; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding). TTL 4 h.
Done means (keeper item): card schema (design §3 cards fields: outcome, read/write sets, exports, forbid paths, check, smoke checks,
held-out check, window class 128k/256k/1m, risk tags, estimate tokens) + import from batch YAML; readiness gate (design §4.2: check
present + dry-runnable, read files exist, write sets disjoint from running cards, estimate <= 50-60 % of class window and <= leg max
request, risk tags present; fail -> planner queue); keeper-side routing (design §4.3, §5: risk/estimate/subtasks -> harness, window
class, attempt budget, review level; leg order D25 qwen-3.8-flash via qodercli first -> free -> mimo-v2.6-flash -> agy -> deepseek last);
context pack builder; tests on SQLite + Postgres CI (main 1779d2d); 1 cross-family seat PASS. Jev hook + planning skill rewrite are NOT in scope.
Coordinate: keeper-pg W3 in flight — write set = src/plangraph/planner/* (new) + tests + one new migration if needed; edits to
store*.py/schema*.py -> ask build-L1 first.
Worktree /home/s/code/plangraph-worktrees/planner-kit-code, branch lane/rt-p1-planner-kit-code from origin/main; draft PR; MERGE-READY <sha> to build-L1.
