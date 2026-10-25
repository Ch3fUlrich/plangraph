# R2a: phase/rt-p1-bus FIX r2, Rust exec slice (D-2802, D-2739, D-2766)
You are the WRITER. cwd = git worktree on branch lane/phase-fix-r2a (base ea21b539). Edit files in place, run the checks, and commit. Do NOT push; build-L1-bus pushes.
Scope: rust/worker/src/workflow.rs, activities.rs, keeper.rs and their tests ONLY. Wake/docs = slice R2b (NOT yours; ignore seat lines about wake_http.py/wake.py/docs).
Budget: <= ~400 changed lines. Keep edits surgical. Never print the whole file back; edit only the needed hunks.
Checks (all must pass before commit; paste the tail of each into your final report):
- cd rust/worker && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test (whole crate)
Commit: one or more commits, message trailers:
  Plan-Item: plangraph:RT-P1-BUS
  Writer: mimo/mimo-v2.6-pro <spawner run id if known, else "plangraph-spawner">
Red test first for each HIGH/MED. Each finding below ends FIXED (named test) or WONTFIX <id>: reason.
Fences: no .github, no Python keeper store/schema, no site IP anywhere.
Final report: list of finding id -> FIXED <test> | WONTFIX reason | N/A-A | R2b; then the check tails; then stop.

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


## Rules (D-2739 amendment, applies to you)
- Fix the WHOLE defect class: every activity that degrades a failed keeper read into a fallback (observe, card_state users); every stub weaker than the real keeper CAS.
- Invariants (each one is a test assertion):
| # | Invariant | Input/state | Expected |
|---|---|---|---|
| I1 | A fresh run that parks records OBSERVE_PATCH and schedules observe before park | new workflow, usage_limit -> waiting | marker in history; park expected_state = observed ["waiting"] |
| I2 | Old history (no marker) replays unchanged | replay of a pre-marker history | observe_card -> None; no new command; no nondeterminism |
| I3 | A failed observe never makes the cap-park permanently refused | observe fails once (timeout/500), card waiting | park lands within bounded retries (re-observe on exec_stale_state) |
| I4 | A live-set fallback is never accepted for park on waiting/needs_input, in the stub or the real keeper | StubKeeper + store_worker, held state waiting | refused exec_stale_state in both |

## Seat B review, VERBATIM (Rust-owned lines are in scope; wake/docs lines = R2b; V7-1/2/3, V7-6/7 = N/A-A)
# Round-2 DELTA review — PART B (Rust exec worker + wake): V3, V5, V7
Branch phase/rt-p1-bus, head ea21b539 (delta aded8d82..ea21b539). Vectors V1, V2, V4, V6 and the new BUS-REGISTRY-PERSIST (V8) are Part A — judged N/A here.

## ROUND-1 (finding-by-finding)

Review A (qwen, no numbered ids):
- A-V2 HIGH client↔keeper signing mismatch — N/A (Part A; no store/client/test_fleet_msg change in this delta).
- A-V1 MED server-side BODY_MAX; A-V1 LOW copy_to_pg silent `[]` — N/A (Part A).
- A-V3 "no CAS hole; cap-park lands because the worker reads card_state at park time" — SUPERSEDED/OBSOLETE: that activity-time read *was* the defect B-V3-1 named. Seat A raised no V3 or V5 finding.
- A-V7 critical gap (no client→real-keeper end-to-end test) — OPEN. The delta adds end-to-end coverage only for wake (test_fleet_bus_wake.py) and the Rust park path; tests/test_fleet_msg.py's `_Handler.do_POST` fake keeper is untouched, so the V2-1 mismatch is still unexercised.

