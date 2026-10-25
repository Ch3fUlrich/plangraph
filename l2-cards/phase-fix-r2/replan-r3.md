# REPLAN phase/rt-p1-bus fix r2 after r3 (D-2820, D-2739) · build-L1-bus gen-3 (Opus) · 2026-10-25
State: R2a = lane/phase-fix-r2a @59c1287e (agy gemini-3.8-flash run-d41eddf4, D-2820 exception), CI 40215641236 green, NOT merged.
r3 qwen seat run-ea8a46e1: build-L1-bus-seats/phase-r3-R2a-59c1287e/ (PROMPT sha in PROMPT.sha256, review.txt sha256 b673ef05...).
VERDICT line "pass" but 2 MED + 5 LOW in the body -> NOT clean (D-2739). Spawner kept only the stdout tail: the ROUND-2/INVARIANTS sections were cut (spawner gap).

## L1 adjudication of r3 (checked against code at 59c1287e)
| id | sev | L1 check | disposition |
|---|---|---|---|
| NEW-1 re-observe loop fires on ANY park error, drops causes | MED | true: workflow.rs park_and_alarm_in_state `Err(e) => if is_fallback && retries<2` | fix (design D2, D3) |
| NEW-2 report cites `http_card_state_agent_has_connect_and_read_deadlines`; test does not exist | MED | true (grep: 0 hits). Writer overclaim | fix (D5) |
| NEW-3 `get()` routes via `card_state_agent` (name/scope) | LOW | true; only caller is card_state, so behaviour ok, name wrong | fix (D5 rename) |
| NEW-4 e2e I1/I3 proofs behind env_or_skip; `execs()` verb-blind | LOW | true; CI sets PG_TEMPORAL_ADDR (tests.yml:298), local skip prints ok | fix (D6) |
| NEW-5 stub accepts live set on running | LOW | FALSE: store_worker._exec_expect_state refuses the live set only for _AWAY_STATES; running accepts | no change; keep the test, cite store_worker.py:176 in a comment |
| NEW-6 I2 replay test duplicates replays_check_fails | LOW | true: proves nothing about a pre-marker PARK history | fix (D7) |
| NEW-7 every Refused (401/403/409/422) on park non-retryable | LOW | true, unrequested behaviour change | keep, but scoped + tested (D3) |
| own: the R2a loop adds workflow commands with no patch marker | MED | a history whose park failed terminally replays differently (new observe/park commands) -> nondeterminism on reset/query | fix (D4 patch) |

## Design (root cause: a read failure is reported as "no observation")
D1. Make the read failure visible, retry it server-side, then degrade.
  - `Keeper::card_state(&self, card) -> Result<Option<String>, KeeperError>`.
    - HTTP non-2xx -> Err(Status(code)); transport -> Err(Http); oversized/undecodable body -> Err(Decode); 2xx with a state -> Ok(Some(state)).
    - StubKeeper: the `card_state_fail_times` failures return Err(Status(500)), not None.
  - `observe_read_within` -> Result: a timeout or JoinError -> Err (transient).
  - The `observe` activity returns a RETRYABLE ActivityError on Err. Temporal's activity retry policy (activity_options: 5 attempts, 2 s initial) then retries the read inside one scheduled activity: no new workflow commands, so replay is safe.
  - Only after the retries run out does `observe_card` degrade to None/fallback. Its existing `Err(_) => Ok(None)` stays, and it logs the cause.
D2. Re-observe only on a stale-state refusal.
  - Park refusals get a typed failure: for 409, `keeper_park_error` returns a non-retryable ApplicationFailure whose type (or message prefix, if the SDK has no type field) is `exec_stale_state`.
  - On that failure, and only that one, `park_and_alarm_in_state` re-runs observe -> park. This covers both a fallback park and a park with a stale observation: the card moved, so re-observing is the fix.
  - Bound: at most 2 re-observes. Any other error returns at once, unchanged.
D3. Error causes are never dropped.
  - The final error after the bound carries the last cause plus "after N re-observes".
  - Each re-observe logs the refused cause through the workflow logger.
  - 401/403/422 stay non-retryable: they never heal, and each needs a test. A transient 5xx stays retryable.
D4. Replay safety: gate the D2 loop behind a new marker, `const PARK_REOBSERVE_PATCH: &str = "park-reobserve-v1"` (checked with ctx.patched like OBSERVE_PATCH).
  - Without the marker: the 59c1287e-pre behaviour, i.e. one park with no loop, and the error is returned.
  - Add a `REOBSERVE_PATCH` const-name test like the GATE_PATCH one at workflow.rs:2105.
D5. Rename `card_state_agent` -> `read_agent`; the doc says it is used only by card_state.
  - Add `HttpKeeper::with_read_deadlines(connect, read)`; `new()` keeps 2 s / 5 s.
  - Real test: a TcpListener that accepts and never answers. card_state with a 200 ms read deadline returns Err within 1 s.
  - Real test: a non-routable connect is refused within the connect deadline.
