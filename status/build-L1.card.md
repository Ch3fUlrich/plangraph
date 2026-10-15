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