Review B (second seat):
- V1-1..V1-7, V2-1..V2-7 — N/A (Part A files unchanged in this delta).
- V3-1 HIGH call-time observation defeats the delayed-park guard — FIXED. Observation moved to decision time: `workflow.rs:park_and_alarm_in_state` calls `observe_card` then `expected_state_for(observed, fallback)` and passes the result into `park_json_with_attempt`; `activities.rs:park` now does only `exec_of(&ctx, park_attempt_of(&input), expected_state_of(&input))` → `park_path`, with the `card_state` call deleted; `ask_question` likewise no longer reads. Proven by `park_forwards_the_recorded_observation_without_reading`, `a_retried_park_with_a_stale_observation_is_refused` (stale `running` → `KeeperError::Status(409)`, `parked().is_empty()`), and `the_park_activity_path_presents_the_observation_it_passes` asserting `card_state_calls().is_empty()` on both the landing and the refused park.
- V3-2 MED cap-park dies under a transient read error — FIXED in mechanism (`observe_read` runs once at decision time; `HttpKeeper::card_state` failure no longer blocks the park POST; `http_cap_park_lands_while_show_fails` shows GET=500 while POST /v1/park succeeds), but see NEW V3-1/V3-2: the marker never gets recorded and a decision-time read failure now routes to the fallback, which the keeper refuses.
- V3-3 MED ask narrowed its CAS, contradicting the ADR — FIXED. `activities.rs:ask_question` sends `input.expected_state.clone()` verbatim; `workflow.rs:question_step` still hands the any-of live set; `docs/adr-exec-ownership.md` now says so; `ask_keeps_the_any_of_live_set_and_does_not_read`.
- V3-4 MED blocking ureq read on a tokio worker thread with no deadline — FIXED. `activities.rs:observe_read_within` = `spawn_blocking` + `tokio::time::timeout(OBSERVE_READ_TIMEOUT)`, `Err(_) => None`; `observe_read_is_bounded_by_the_five_second_cap` uses a 500 ms stub delay against a 25 ms bound.
- V3-5 LOW unbounded `into_json` — FIXED. `keeper.rs:HttpKeeper::card_state` now `take(CARD_STATE_MAX_BODY + 1)` and returns `None` past 64 KiB; `http_card_state_oversized_body_is_no_observation`.
- V4-1..V4-5 — N/A (Part A).
- V5-1 MED wake never wired (`WakeHook.run` uncalled, `client.py` passes `wake=None`, `_wake` only logs) — OPEN. The delta touches only `wake.py` docstrings, `wake_http.py` redirect policy and two test files; no caller of `run`, no client/keeper wiring, no timer-removal change on the routing side.
- V5-2 LOW debounce deviates from "one event = one wake"; only spaced events tested — FIXED (as an accepted, now-documented bound): `wake.py` module docstring states one wake per burst and at most one follow-up, and `test_two_events_five_seconds_apart_are_one_wake_per_burst` exercises the 5 s case that was previously uncovered (`deferred` at 5 s, no wake at 10 s, `len(sink.calls) == 1`).
- V5-3 LOW crash between `sink.wake` and `WakeState.save` replays the row — PARTIAL: not eliminated (still deliver-then-save, no pre-persist), now explicitly bounded and tested by `test_a_crash_between_wake_and_save_costs_at_most_one_duplicate_wake` (post-crash file absent → replay wakes exactly once → its own save writes cursor 1 → a third hook reports `duplicate`). Acceptable as a documented at-least-once tradeoff if the reviewer accepts the bound; the window itself remains.
- V5-4 LOW `urlopen` forwards Authorization across a redirect — FIXED. `wake_http.py:_SameOriginRedirects.redirect_request` compares `_origin(target)` with `_origin(req.full_url)` and returns `None` for a foreign hop, so urllib hands the 3xx back as a status (→ `WakeBusy` for the sink, `PollFailed` for the poll, cursor kept); `test_a_cross_origin_redirect_is_refused_and_never_forwards_authorization` proves the second server receives no request at all.
- V6-1..V6-9 — N/A (Part A).
- V7-1 HIGH fake keeper in test_fleet_msg.py re-implements the envelope check — OPEN (Part A test file unchanged).
- V7-2 MED vacuous `test_jsonl_outbox_stays_metadata_only` — OPEN (unchanged).
- V7-3 MED Windows shims asserted by string presence, never run — OPEN (unchanged).
- V7-4 MED 60 s wake test advanced its own fake clock (tautological) — FIXED. `test_report_wakes_router_within_60s_on_the_long_poll` now runs a real keeper (`World`/`Doors`), the real `KeeperMessagesPoll`, the default `time.time` clock, and measures `elapsed` against the commit; the self-advancing `Clock`/`Stop.waits` assertion is gone.
- V7-5 MED delayed-park tests passed the observed state directly; nothing covered `CardActivities::park`'s read — FIXED at the activity level: the read is deleted and `park_path` is now directly exercisable, with `card_state_calls().is_empty()` on both branches, plus a stub park CAS so the 409 is real. Remaining gap in V7 NEW-1 (no workflow-level test of `observe_card`).
- V7-6 LOW PG parity gated on `PG_DSN` — OPEN (Part A).
- V7-7 LOW `time.sleep(.1)` ordering in the wait test — OPEN (Part A); the delta adds a similar ordering sleep in the new wake test (see NEW V7-2, benign).
- V7-8 LOW missing cases — PARTIAL: oversized-body and cap-park-under-outage are now covered; future-ts refusal, client↔keeper body shape, `fmt` on read, agent-name recipients, keeper `bus_registry` wiring and PG TRUNCATE/DROP-TRIGGER parity are still not.

## V3 Exec observed state — new findings

