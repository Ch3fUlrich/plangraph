# build-L2-governor card (RT-P1-GOVERNOR)
head 7caa2d7f  lane/rt-p1-governor  PR #37 (draft)  trailer Plan-Item ok
CI run 38956827289: pytest pass, pytest-postgres pass (head 7caa2d7f)
full local suite on 8c9b0616: 3428 passed, 15 skipped; src/docs unchanged after (7caa2d7f = test-only)
Seats (final src 8c9b0616): qwen3.8-flash qodercli PASS, ovh/gpt-oss-120b PASS; dirs /home/s/code/plangraph-worktrees/governor-seats-8c9b0616
 earlier rounds: qwen V2 false positive (dispatch has no role gate; moved set_metrics to ROLE_CHECKED_VERBS anyway), qwen V5 real bug daily_metrics all-days sum -> fixed 8c9b0616
Shape: keeper metrics table v11 + PG mig 4; verbs set_metrics/get_metrics/log_governor_override; GET /v1/metrics;
 governor/{metrics,policy,keeper_source,cli}.py; planner/start.py guard_start; docs/governor.md
D-1533: Claude burn>1/7 or rate limited -> agent planner/expert/judge falls back to non-Claude leg (logged), tested
Follow-ups: AutoOS spawner wiring (call `python -m plangraph.governor check` / guard_start); RT-P1-WORKER leg check (metrics provider_used.<p>, rate_limited.<p>); systemd timer unit documented, not installed
Not touched: live keeper, AutoOS, main
