# L2 brief: build-L2-bus-keeper (OpenCode, cx/gpt-6.1-sol, workstation)
Parent: build-L1-bus (took over from build-L1, D-2534). Load skill unattended-orchestration (L2 rules: orch + coord heartbeat). Effort medium.
Level: L2. Never merge or push main, no crons, card <= 40 lines next to this brief's copy, RECYCLE-READY <card> before 270k.

## Chunk (D-2532 P1; all items claimed by build-L1-bus, it heartbeats them)
Read each item first: `pg show plangraph:<KEY> --brief` (desc + done criteria are the spec; do not paraphrase).
1. plangraph:RT-P1-BUS-KEEPER (HIGH p8): keeper messages table, append-only hash-chained, relay acks after commit,
   write/read verbs by trace_id/key/time, pg_notify hint + long-poll on direct conn re-reading after last seq, jsonl metadata-only.
   Lane: lane/rt-p1-bus-keeper off origin/phase/rt-p1-bus. Unblocks lane 4.
2. plangraph:EXEC-OBSERVED-STATE (HIGH p5): worker sends observed card state as expected_state (rust/worker/src/workflow.rs),
   live-set fallback only when none; delayed same-run park onto newer waiting/needs_input refused; cap-park still lands; ADR + CHANGELOG.
   Lane: lane/exec-observed-state off origin/phase/rt-p1-bus. Independent of 1: run in parallel.
3. plangraph:BUS-WATCHER-TRACKED (p7): workstation relay watcher into plangraph tools/, no site literal, bind+session from env,
   wake via POST /api/session/<id>/synthetic once per relay record (cursor survives restart). Lane: lane/bus-watcher-tracked. Parallel to 1+2.
   Replace the untracked workst copy only after build-L1-bus says so (record the swap).
4. plangraph:RT-P1-BUS-WAKE (p7, HIGH): starts AFTER lane 1 merged into the phase branch. Wake from keeper message event via long-poll
   + /synthetic, no visible user message, one event = one wake, router commit = none (unit test), 2-min timer unit removed.
   Lane: lane/rt-p1-bus-wake off the phase branch at that point. Run 1+2+3 in parallel now (<= 3 writers).

## Phase-batch rule (OPERATOR D-2534, DECISIONS 3.0) - replaces per-task seats
- Every lane branches off origin/phase/rt-p1-bus (NOT main) and opens its PR INTO phase/rt-p1-bus.
- NO per-task seats. Lane is done when its PR CI is green: send `PHASE-READY <KEY> PR#<n> head <sha> CI <run ids>` to build-L1-bus; build-L1-bus merges into the phase branch.
- After all phase tasks land, build-L1-bus runs ONE HIGH review of the whole phase diff vs main; the findings come back to you as ONE fixer brief (D-2458).
- Writer brief carries the done criteria + attack vectors (D-2472); red tests first; pattern sweep.
- Writers cx/gpt-6.1-sol via autoos-agent spawn on the workstation; fallback mimo/mimo-v2.6-flash exact id (never omniroute/mimo), record skip+reason. Never Claude writers.
- Commit trailer: Plan-Item: plangraph:<KEY> + Writer: <model> <run id>. CI per D-2418; never coding.vm agent runs (D-437).

## Return (to build-L1-bus, SendMessage, one line each)
PHASE-READY <KEY> PR#<n> head <sha> CI <ids>.  BLOCKED <KEY> | what | options, *default.  Refusals verbatim, stop.
DONE note: commits, what remains, evidence per claim. Keeper: build-L1-bus holds claims + heartbeats; you never claim/submit.
When done: report, then stop this session.
