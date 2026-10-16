# L2 brief: build-L2-ci-conclusions — plangraph:KEEPER-CI-CONCLUSIONS (D-1403, prio 1, HIGH; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding, incl. D-1380, PR-head trailer). TTL 2 h.
Why: autoos:CI-WORKST-RUNNER commit+ci for a1b1da808a057dc5e149163bff62550b33560fec verifies false: "Windows suite" check-run is
completed/skipped by design (runs 38850826053 + 38852383419) and shard b has a second, in_progress run on the same sha.
Done means (keeper item): src/plangraph/keeper/evidence.py CommitCiVerifier (~L188-208) + CIAdapter.check_runs as needed:
- per check-run NAME only the newest run counts (by check-run id, tie-break started_at);
- newest conclusion success/skipped/neutral = ok; failure/cancelled/timed_out/action_required/stale = fail (name it);
- newest not completed = fail "CI pending" (retryable wording); all counted runs skipped = fail (fail closed); no runs = fail as today;
- tests for each case, incl. the a1b1da808 shape (Windows suite skipped x2, shard b success then newer in_progress -> pending;
  then newer success -> ok). Hermetic tests (D-852): fake CIAdapter reproducing GitHub check-runs JSON (read the real adapter first, R-worker-04).
Write set: src/plangraph/keeper/evidence.py, its tests. Nothing else; no keeper.json, no live restart (build-L1 does D-915 restart after merge).
HIGH: 2 non-Claude seats of distinct families (gpt-oss openai + qwen via qodercli -o json), seat_verdict.py dry-run exit 0 for both;
seats pushed via workst-L1-autoos as orphan branch seats/ci-conclusions-<sha>. Judge: server-L1-routing (build-L1 relays).
Writers: free first (mimo-v2.6-flash), Sonnet last. Worktree from origin/main, branch lane/keeper-ci-conclusions; draft PR;
head trailer `Plan-Item: plangraph:KEEPER-CI-CONCLUSIONS`; affected tests + CI on workst; MERGE-READY <sha> + CI run + seat branch to build-L1.