HIGH `workflow.rs:park_and_alarm_in_state` / `observe_card` — the observe step is gated on a marker nothing records. `const OBSERVE_PATCH: &str = "park-observe-v1"` is introduced and only *read* (`if !ctx.patched(OBSERVE_PATCH) { return Ok(None) }`); no touched file (or the ADR) writes it, and unlike `GATE_PATCH` it has no legacy producer. Failing scenario: a run hits usage_limit, the projector writes `waiting`; because the marker is never set, `observe_card` returns `None`, `expected_state_for` hands back the live-set fallback, the park activity forwards it verbatim, and `store_worker.py:_exec_expect_state(park=True)` refuses it with `exec_stale_state` ("the live set is not an observation"). The park activity retries with the same recorded fallback forever, so the cap-park never lands and the card sticks in `waiting` — a regression of the acceptance "the cap-park after usage_limit/outage still lands", and the exact hole G3-2 was supposed to close. No test drives this: every new Rust test builds the `ExecId`/input by hand. Confirm against the patch-registration mechanism outside this diff; if it is not recorded at run start, fix by marking the patch where `GATE_PATCH` is marked and assert `card_state_calls`-free park-with-observation at the workflow level. Severity contingent on that one line of wiring.

MED `workflow.rs:observe_card` + `activities.rs:park` — a decision-time read failure permanently poisons that park. `Err(_) => Ok(None)` swallows every non-cancel observe failure (the 5 s timeout, an unreachable or slow keeper, an oversized body → G3-5 `None`), and `park_and_alarm_in_state` runs `observe_card` exactly once before scheduling the park. Failing scenario: the observe activity completes with `None` during a keeper hiccup while the card is `waiting`; the park sends the live-set fallback, the keeper refuses 409, Temporal retries only the park activity — the completed `observe` is never re-run — so every retry re-presents the same non-observation and is refused. `http_cap_park_lands_while_show_fails` does not catch this: it injects `expected_state: Some(["waiting"])` by hand instead of exercising the fallback route the workflow takes on a failed read. Fix: on `exec_stale_state` from a fallback-only park, re-run `observe` (or make the observe activity itself retry before degrading).

LOW `activities.rs:park_path` — `spawn_blocking(...).await.map_err(activity_error)?` maps the `JoinError` (panicked blocking task) through `activity_error` into an opaque message, so a keeper panic inside `park` loses its cause. Also `observe_read_within`'s `Err(_) => None` detaches the blocking task, which keeps its ureq socket and a blocking-pool thread alive until its own timeout expires; under a keeper that accepts TCP and stalls, repeated observes consume blocking threads. Bounded and documented, but worth a connection deadline inside `card_state` itself.

LOW `keeper.rs:StubKeeper::park` — the new stub CAS only compares against `s.card_state` and therefore *accepts* the live-set fallback on a `waiting` card (waiting ∈ `WORKER_LIVE_STATES`), which the real keeper refuses for park. A Rust-side regression that sends the fallback on an away state is invisible here; only the Python store test catches it. Consider refusing the bare live set when the held state is `waiting`/`needs_input`.

## V5 Wake — new findings

MED `wake_http.py:_SameOriginRedirects` + `SyntheticSink.wake` — a same-origin redirect on the wake silently loses the wake. `redirect_request` defers to `super()` for a same-origin hop, and urllib's `HTTPRedirectHandler` converts a 302/303 POST into a GET with no body. Failing scenario: the session service sits behind a same-origin 302 (gateway rewrite); the synthetic wake is re-issued as `GET /synthetic/final` without `{"text": ..., "resume": true}`; that endpoint answers 204, `SyntheticSink` sees 204 and returns, so the hook marks the row `woken` and advances the cursor while the session was never woken. Pre-existing in urllib, but this delta makes same-origin redirects an explicitly supported, tested path, so it now deserves handling: follow 307/308 only, or treat a 30x on a POST wake as `WakeBusy` rather than success.

LOW `wake_http.py:_request` — an unhandled exception type escapes the documented "never raises" contract. `_origin` calls `parts.port`, which raises `ValueError` for a malformed Location (`http://h:99999/steal`), so the transport error surfaces as `ValueError`, not `OSError`: `SyntheticSink.wake`'s `except OSError` misses it (the hook's `except Exception` still degrades it to busy, so nothing is lost, but the wake is retried forever against an endpoint that will never change) and `KeeperMessagesPoll.poll` raises a non-`PollFailed` type. Clamp/validate the port or catch `(OSError, ValueError)`.

LOW `wake_http.py:_SameOriginRedirects` — a permanently cross-origin-redirected endpoint is reported as `WakeBusy`, so the obligation is retried every `retry_s` forever with no give-up path. Documented ("can delay a wake but never drop the row"), consistent with the busy semantics; call it out only because a misconfigured session base becomes an immortal pending entry rather than a refusal.