D6. e2e honesty.
  - `env_or_skip` panics when env `CI` is set and PG_TEMPORAL_ADDR is missing (a skip in CI = a failure).
  - StubKeeper gets an ordered verb log, `calls() -> Vec<&'static str>` (card_state, park, alarm, ...).
  - I1/I3 assert the order `card_state` before `park`, and that the park's exec.expected_state == ["waiting"] (from the park entry, not the first exec that has Some).
D7. I2 proof: replay a committed pre-marker history that PARKS.
  - Use the usage_limit history (replays_usage_limit_reroute fixture) if it holds a park, else record one at ea21b539.
  - The test asserts the fixture has no `park-observe-v1` / `park-reobserve-v1` marker and does contain the park activity, then replays it clean.
  - Also replay one post-OBSERVE, pre-REOBSERVE history (recorded at 59c1287e) -> clean.

## Invariants (each one a named test; the writer's report quotes the test names exactly; a name not found by grep = rejected)
| # | Invariant | Input/state | Expected |
|---|---|---|---|
| I1 | A fresh run that parks records OBSERVE_PATCH and calls card_state BEFORE park | new workflow, usage_limit -> waiting | verb log [.., card_state, park]; the park's exec.expected_state == ["waiting"] |
| I2 | Pre-marker histories replay unchanged | pre-OBSERVE parking history; pre-REOBSERVE history | replay clean; fixtures asserted marker-free |
| I3a | A transient observe failure is retried by the activity, not degraded | card_state Err(500) x2, card waiting | 1 observe activity, 3 card_state calls, park with ["waiting"] |
| I3b | A stale-state refusal re-observes, bounded | first park 409 exec_stale_state, then observe "waiting" | lands on re-observe 1; 409 forever -> error after exactly 2 re-observes, with the cause in the message |
| I3c | No re-observe on other errors | park 403 / 422 / transport | returned at once, 0 extra card_state calls |
| I4 | The live set is refused for park on waiting/needs_input in stub and store, and accepted on running | stub + store_worker.py:176 | already true: keep the test |
| I5' | card_state deadlines are real | accepting-but-silent socket | Err within 1 s at a 200 ms deadline |
| I6' | e2e never silently skips in CI | CI=1, no PG_TEMPORAL_ADDR | panic |
Budget: <= 400 changed lines on top of 59c1287e (estimate ~260: keeper.rs ~90, activities.rs ~40, workflow.rs ~50, tests ~80).
Checks: cargo fmt --check, clippy -D warnings, cargo test for the whole crate WITH PG_TEMPORAL_ADDR set (start `temporal server start-dev` as tests.yml does), plus pytest tests/test_store_worker*.py on SQLite (I4 store half).

## Exact fix brief R2a' (spawn from worktree bus-fix-r2a, branch lane/phase-fix-r2a, base 59c1287e)
Prompt = this file's "Design" + "Invariants" sections + the r3 review.txt VERBATIM + the r2 seat-B Rust lines (r2a-brief.md "Rust exec" + "Seat B review").
Header rules: you are the WRITER; edit only rust/worker/**; no .github, no Python store, no site IP; red test first for D1-D7; one commit, trailers `Plan-Item: plangraph:RT-P1-BUS` + `Writer: <model> <run id from env PLANGRAPH_RUN_ID or "plangraph-spawner">`.
The final report maps each r3 id and each invariant to FIXED <exact test name> | WONTFIX reason, and pastes the check tails. Do not claim tests that you did not add.
Writer: non-flash per D-2739. Candidates: mimo/mimo-v2.6-pro (D-2820 bars further mimo for R2a: routing must lift it) or cx-gpt-6.1-sol (quota). ROUTING DECIDES. 1 run, gate in the spawn command (gate.sh, D-2823).
r4 seat: 1 qwen (qodercli, --no-fallthrough; writer families gemini+mimo+gpt excluded), delta 59c1287e..head, prompt < 128 KiB (spawner passes argv to qodercli -p).

## R2b (wake + docs) unchanged in scope; starts only after R2a' is clean
Scope = brief.md "## Wake" (R2-4, R2-5, LOWs) + "## Docs gap" + amend-d2739 I5-I9 + r2 seat-B V5/V7-wake lines verbatim. Base = merged R2a'.
Writer mimo/mimo-v2.6-pro, 1 run, gate in the spawn command (D-2820). Seat: 1 qwen. Same report-honesty rule (test names grep-checked by L1 before the seat).
Lesson for routing LESSONS: L1 grep-checks every test the writer cites before spawning the seat (r3 NEW-2 overclaim would have been caught for free); spawner review result must keep the full stdout, not the tail.
