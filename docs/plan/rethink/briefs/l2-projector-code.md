# L2 brief: build-L2-projector — plangraph:RT-P1-PROJECTOR-CODE (HIGH; coding.vm; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding). TTL 4 h.
Done means (keeper item): projector reads Temporal workflow history and writes attempts + keeper events idempotently, keyed
(workflow_id, run_id, activity_id, attempt), states only move forward, watermark stored; full rebuild from history gives identical
rows; nightly reconciler compares open workflows vs keeper states and flags drift (Inbox alarm row); tests against a local
`temporal server start-dev` + the Postgres CI service (pytest-postgres job, main 1779d2d) and SQLite; 2 seats incl. non-Claude PASS on final head.
Spec: design /home/s/code/routing-worktrees/rethink-setup/docs/reports/2026-10-15-rethink-setup-design.md §2 "One owner per fact",
§3 attempts + exit classes, §4.5. Keeper Postgres backend + design-3 tables = PR #8 (src/plangraph/keeper/schema_pg.py, store*.py).
Language: Python (keeper side, `temporalio` SDK) unless you find a blocker; note why. Spike findings: rust/ + docs on main (TEMPORAL-SPIKE).
Coordinate: build-L2-keeper-pg W3 is in flight on keeper files — your write set = new module(s) src/plangraph/projector/* + tests;
schema additions only via a new migration file; any edit to store*.py/schema*.py -> ask build-L1 first (D-891 style, one line).
Worktree /home/s/code/plangraph-worktrees/projector-code, branch lane/rt-p1-projector-code from origin/main; draft PR; MERGE-READY <sha> to build-L1.
