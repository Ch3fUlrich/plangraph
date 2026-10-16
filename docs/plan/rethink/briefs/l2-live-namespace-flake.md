# L2 brief: build-L2-ns-flake — plangraph:PROJECTOR-LIVE-NAMESPACE-FLAKE (D-1433, prio 2, standard; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding, incl. D-1380, PR-head trailer). TTL 2 h. Never merge, no crons (D-1377 binds L2s).
Symptom: tests/test_projector_live.py fails on CI attempt 1 with "Namespace default is not found" vs the live Temporal dev server,
3x on 2026-10-16: test_ok_attempt (run 38849305837, sha 4e11857), test_heartbeat_timeout_is_lease_lost_and_item_failed (38855239133,
1621385), test_ok_attempt + test_check_fail_then_ok (38856993443, 5f56487). Green on rerun; lanes never touched the file. CI now runs
pytest -n 8 (D-1393) on the fleet-ci pool. Hypothesis (unverified): dev-server startup race — frontend answers before namespace
"default" is registered, or several xdist workers each start/share a dev server.
Done means (keeper item): root cause named with evidence (CI logs filtered, a local repro under -n 8, or the fixture's own code);
fix in the fixture/helper (e.g. wait until DescribeNamespace(default) succeeds, bounded, before yielding; one server per worker/port)
so 20 consecutive runs of tests/test_projector_live.py under pytest -n 8 pass with 0 occurrences (log the loop). No skip, no retry
decorator, no rerun plugin hiding it.
Write set: the live-Temporal fixture/helper (tests/conftest.py or the helper test_projector_live.py uses) + test_projector_live.py.
Nothing in src/ unless the root cause is in src (then say so before changing it).
Seat: 1 cross-family non-Claude seat PASS (gpt-oss or qwen via workst-L1-autoos), seat_verdict dry-run rc 0, orphan branch seats/ns-flake-<sha>.
Writers: free first (mimo is 402 today -> muse-spark free), Sonnet last. Worktree from origin/main, branch lane/live-namespace-flake;
draft PR; head trailer `Plan-Item: plangraph:PROJECTOR-LIVE-NAMESPACE-FLAKE`; MERGE-READY <sha> + CI run + 20-run loop log + seat branch to build-L1.
