# R2a' fix R4b: duplicate copy table (lane/phase-fix-r2a head 26ea12d5)
You are the WRITER. cwd = git worktree on branch lane/phase-fix-r2a. Edit files in place, run the checks, ONE commit. Do NOT push.
Edit only: src/plangraph/keeper/copy_to_pg.py `TABLES` (the tuple) and tests/test_keeper_oversight_scope.py (one new test). Nothing else. Write your first change within 10 minutes; do not survey the repo; never print whole files.

## Finding (seat text verbatim, r4b qwen seat)
"LOW copy_to_pg.py::TABLES — `"jev_shadow"` listed twice; double-copied. Harmless only if the loader is truncate-per-table; dedupe."
L1 evidence: it is not truncate-per-table. `run` (copy_to_pg.py ~line 183) does `for table in tables: _copy_table(src, conn, table)` inside one transaction, then compares per-table counts with the source; `jev_shadow.id` is INTEGER PRIMARY KEY (schema.py). A second copy of a non-empty jev_shadow re-inserts the same ids, so copy-to-pg of any keeper with JEV rows fails. The duplicate came from the main merge 89da4c78: main lists "jev_shadow" before "oversight_tokens" (line ~26), the lane before "messages" (line ~30).

## Fix
- Remove the SECOND "jev_shadow" (the one directly before "messages"); keep the one before "oversight_tokens" (main's order). No other change to TABLES.
- Red test first, in tests/test_keeper_oversight_scope.py right after the test that asserts `order.index("oversight_tokens") < order.index("oversight_ops")` (~line 1219): `test_copy_tables_are_unique`: `assert len(copy_to_pg.TABLES) == len(set(copy_to_pg.TABLES))` with the duplicates named in the assert message. It must FAIL before the TABLES edit and pass after.

## Checks (foreground; then one commit)
- python3 -m pytest -q -p no:cacheprovider -n 4 tests/test_keeper_oversight_scope.py tests/test_keeper_pg_copy.py -> 0 failed
- python3 -m pytest -q -p no:cacheprovider -n 4 (whole suite) -> 0 failed
Commit trailers exactly: `Plan-Item: plangraph:RT-P1-BUS`, `Writer: mimo/mimo-v2.6-pro <env PLANGRAPH_RUN_ID or plangraph-spawner>`. No other trailers.
Final report (< 1500 tokens): FIXED <test name>, red-before/green-after evidence, the two pytest tail lines. Then stop.
