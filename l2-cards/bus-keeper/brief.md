# L2 brief: build-L2-bus-keeper (OpenCode, cx/gpt-6.1-sol, workstation)
Parent: build-L1. Load skill unattended-orchestration (L2 rules: orch + coord heartbeat). Effort medium.
Level: L2. Never merge or push main, no crons, card <= 40 lines next to this brief's copy, RECYCLE-READY <card> before 270k.

## Chunk (D-2532 P1; both items claimed by build-L1, L1 heartbeats them)
Read each item first: `pg show plangraph:<KEY> --brief` (desc + done criteria are the spec; do not paraphrase).
1. plangraph:RT-P1-BUS-KEEPER (HIGH p8): keeper messages table, append-only hash-chained, relay acks after commit,
   write/read verbs by trace_id/key/time, pg_notify hint + long-poll on direct conn re-reading after last seq, jsonl metadata-only.
   Lane: lane/rt-p1-bus-keeper off origin/main (>= 0d3a3898). Unblocks RT-P1-BUS-WAKE, RT-P1-BUS-RUST (next chunk, L1 decides).
2. plangraph:EXEC-OBSERVED-STATE (HIGH p5): worker sends observed card state as expected_state (rust/worker/src/workflow.rs),
   live-set fallback only when none; delayed same-run park onto newer waiting/needs_input refused; cap-park still lands; ADR + CHANGELOG.
   Lane: lane/exec-observed-state off origin/main. Independent of 1: run in parallel.
Not in chunk: BUS-WAKE-STOPGAP-2 (server-L1-routing), BUS-WATCHER-TRACKED (workst-L1-autoos) - owners differ.

## Writers / seats
- Writers: cx/gpt-6.1-sol via autoos-agent spawn; fallback mimo/mimo-v2.6-flash exact id (never omniroute/mimo), record skip+reason. Never Claude writers.
  Writer brief carries the done criteria + attack vectors; red tests first; one writer per round with ALL findings + pattern sweep.
  Commit trailer: Plan-Item: plangraph:<KEY>, Writer: <model> <run id>.
- Seats (HIGH = 2): family != every writer family of the PR (D-2511). Order: qwen qodercli (alibaba) > agy gemini (google) > opencode free dsfree/glm.
  Seat prompt = item + diff + artifacts only, NO-tools header, no rulings/rebuttals (D-302). Seats run parallel to CI. Delta re-review on fixes.
- CI: workstation runner / GitHub-hosted for plangraph (D-2418); never coding.vm agent runs (D-437).

## Return (to build-L1, one line each)
MERGE-READY <KEY> PR#<n> head <sha> CI <run ids> seats <model+provider+family verdict paths>  -> L1 asks router GO (HIGH) and merges.
BLOCKED <KEY> | what | options, *default.  Refusals verbatim, stop.
DONE note: commits, what remains, evidence per claim.
When done: report, then stop this session.
