# REPLAN v2 R2a' -> 6 slices <= 150 lines, one red test each (D-2836, DECISIONS 0.2e) · build-L1-bus gen-3 (Opus) · 2026-10-25
Design = replan-r3.md (D1-D7, invariants) unchanged. Why v2: mimo-pro R2a' was stall-cancelled twice (D-2835).
- run-c3b5a522: 0 writes in 22 min.
- run-8cba759a: 1 file at 18:50, then nothing for 15 min.
mimo-pro works in long single turns, 4-20 min between tool calls. A ~260-line, 7-part brief does not fit the 15-min no-write window.
Base for all slices: lane/phase-fix-r2a @59c1287e (R2a, agy). Slices run serially, each on top of the previous one. One commit per slice, each green alone.
Each slice brief = header rules (r2a-prime-prompt.md top + its "SDK API notes") + replan-r3 design item(s) named below + only the invariant row(s) below. Each brief is ~6 KB, not 33 KB.
Rules per slice: write the red test FIRST and save it within 5 min (stall rule); <= 150 changed lines, generated fixtures excluded; the cargo fmt/clippy/test checks with PG_TEMPORAL_ADDR set; exact test names in the report (L1 greps them).

| # | Design | Files | One red test (exact name) | Est. lines |
|---|---|---|---|---|
| S1 | D1a: `Keeper::card_state -> Result<Option<String>, KeeperError>`. HttpKeeper maps non-2xx->Status, transport->Http, oversize/decode->Decode. StubKeeper fail_times -> Err(Status(500)). observe_read_within maps Err->None for now (behaviour unchanged) | keeper.rs, activities.rs | `http_card_state_reports_failures_as_errors` (500, oversized, bad JSON -> Err; 200 -> Ok(Some)) | ~110 |
| S2 | D5: rename `card_state_agent` -> `read_agent`; add `HttpKeeper::with_read_deadlines(connect, read)`; new() keeps 2 s / 5 s | keeper.rs | `http_card_state_read_deadline_is_enforced` (accepting-but-silent TcpListener, 200 ms -> Err within 1 s) | ~50 |
| S3 | D1b: observe_read_within -> Result; the `observe` activity returns a retryable ActivityError on Err; observe_card's degrade path logs the cause | activities.rs, workflow.rs | e2e `transient_observe_failure_is_retried_not_degraded` (I3a: fail x2 -> 3 card_state calls, 1 observe activity, park ["waiting"]) | ~90 |
| S4 | D2+D3+D4: typed `exec_stale_state` non-retryable failure on park 409; `is_stale_state_refusal` cause-chain helper; replace R2a's untyped loop with a PARK_REOBSERVE_PATCH-gated loop (stale only, max 2, cause in the final error, logs each retry); const-name test | activities.rs, workflow.rs | e2e `stale_state_refusal_reobserves_at_most_twice` (I3b: land on re-observe 1; 409 forever -> error after 2, message has the cause) | ~140 |
| S5 | D6 + I3c: env_or_skip panics under CI without PG_TEMPORAL_ADDR; StubKeeper ordered verb log `calls()`; I1 asserts card_state before park and the park's own expected_state | tests/e2e.rs, keeper.rs (stub) | e2e `non_stale_park_errors_do_not_reobserve` (I3c: 403/422/transport -> 0 extra card_state). I1 re-asserted in the existing fresh_run test | ~100 |
| S6 | D7: replay fixtures. (a) a pre-OBSERVE parking history: NONE of the 11 committed fixtures contains a park (L1 grep), so record one at ea21b539 via e2e PG_RECORD_DIR; (b) post-OBSERVE pre-REOBSERVE: recorded 59c1287e history from run 2, build-L1-bus-seats/r2a-prime-run2-partial/observe_park.json (scrub-checked: identity e2e-worker only); (c) replace the duplicate old_history_... test | tests/replay.rs, tests/fixtures/*.json | `replays_pre_observe_cap_park` + `replays_pre_reobserve_cap_park` (assert the markers are absent and the park activity is present, then replay clean) | ~60 code + fixtures |
Sum ~550 code lines, more than replan-r3's 260 estimate, because each slice re-states its tests and helpers. Each slice stays far below the stall window.

## Writer choice (routing decides; no third mimo-pro run on the same big brief, D-2836)
- (a) mimo-pro per SLICE, 6 short runs: same leg, but a different job shape (6 KB brief, 1 red test, <= 150 lines). Needs routing's OK under D-2836's "no third run on the same leg".
- (b) cx-gpt-6.1-sol when its quota returns (~10-30).
- (c) mixed: S2 (~50 lines) and S6(c) are near Hand-Edit size but above 10 lines: not allowed until ORCH-EFFICIENCY-ENFORCE (30).
Seats: ONE r4 qwen seat after S6 over 59c1287e..S6 head; prompt < 128 KiB; the seat writes its full review to a file (the spawner keeps only a 4 KB tail). L1 runs S1-S6 CI per slice and greps the cited tests before each next slice.
R2b unchanged; it starts after r4 is clean.
Lesson (to routing LESSONS): the 15-min no-write stall rule and mimo-pro's long-turn style are incompatible with > 150-line briefs. Size slices to the stall rule, not only to the 400-line review limit.
