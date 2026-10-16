# L2 brief: build-L2-governor — plangraph:RT-P1-GOVERNOR (HIGH: keeper schema; parent build-L1; TTL 6 h)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding, incl. D-1380, PR-head trailer).
Item claimed by build-L1 (epoch 1; build-L1 heartbeats the lease). Repo plangraph, worktree from origin/main (e70eb566),
branch lane/rt-p1-governor, cwd /home/s/code/plangraph-worktrees/governor. Draft PR; head trailer `Plan-Item: plangraph:RT-P1-GOVERNOR`.
Done (keeper): daily job writes section-9 metrics into the keeper; governor blocks new planner/expert starts when Claude burn > 1/7 per
day (operator-started sessions exempt); provider quota throttle for worker legs; never blocks answers, operator-started sessions or
operator-requested judge runs; overrides logged.
Spec: design section 9 (/home/s/code/routing-worktrees/rethink-setup/docs/reports/2026-10-15-rethink-setup-design.md L190-206).
Metric source + method = routing:RT-P0-BASELINE (routing main 42ffcc0: bin/rt-p0-baseline.py, docs/reports/rethink-p0/baseline.{md,json}):
weighted = in + out + cache_write + 0.1 x cache_read, dedup per message id; landed = first-parent merges; burn index = day / (week/7).
Port the computation into plangraph (public-repo hygiene: no home paths/hosts; config via env/args), do not import from routing.
Shape (decide smallest; document in docs/governor.md):
1. keeper: metrics table (date, metric, value, source, computed_at; upsert per date+metric) + PG migration + SQLite parity if the store
   still dual-runs + read API (GET) + write via authenticated verb. Schema/migration = HIGH -> judge review by build-L1's judge.
2. daily job: CLI `plangraph metrics daily --date D` (idempotent), run from a timer later (no systemd install in this lane; document unit).
3. governor: pure function `governor.allow(start_kind, initiator, now) -> (allow, reason)` reading today's burn + provider quota;
   start_kind planner|expert|judge|answer|worker_leg; initiator operator|agent. Answer/operator/operator-judge always allowed.
   Override = explicit flag, logged as keeper event/audit row. Wire it into the planner-kit start path (src/plangraph/planner) and expose
   it for the spawner (AutoOS spawner wiring = follow-up item; name it in the report, do not edit AutoOS).
4. worker-leg quota throttle: per-provider daily quota config -> allow/deny + reason; where the Rust worker picks a leg, add a keeper/API
   check call only if small, else document the interface for RT-P1-WORKER.
Tests: unit (burn calc on fixture transcripts, governor matrix incl. never-block cases, override logging), keeper API + migration tests
(SQLite + pytest-postgres). Full local suite once before PR push; CI per common rules.
Seats: HIGH = 2 non-Claude families PASS on the final head (qwen3.8-flash via qodercli + gpt-oss/ovh or mimo-v2.6 via mimo), HIGH seat
format per vector file:function. MERGE-READY <head sha> + CI run + seat dirs to build-L1. Never merge, never touch the live keeper.
