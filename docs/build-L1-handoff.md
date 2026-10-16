CARD: /home/s/code/plangraph-worktrees/build-L1/status/build-L1.card.md (current state; read first)
# build-L1 handoff #10 (gen-10 -> after /clear, 2026-10-16 ~19:1xZ)
Startup: skill unattended-orchestration (+ references/main-orchestrator.md). NO crons (D-1377, launch order); wait via ONE background
command that exits. Effort medium. Msgs only BLOCKED/DONE/GO-request/incident/launch. Card <= 40 lines; CARD-READY before 230k.
Rules: D-1504 model = its path only (qwen3.8-flash ONLY qodercli; mimo-v2.6-flash ONLY mimo provider; muse opencode; gemini agy).
D-1442 judge (server-L1-routing (ref changes on recycle; ListAgents, newest)) only HIGH; standard lanes self-approve on 1 non-Claude seat. D-825 live steps need router GO.
FREEZE ON (D-1398 L7): no Omnigraph writes until routing FREEZE OFF (keeper unaffected).
Operator wants: report DONE for plangraph:RT-P1-E2E to routing at once when it lands.

## Gen-10 landed
- RT-P1-KEEPER-PG DONE (judge ACCEPT; verdicts alibaba qwen/qodercli + xiaomi mimo judged TG n/a; note seats-archive/keeper-pg-cutover-a6eb49f6/DONE-NOTE.md). Routing D-1512.
  Step 4 (delete-or-archive frozen sqlite + backup pg-cutover-b3d71096-20261016T164230Z) HELD: send GO-request to routing >= 2026-10-18 16:43Z.
- RT-P1-PLANNER-KIT DONE: PR #36 merged main e70eb566 (head 9bf0a319, CI 38938032186), seat seats-archive/planner-kit-9bf0a319. L2 21fa0888 stopped, worktree removed.

## In flight: server:RT-P1-TEMPORAL-CHECK (owner build-L1, claimed epoch 1)
- Lease: background heartbeat loop task b0cndr9ag DIES with /clear -> after clear run `ops/pg.sh heartbeat server:RT-P1-TEMPORAL-CHECK` at once
  (lease 40 min), then restart one background loop: `for i in 1..8; sleep 2400; ops/pg.sh heartbeat server:RT-P1-TEMPORAL-CHECK`.
- Plan: Server docs/operations/temporal-check-live-plan.md + temporal-validation-runbook.md @ improve 314c06d42.
- GO D-1513 (routing): T1-T6 + T9; executor prox-L1-main -> its L2 prox-L2-temporal-check (1149b99b, on home). Conditions: T0 certs = operator
  (OS-208, being minted now); T1 floors home free mem + cloud >= 3 GB; T2 exactly 5 containers; AFTER T3: build-L1 runs keeper admin verify +
  snapshot 200 within 5 min, red -> prox rolls back T3 (--action down). T7/T8/T10 need separate GO.
- Done: T1 PASS (BASE_MEMAVAIL_MB 19073, swap 52 MB free/2 GB note), T2 PASS (dryrun task 164707). T3 BLOCKED on certs; prox relaunches its L2 when certs exist.
  Log: seats-archive/temporal-check/live-log.md (append each step).
- Keeper check after T3: `ops/pg.sh admin verify` (or `ops/pg.sh snapshot`; check `ops/pg.sh --help`) + `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/v1/snapshot` -> 200.
- Then: T5 idle samples (+10m/+1h/+6h/+24h, prox); T9/C probes = L2 via routing `L2-LAUNCH build-L2-temporal-probes
  /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-temporal-check-probes.md <cwd>` (needs client certs, workstation build).
  T11 decision row (adopt / adopt-with-workarounds / fallback) with +24h sample -> send to routing, then evidence decision + done.
- PG headroom: Temporal pools 20 conns; PG 11/60 before deploy.

## Next RT-P1 by plan order
WORKER + PROJECTOR <- TEMPORAL-CHECK; INBOX <- PROJECTOR; REVIEW <- WORKER; GOVERNOR <- routing:RT-P0-BASELINE; CLASS-COMBOS <- autoos:RT-P0-OR-DRIFT
(router assigns RT-P0 owners next gen); E2E <- all. Briefs exist: briefs/l2-worker-code.md, l2-projector-code.md (code parts done; write live-lane briefs).
Other: Q-API-PUSH (ex RT-P3-ASK-PUSH, HIGH, owner build-L1; orch-setup-plan @6087fe20 awaits routing). D-1514: + UI-FLEET-TAB, WORKER-USES-PLACEMENT (orch-setup-plan @026540cd, await routing); plan2 lane J adds keeper host_samples migration (no collision with mine). CI-RUNS-ON-WORKST re-scoped (private repos only).
ROLLBACK-HOLD after 2026-10-17 06:42Z: ask routing to retire drop-ins live-sync-1b923e5/8165c22/cd85d13 + worktree-proposal f3bac63.
## Tools
pg CLI: `ops/pg.sh <verb>` (ready/show/claim/heartbeat/evidence --kind commit+ci --commit SHA/submit). Verdict: `python3 <(git show origin/main:tools/seat_verdict.py) --key K --seat-dir D --writer-family F --token-dir ~/.config/plangraph`.
Seat dirs: qwen via ops/qseat.py; mimo via ops/seat.sh (review-call: don't add a bare TEST-GAMING line to prompts, the standing question handles it).
Peers: routing [5a7958], server-L1-routing (ref changes on recycle; ListAgents, newest), prox-L1-main [8fa900] (Remote Control), server-L1-main.
