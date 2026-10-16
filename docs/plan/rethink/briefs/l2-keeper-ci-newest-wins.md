# L2 brief: plangraph:KEEPER-CI-NEWEST-WINS (D-1479, prio 2, HIGH; parent build-L1) — build-L2-ci-newest-wins (sonnet medium)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding) + skill unattended-orchestration. TTL 3 h. Effort medium.
Hard limits: NO push to main, NO merge, NO crons; stop on any harness/classifier denial, report verbatim; report ONLY to build-L1; no live keeper/systemd touch (restart = build-L1 under D-915 after merge).
Item: `ops/pg.sh show plangraph:KEEPER-CI-NEWEST-WINS` (build-L1 holds the claim). Done criteria there are binding.
Problem: src/plangraph/keeper/evidence.py `_ci_conclusion` keeps the newest run per (check_suite, app, name). Each workflow_dispatch run = its own check suite, so an old cancelled/failed
dispatch on a sha blocks commit+ci forever (CI-PYTEST-SPEED 998f8f3: suites 105351488221/105355583034 cancelled, 105362026458 failure, newer green 38882090233 ignored).
Rule (router D-1479): for DISPATCH suites the newest COMPLETED run per (workflow, job name) on the sha wins; a cancelled run never counts once a newer completed run exists; a newer failure
still blocks; pull_request suites unchanged; mixed PR + dispatch on one sha resolved by newest completed per (workflow, job).
Note: GET commits/{sha}/check-runs gives check_suite.id but not the event/workflow; evidence_ci.py (`check_runs`) must map suite -> workflow run (event, workflow name)
via actions API (e.g. repos/{o}/{r}/actions/runs?head_sha=) or check-suites API; keep the fake contract in tests in step with the real gh output (R-worker-04: cite real fields).
Write set: src/plangraph/keeper/evidence.py, evidence_ci.py, their tests (+ fakes). Nothing else in keeper without asking build-L1 (KEEPER-SNAPSHOT-PG lane owns api/store/snapshot_io).
Tests (failing first): cancelled-then-green dispatch -> success; green-then-failed -> failure; PR suites unchanged; mixed PR+dispatch; truncation/pagination still refused.
Live replay (read-only): run the new verifier against 998f8f394b33cece65ee5bf52253fcbc6ecae560 using recorded pre-rerun check-run JSON (reconstruct from gh api attempts; runs 38875149323
38876665137 38879016538 38882090233) -> success without reruns; save the fixture under tests/fixtures.
L3 writers via autoos-agent spawner free-first; you review every diff. Seats: 2 incl. >=1 non-Claude (gpt-oss ops/seat.sh, Qwen qodercli), vectors per file:function
(evidence._ci_conclusion, evidence_ci.check_runs): false-green (a failure masked), PR path regression, API field honesty. Branch seats/ci-newest-<sha>; seat_verdict dry-run rc 0.
Branch lane/keeper-ci-newest-wins from origin/main (7f5a35a+); draft PR; trailer `Plan-Item: plangraph:KEEPER-CI-NEWEST-WINS` on every commit.
CI: workflow_dispatch on workst/fleet-ci or ci.vm, max 1 pytest-postgres run per lane, NEVER cancel a run on the head sha. MERGE-READY <sha> + CI run id + seat branch to build-L1. Card <= 40 lines, RECYCLE-READY before 230k.
