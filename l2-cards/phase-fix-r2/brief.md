# phase/rt-p1-bus FIX ROUND 2 (D-2540) · base phase ea21b539 · ONE writer, lane/phase-fix-r2
Delta r2 seats: qwen part A (Python keeper, client, registry) PASS. qwen part B (Rust exec + wake) FIX. Evidence: build-L1-bus-seats/phase-r2-ea21b539{A,B}/ on coding.vm.
Writer: agy/gemini-3.8-flash, else mimo/mimo-v2.6-pro. Never Claude, never qwen (qwen is the seat). max_tokens 128000 (D-2694).
Rules:
- red test first for each HIGH/MED;
- cargo test + clippy -D warnings + cargo fmt --check, plus the affected pytest on SQLite AND PG16;
- then `gh workflow run tests.yml --ref lane/phase-fix-r2` green;
- full pytest before merge (D-2726: deploy template);
- trailers `Plan-Item: plangraph:RT-P1-BUS` + `Writer: <model> <run id>`.
Each finding is FIXED (named test) or WONTFIX <id>: reason. Report, then stop.

## Rust exec (rust/worker/src/workflow.rs, activities.rs, keeper.rs)
- R2-1 (seat HIGH, contingent):
  - Seat claim: observe_card is gated on `ctx.patched(OBSERVE_PATCH)`, and "nothing records the marker".
  - L1 note: GATE_PATCH (workflow.rs ~L1193) uses the same ctx.patched() call. In the Temporal SDK, patched() itself records the marker on a fresh run.
  - Prove it with a workflow-level test (the SDK test harness or replay):
    - a fresh run that parks schedules `observe` and records the marker;
    - the park carries the observation, not the fallback;
    - a history that predates the marker replays with None.
  - If the test shows the marker is not recorded, fix the wiring.
- R2-2 MED: an observe failure poisons the park.
  - Scenario: observe returns None during a keeper hiccup, the park sends the live-set fallback, the keeper refuses exec_stale_state, and Temporal retries only the park, so it fails forever.
  - Fix: on exec_stale_state from a fallback-only park, re-run observe (bounded retries), or make observe retry before it degrades.
  - Test: the cap-park on a `waiting` card lands after one failed observe.
- R2-3 MED test: workflow-level coverage of the observe -> park path (it can share R2-1's test).
- LOWs:
  - park_path JoinError: keep the panic cause in the activity error.
  - observe_read_within detaches a stalled blocking task: give card_state's ureq agent a connect+read deadline.
  - StubKeeper::park must refuse the bare live set when the held state is waiting/needs_input (match store_worker).
  - serve_n in the keeper.rs tests: count requests and assert no third read.

## Wake (src/plangraph/fleet_bus/wake.py, wake_http.py, tests/test_fleet_bus_wake*.py)
- R2-4 MED (open since r1 V5-1): nothing runs WakeHook.run.
  - Add a runnable entrypoint, `python -m plangraph.fleet_bus.wake --config <file>`. The config holds the recipient -> session map, the keeper URL, the token file and the session base.
  - Fail closed on a bad config.
  - Add deploy/systemd/fleet-wake.service.example (user unit, Restart=on-failure, no secrets inline, no site IP).
  - Test: the entrypoint parses the config and drives one wake against the real keeper (World) + a stub session server.
  - Docs: docs/FLEET-BUS.md "wake daemon".
- R2-5 MED: a same-origin redirect downgrades the wake POST to a body-less GET, and the resulting 204 counts as a wake.
  - Fix: follow only 307/308 for POST; any other 30x on a wake = WakeBusy.
  - Test: the stub server records the method + body; assert the final request is a POST with the body.
- LOWs:
  - `_origin` ValueError on a bad port: catch (OSError, ValueError) or validate.
  - A permanently cross-origin-redirected endpoint must end as WakeRefused after N tries (logged), not stay busy forever.
  - The redirect tests record self.command.
  - The 60 s wake test: a positive handshake instead of sleep(0.1).

## Docs gap (BUS-REGISTRY-PERSIST)
- docs/FLEET-BUS.md: a short "host registry persistence" paragraph:
  - bus_hosts table;
  - restore forces health offline;
  - GET hosts is joined with the inventory;
  - no key path is stored.

## Fences
- No .github edits.
- No keeper store/schema edits.
- No fleet_bus/client.py edits, except the wake entrypoint import if needed.
- No site IP in code or tests (scrub_check must be 0 hits).
