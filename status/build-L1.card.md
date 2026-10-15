# build-L1 card — rethink P1 (D-1285, batch D-1303)
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md · design (binding, 14a): .../docs/reports/2026-10-15-rethink-setup-design.md
Branch build-L1 = control branch (card, operator rounds, batches, handoff); never merged to main. Code = lane worktrees from origin/main.

## P1 execution order (tracks run in parallel; max 3 Sonnet writers)
A infra (live, via routing GO): A1 server:RT-P1-PG-DEPLOY (server-L1-main) -> A2 server:RT-P1-TEMPORAL-CHECK live part (RAM coding.vm else cloud.vm, WSL2 reach, UI link) -> decision row
B keeper:  B1 RT-P1-CONTRACT (freeze current API, black-box suite) -> B2 RT-P1-KEEPER-PG (port, new tables/states, blue-green; HIGH) -> B3 RT-P1-PLANNER-KIT | RT-P1-GOVERNOR (needs RT-P0-BASELINE)
C rust:    C1 RT-P1-TEMPORAL-SPIKE (local dev server) + C2 RT-P1-WORKER-CORE (engine-free) -> C3 RT-P1-WORKER (Temporal + keeper glue, merge lock; HIGH) -> C4 RT-P1-REVIEW
D events:  after B2+A2: RT-P1-PROJECTOR (HIGH) -> RT-P1-INBOX
E gateway: after autoos:RT-P0-OR-DRIFT: autoos:RT-P1-CLASS-COMBOS (live apply under GO)
Exit:      RT-P1-E2E (free leg card end-to-end + Inbox question resumes run)
Start now: B1, C1, C2 (batch 2026-10-15-build-l1-b01 reorders deps; pending routing/fixes-L1)

## State
- 2026-10-15 start. Ready for build-L1 in keeper: none (all gate on PG-DEPLOY). Need: build-L1 token, batch b01 applied, PG-DEPLOY GO.
- 2026-10-15 D-1305: b01 accepted (fixes-L1 applies), token via fixes-L1, PG-DEPLOY asked of server-L1-main (plan first). SPIKE on coding.vm ok (-j4, stop dev server idle).
- Writers running (Sonnet, isolated worktrees): lane/rt-p1-contract, lane/rt-p1-temporal-spike, lane/rt-p1-worker-core. Claim on token arrival.
- Build rule (D-1319 disk): one CARGO_TARGET_DIR, cargo clean after; Temporal SDK builds (5 GB target) on the workstation via workst-L1-autoos. D-1317: tool-less seats on coding.vm ok (max 4 fleet).
- WORKER-CORE: seat ovh/gpt-oss-120b PASS, janitor fix ea684b9b, PR #1. SPIKE: all demos pass (SDK 1.1.0), adopt-with-workarounds; FINDINGS pending.
- 2026-10-15 16:05 gen-2 (8f5ea8): CONTRACT done (main 7e5a61e3, live via GO D-1331, keeper ci block on). seat_verdict merged (PR #4, e56d2d9a). WORKER-CORE commit+ci✓; ovh r5 = fix (3 HIGH) -> writer lane/rt-p1-worker-core-r2 triaging. xiaomi seats time out; 2nd family = ovh qwen3.8-27b (D-1329) but no reviewer-alibaba token yet.
- 16:15 gen-2 recycle: handoff #2. CI triggers fixed (D-1338, main ab3d0817). KEEPER-PG-CODE claimed, W1 writer running. WORKER-CORE r2 PR #6 green, awaiting 1 recordable seat (qwen).
- 20:0x gen-4 (5fadc0): beat 17,47 (fleet rule). PR #8 seat gpt-oss 5/5 pass (keeper-pg-seats/w12-ffc1bba), await ffc1bba push+CI -> merge. CI-PARALLEL PR #11 e91a415 CI ✓ (run 38796191893, 2557 passed 113 s), seat pass; L2 backup stopped at cap. #11 conflicts #8 (tests.yml, conftest.py) -> merge #8 first, then rebase #11 (add xdist to pytest-postgres). mimo: no OmniRoute cap; review-call.py fix routed (operator GO).
- D-1364: messages only BLOCKED/DONE+sha/GO/incident/launch; L2s no beat; my beat 17,47 only while polling CI/lease.
- 20:2x: PR #8 merged main 1779d2d. W3 go (keeper-pg). REBASE-11 go (backup). Claimed PROJECTOR/PLANNER-KIT/INBOX-CODE (lease 21:50Z), briefs e33eb87, worktrees from 1779d2d, L2-LAUNCH x3 asked of routing. Watch: migration-number collisions across lanes at merge.
- 20:5x D-1374 router paused until ~04:10Z: no messages to routing; decisions D-935 default + log here.
- AO-DEEPINFRA-REMOVE: fixes-L1 OFFLOAD to build-L1 L2 (fixes-L1 merges; order maingreen -> ordrift -> priv-writers -> reviewcall-mimo -> deepinfra). Brief 87720f8. Writer leg 1 = omniroute/mimo-v2.6-flash (all L2s told).

## FOR ROUTER
- L2-LAUNCH build-L2-deepinfra /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-deepinfra-remove.md /home/s/code/AutoOS-worktrees/deepinfra-remove (worktree to create from AutoOS origin/main AFTER priv-writers merges; build-L1 creates it). Sonnet, coding.vm (check MemAvailable >= 1 GB + headroom).
- EDIT server:RT-P1-PG-DEPLOY done_criteria (D-1375, B2 off-host backup optional): replace "nightly off-host backup + restore drill" with "nightly backup (VM/NFS backup covers off-host; B2/OS-191b optional, non-gating) + restore drill". It gates plangraph:RT-P1-KEEPER-PG, server:RT-P1-TEMPORAL-CHECK, routing:RT-P1. Checked all plangraph/RT items: no other B2 dep (M2B-OFFHOST = keeper hosting, not backup).
