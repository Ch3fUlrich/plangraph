# build-L1 handoff (gen-30 -> gen-31, compaction recycle per routing D-2731, 2026-10-25 ~10:00Z)

Successor: read status/build-L1.card.md (refresh it first: rows below supersede it). Start the hb loop FIRST:
`cd ~/code/plangraph-worktrees/build-L1 && bash <jobdir>/tmp/hb.sh >> <jobdir>/tmp/hb.log 2>&1` (run_in_background; copy hb.sh from /home/s/.claude/jobs/2f3882a9/tmp/hb.sh; 3x8 min over ops/hb-keys.txt; restart on every exit).
Claimed keys (ops/hb-keys.txt): RT-OVERSIGHT-KEEPER-SCOPE, RT-P1-EXEC-OWNERSHIP-SQ12, CI-SELECT-DEPLOY-REFS.
Helper scripts to copy from /home/s/.claude/jobs/2f3882a9/tmp: hb.sh, spawnfix.sh (memory-gated agy > mimo-pro writer: `spawnfix.sh <worktree> <brief-name> <item>`), nomcp.json, empty/ (seat cwd).

## Rules in force
- Writers D-2683: agy/gemini-3.8-flash > mimo/mimo-v2.6-pro > openrouter/free + opencode free > mimo-v2.6-flash; never Claude; cx out ~10-30.
- Seats: D-2540 first review 2 distinct families, re-review 1 (delta); D-2511 exclude every writer family (my Hand-Edits = claude family -> no Haiku re-seat on those). Prompts start "NO TOOLS AVAILABLE..." (qodercli calls tools otherwise). Qwen: `qodercli -p "$(cat F)" --model Qwen3.8-Flash --tools "" -o json` from tmp/empty. opencode free WORKS again since 09:50Z: `opencode run -m opencode/deepseek-v4.1-flash-free "$(cat F)"` (or glm-5.3-flash-free). Haiku: `claude -p --model haiku --tools "" --strict-mcp-config --mcp-config tmp/nomcp.json < F`. Verify every seat claim against code before a fix round (about half were false today).
- D-2686: no new spawn while MemAvailable < 4000 MB. Max 4 writers here.
- L26 / D-2726: full suite (`PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider tests --deselect tests/test_planner_readiness.py::test_smoke_checks_default_dry_run`, ~12 min) before merging deploy/config/CI-selection changes.
- Quiet: no cron; messages only BLOCKED/DONE/GO/incident.

## Done this gen (gen-30)
#188, #187, #184, #190, #191/#192 (STALL-WATCHDOG), #194+#195 (STALL-WD-WIN-WORKDIR; #194 broke main, #195 fixed ab44789b), #197 STALL-WD-DELIVERY-QUIET 5d7873c6 (submitted). D-2721 closed 5. D-2731 batch applied (see below).

