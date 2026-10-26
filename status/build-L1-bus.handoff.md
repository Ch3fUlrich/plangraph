# build-L1-bus FINAL HANDOFF · gen-5 -> build-L1 (D-2907 fold) · 2026-10-26T04:5xZ
Read with: status/build-L1-bus.card.md (rules, D-ids, item table), l2-cards/phase-fix-r2/{fix-r2b1.md, fix-r2b2.md, replan-r3.md, brief.md "## Wake"/"## Docs gap", amend-d2739.md I5-I9}.
build-L1-bus starts nothing after this file. No live writer/seat runs of mine (all ended).

## State
- Phase branch phase/rt-p1-bus head 9287648a = R2a' MERGED via PR #221 (lane/phase-fix-r2a @5af5663a, CI 40263454552 green).
  R2a' seats: r4 qwen run-719676a2 @27948c8f fix -> bf7c3bfb; r4b qwen run-e1673f3f @bf7c3bfb PASS; r4b glm-5.3-flash-free run-11c4819b @5af5663a PASS (delta 27948c8f..5af5663a + D-2876 merge hunks + D-2878 status change). Evidence: build-L1-bus-seats/phase-r4b-R2a-{bf7c3bfb,5af5663a}/ (SHA256SUMS). Accepted LOWs listed in the PR #221 body.
- R2b lane lane/phase-fix-r2b (worktree ~/code/plangraph-worktrees/bus-fix-r2b, base 9287648a):
  - R2b-1 (wake_http redirect class, I5-I7; brief fix-r2b1.md) = 5020d9ed, writer mimo-pro run-042f3d8f (run ended "failed" before commit after its full-suite run was cut; L1 verified: 6 brief test names present, red-before 9 failed on old wake_http.py, targeted 131 pass, full pytest 7696 pass, scrub (tracked tree) 0; committed with Writer trailer). Pushed; CI 40270138022 triggered 04:5xZ -> CHECK.
  - R2b-2 (daemon entrypoint `python -m plangraph.fleet_bus.wake --config`, deploy/systemd/fleet-wake.service.example, FLEET-BUS.md "Wake daemon" + "Host registry persistence", I8-I9; brief fix-r2b2.md) = NOT STARTED. Spawn: `W=~/code/plangraph-worktrees/bus-fix-r2b CARD='{"estimate_tokens":60000,"paths":["src/plangraph/fleet_bus/wake.py","tests/test_fleet_bus_wake_daemon.py","docs/FLEET-BUS.md"],"has_tests":true,"risk_tags":["public-contract"]}' ~/.local/share/plangraph/build-L1-bus-tools/run-fix.sh r2b2` (gate-queued, stall/no-first-write cancel; brief path l2-cards/phase-fix-r2/fix-r$S.md resolved from build-L1-bus checkout).
  - Left out on purpose: r2 seat-B LOW "60 s wake test sleep(0.1)" (seat itself: "Acceptable") -> note in PR body.
- Then: 1 qwen seat on 9287648a..R2b head (replan-r3 R2b: 1 qwen; seat families != mimo/claude/gemini/openai; qodercli-qwen3.8-flash via spawner, prompt <128 KiB, mkr4b.sh is the pattern) -> PR --base phase/rt-p1-bus + `gh pr merge --merge --match-head-commit` -> merge main into phase ONCE (schema fence: SQLite v16 / PG 9 already renumbered in 89da4c78) -> phase PR -> `GO-REQ phase/rt-p1-bus <head> <seat paths + sha256>` to routing (HIGH: keeper schema + worker -> judge).

## Claims to hand over (owner build-L1 token, sh ops/pg.sh in build-L1-bus checkout)
- plangraph:RT-P1-BUS-KEEPER, plangraph:EXEC-OBSERVED-STATE, plangraph:RT-P1-BUS-DISCOVERY: lease to 2026-10-26T14:39Z (renewed 02:39Z). Detached hb loop pid 3562149 (ops/hb-keys.txt, every 30 min, 20 iterations from ~22:2xZ -> ends ~08:2xZ); kill it when build-L1 takes the keys over.
- plangraph:RT-P1-BUS-WAKE: claim REFUSED ("unfinished dependencies") -> commits carry it as Plan-Item trailer only.
- Held/released earlier: BUS-WAKE-STOPGAP-2, BUS-WATCHER-TRACKED, RT-QC-ACCEPTS (unclaimed). BUS-PEER-DISCOVERY-ADOPT HOLD (D-2730).

## Tools (~/.local/share/plangraph/build-L1-bus-tools/)
gate.sh (host gate), run-fix.sh S (now takes env W= worktree and CARD= json), mkr4b.sh HEAD (delta seat prompt; base 27948c8f hardcoded -> adapt B= for R2b), glmretry.sh R (glm seat with 30-min free-limit backoff x12), r4wait.sh.
Spawner: `PYTHONPATH=~/code/plangraph-worktrees/main-ro/src python3 -m plangraph.spawner.cli spawn|wait|status|cancel`. mimo-pro writer runs often end without committing: verify diff vs brief + brief test command (D-2889), then commit with `Writer: mimo/mimo-v2.6-pro plangraph-spawner <run id>`.
Hand-Edit limits: src/plangraph/keeper/* is AUTH_DOOR (tools/ci_commit_gate.py) -> writer only.

## Lessons (for routing LESSONS)
- A merge resolution kept both sides' insertion of the same tuple entry (copy_to_pg.TABLES jev_shadow twice): seats rated it LOW/"harmless"; it broke copy-to-pg for any keeper with JEV rows. L1 should check merged lists/tuples for duplicates (cheap unique test) on every conflict hunk.
- glm-5.3-flash-free free limit reset within ~35 min (fail 02:48Z, ok 03:23Z).