## V7 Tests — new findings

MED new `workflow.rs:observe_card` / `OBSERVE_PATCH` / `park_and_alarm_in_state` path has no test at all. The Rust tests exercise `expected_state_of`, `park_path`, `observe_read_within` and hand-built `ExecId`s; nothing asserts the workflow schedules `observe`, records the marker, or that a real park carries an observation rather than the fallback. This is the single line of wiring that decides whether V3-1 (HIGH above) is a bug or a non-issue, and it is exactly the "perfect observer" gap V7-5 was raised for, one layer up.

LOW `tests/test_fleet_bus_wake_http.py:_server` — the hits tuple records only `(path, Authorization)`, never the method. Both redirect tests therefore pass while urllib silently downgrades the redirected wake POST to a body-less GET (V5 MED above), so `test_a_same_origin_redirect_is_followed_with_the_bearer_token_intact` asserts less than its name claims. Capture `self.command` and assert the final request is still a POST with the body.

LOW `tests/test_fleet_bus_wake.py` — two ordering sleeps without a positive handshake on the second: `time.sleep(0.1)` after `hooked.set()` only *lets* the poll block (harmless: a row committed before the poll still wakes), but the `assert not thread.is_alive()` after `join(timeout=15)` can flake on a loaded CI runner where the long-poll's 2000 ms budget plus keeper startup exceed the join window; the `assert elapsed <= 60` bound is real but is only reached because `wait_ms=2000`, so the test would also pass with a 20 s budget. Acceptable; flagging because the previous version of this test was the tautology.

LOW `keeper.rs` tests — `serve_n`'s routing closure answers `GET` with 500 and everything else with 204, so `http_cap_park_lands_while_show_fails` cannot distinguish "the park path did not re-read" from "the park path re-read and ignored the failure"; the `card_state_calls`-style assertion the activities.rs tests use has no HTTP-level counterpart. The `seen[1].method == "POST"` check is the only evidence and it does not prove the absence of a third read beyond `n = 2` (a third request would panic the accept loop rather than fail cleanly).

## BUS-REGISTRY-PERSIST (V8)
N/A — Part A. No `keeper/store_bus_hosts.py`, registry persistence, or `api.py` hosts read appears in this delta; the wake/discovery changes here neither satisfy nor contradict the table/parity/restart-survival done criteria.

TEST-GAMING: no - searched this delta for skip, xfail, retry-until-green, sleep-to-pass, a broad except that hides a test failure, a vacuous/tautological assert and a test-only branch in production code: the round-1 tautology (V7-4 self-advanced clock) is gone, replaced by a real keeper + real `KeeperMessagesPoll` + real clock with `elapsed` measured after the commit; the new `while not sink.calls ... time.sleep(0.02)` is a bounded wait that then asserts the measured latency, and the two `time.sleep(0.1)` handshakes do not decide the outcome; `StubKeeper::set_card_state_delay`/`park` CAS and `serve_n` are test doubles used to drive the production functions (`observe_read_within`, `park_path`, `HttpKeeper::card_state`) rather than to bypass them; `crashed.state.save = lambda: None` simulates the crash window instead of asserting it away; `park_path`/`expected_state_of` are genuine extractions called by the activity, not test-only branches; `park_and_alarm_in_state`'s `ctx.patched(OBSERVE_PATCH)` guard is a history-compatibility gate, not a test hook (it is, however, unwired — see the V3 HIGH). Residual weakness is coverage, not gaming: the hand-built `ExecId` in the G3-2 tests and the method-blind redirect `hits` (V7 MED/LOW).

VERDICT: fix - HIGH: OBSERVE_PATCH marker is never recorded in the delta, so the new observe step may never run and every waiting-card park falls back to the live set and is refused (V3; confirm the patch-wiring site outside this diff and add a workflow-level test) / MED: decision-time observe failure permanently sticks the cap-park on the refused fallback (V3); same-origin redirect downgrades the wake POST to a body-less GET and the 204 is counted as a successful wake (V5); the entire new workflow observe path is untested (V7) / LOW: opaque JoinError mapping and detached stalled read task, StubKeeper park CAS weaker than the store (refuses nothing on the live-set fallback), `ValueError` from `_origin` escaping the OSError contract, immortal retry on a cross-origin-redirected endpoint, redirect tests blind to the request method, wake-60s test join/bound fragility / OPEN from round 1 (Part-B-owned): V5-1 wake still not wired into any caller, V7-1/2/3/6/7 unchanged, A-V7 client→real-keeper end-to-end still absent, V5-3 crash window accepted-and-tested rather than closed / FIXED: V3-1, V3-3, V3-4, V3-5, V5-2, V5-4, V7-4, V7-5, and V3-2 in mechanism