## In flight (gen-30 bg shells die with the session; check each)
1. Live stall watchdog repin D-2731: code host DONE (live-stall at 5d7873c6; tick 09:46:55 "completion delivery skipped: keeper not configured (queued)" + "! stall watchdog warning keeper skipped Unreachable", no traceback, rc 0). Workst repin asked (H:\fleet\wt\pg-watchdog ab44789b -> 5d7873c6). On workst DONE: send routing one tick line per host (D-2731 asks for it). Rollback: repin 9b5ba588.
2. #196 CI-SELECT-DEPLOY-REFS (HIGH 2 seats): lane/ci-select-deploy head 97befbe4 (worktree ~/code/plangraph-worktrees/ci-select-deploy). Seats: Qwen PASS(lows) + Haiku FAIL -> fix1 -> Qwen re-review PASS (session 1476a472). Full suite was running: log jobs/2f3882a9/tmp/full-ci-select-deploy.log (re-run if missing). Green -> `bash ~/.local/share/plangraph/tools-l1/job-tmp/mergepath.sh 196 <full head sha> plangraph:CI-SELECT-DEPLOY-REFS "ci: select tests naming changed deploy/config files"`, then submit, prune hb-keys.
3. #198 MODEL-ROUTE-SPAWN writer order: lane/writer-order-d2683 head 3c982c4d (worktree ~/code/plangraph-worktrees/writer-order-d2683). Seats: Qwen FAIL r1 -> fix1 -> Qwen FAIL + Haiku FAIL -> fix2 + Hand-Edit -> Qwen re-review PASS (session 909abdb3). Full suite log jobs/2f3882a9/tmp/full-writer-order-d2683.log (earlier run: only test_spawner_round1 benchmark roundtrip failed; Hand-Edit 3c982c4d fixed it). Route dry run implement: agy-gemini-3.8-flash, mimo-v2.6-pro, openrouter-free, opencode-deepseek-v4.1-flash-free, opencode-glm-5.3-flash-free, mimo-v2.6-flash, cx-gpt-6.1-sol, deepseek. BLOCKED for mergepath: D-2731 gated MODEL-ROUTE-SPAWN on new SPAWN-ISOLATION-INPLACE, claim refused (unfinished deps), mergepath needs the claim. Ask routing: merge #198 now under the item without the claim, or after SPAWN-ISOLATION-INPLACE. Real spawns for close: code run-667e9a9e, run-b8917744, run-80542f93; workst run-c8d22f50 (cx skipped not served -> agy).
4. New leaves (D-2731, owner build-L1, standard, open): SPAWN-ISOLATION-INPLACE (gates MODEL-ROUTE-SPAWN), SPAWN-ORPHAN-REAP (gates PH-MODEL-ROUTE). Need briefs + writers. Batch files: briefs/lanes-d2657/batch-d2731/ (source.md, batch.yaml, pre-snapshot.json). Applied with router token under OS-92 after release of MODEL-ROUTE-SPAWN (edit needs open).
5. #164 RT-OVERSIGHT-KEEPER-SCOPE (HIGH, round 3/3): r10a workst lane/keeper-scope-r10a 388bb1c4 (A1-A8 fixed). CI 40180813017 red on 4 PG tests that ALSO fail on base 88bc89d4 (set_authorizer SQLite-only, sqlite_master on PG, open_alarm index text, batch_ref_is_bounded). I gave workst option (a) continuation writer with conditions (product-vs-test first, same property on PG, forged-prepared security test proven on PG, no weakening). r10b running on workst (plangraph spawn). Next: both DONE -> merge into r6, keeper tests SQLite + PG, delta seats (Qwen + opencode free; exclude writer families openai/xiaomi/qwen/google -> qwen excluded! use opencode deepseek-free + glm-free or Haiku), GO-REQ round 3.
6. RT-P1-EXEC-OWNERSHIP-SQ12 (HIGH): writer on workst lane/exec-own-sq12; then 2 seats, GO-REQ.
7. Serial keeper queue after #164: #166 KEEPER-PG-RECONNECT, #139 KEEPER-TRAILER-MULTI, KEEPER-WATCHDOG-ROLE.
8. Operator-gated: OS-PG-PUBLIC-PUSH, OS-KEEPER-LS9-MIGRATE.

## Notes for routing / lessons
- run-u*.scope failed units (pg-jail fork-bomb tests u1464..u2002) are from jobs e4241665 / opencode review / sandbox-wA, not build-L1: left as is.
- Orphan opencode serve pid 852130 (scope ...063256-pgl3mt-r2...) stopped 09:5xZ; a normal `opencode run` leaves its own managed serve on 49374 and later runs reuse it (fine).
- Lesson: seats' false claims ~50% (Qwen, Haiku) -> verify before fix briefs.

## Traps
- zsh: wrap chains in bash -c; heredoc with $( ) needs quoted delimiter (bash_guard). git cherry-pick has no -q; use `git cherry-pick FETCH_HEAD` (single writer commit).
- mergepath: KEY must be claimed; needs full sha; no submit; failed run leaves merge-<PR> worktree.
- apply_batch: principal must be routing-00; edit of a claimed item refused (release first).
- ci_commit_gate: one trailer block Plan-Item + Writer, or `Hand-Edit: <who> D-2240 <reason>` (<=10 lines).
- No `python` binary locally; no xdist.
- ~/code/plangraph = control repo. Control push: git push control build-L1 only. Never dump environ.
