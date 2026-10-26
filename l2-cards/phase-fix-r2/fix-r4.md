# R2a' fix R4: r4 qwen seat findings (lane/phase-fix-r2a head 27948c8f)
You are the WRITER. cwd = git worktree on branch lane/phase-fix-r2a. Edit files in place, run the checks, ONE commit. Do NOT push.
Write your first file change (the red test for item 1) within 10 minutes: all anchors are below, do not survey the crate. Edit only the needed hunks; never print whole files. <= 150 changed lines.
Scope: rust/worker/src/{keeper.rs,activities.rs}, rust/worker/tests (if needed), tests/test_keeper_oversight_scope.py (rename only), tests/test_keeper_message_envelope.py (one test), CHANGELOG.md (one phrase). Nothing else: no src/plangraph/**, no .github, no fixtures.

## Items to fix (grouped by file; seat text quoted verbatim)
### 1. rust/worker/src/keeper.rs + activities.rs (MED)
Seat: "MED activities.rs::keeper_park_error — types on the HTTP code alone; HttpKeeper discards the body, and STATUS carries 6 other 409 codes (conflict, locked, epoch_stale, lease_held, exec_stale, bad_transition). A park refused `lease_held` is labelled `exec_stale_state`, spends 2 pointless re-observes, then fails as "stale"."
Fix: on a non-2xx park response, read at most 4096 bytes of the body and parse the KEEPER refusal shape, which is FLAT: `{"error": "<code>", "msg": "..."}` (src/plangraph/keeper/api.py module docstring line 18: "Refusals are 4xx {\"error\": code, \"msg\": ...}"). The nested `{"error":{"code":..}}` form is the fleet bus wire, NOT the keeper. Add a variant `KeeperError::Conflict(String)` (status 409 + the keeper's error code) produced ONLY by the park request path; `classify` maps it to `Failure::Refused`. `keeper_park_error` types `STALE_STATE_FAILURE` only for `Conflict(code)` with code == "exec_stale_state"; any other 409 (other code, missing or unparsable body) stays non-retryable and UNtyped (no re-observe). Other verbs keep `KeeperError::Status`. `StubKeeper::set_park_error` must be able to express both.
Red tests (exact names): `park_409_with_another_code_is_not_typed_stale` (lease_held and exec_stale -> untyped, workflow does not re-observe: one park exec), `park_409_exec_stale_state_body_is_typed_stale` (HttpKeeper against a local TcpListener answering 409 + that JSON body -> Conflict("exec_stale_state") -> typed), `park_409_without_a_code_is_not_typed_stale`.
Exact anchors (no need to explore further):
- keeper.rs `HttpKeeper::park` (line ~470) calls `self.post204("/v1/park", ...)`; `post204` (line ~370) maps `Err(ureq::Error::Status(code, _))` to `KeeperError::Status(code)`. Add a park-only sibling (e.g. `post204_park`) that, for `ureq::Error::Status(409, resp)`, reads <= 4096 bytes of `resp` and returns `Conflict(code)` (code = the `error` string, or "" if missing/unparsable -> still `Conflict("")`, untyped). Leave `post204` and every other verb unchanged.
- keeper.rs `enum KeeperError` (line ~161): add `#[error("keeper refused 409: {0}")] Conflict(String)`.
- activities.rs `classify` (line ~351): `KeeperError::Conflict(_) => Failure::Refused`.
- activities.rs `keeper_park_error` (line ~544): typed only for `Conflict(c) if c == "exec_stale_state"`; `Status(409)` is now UNtyped non-retryable.
- activities.rs test `only_a_stale_park_refusal_is_typed_and_a_5xx_stays_retryable` (line ~1782) asserts `Status(409)` -> typed: UPDATE it so `Status(409)` and `Conflict("lease_held")` -> untyped, `Conflict("exec_stale_state")` -> typed; keep its 401/403/422/500 cases.
- StubKeeper `set_park_error(Some(KeeperError::Conflict("exec_stale_state".into())))` replaces `Status(409)` in the workflow/e2e tests that drive the re-observe loop (grep `Status(409)` in rust/worker; change only park-error uses).
- keeper.rs tests module (line ~984): `http_card_state_read_deadline_is_enforced` (line ~1102) shows the TcpListener pattern for the HttpKeeper 409-body test and for the connect test.
Keep green: `stale_state_refusal_reobserves_at_most_twice`, `only_a_stale_park_refusal_is_typed_and_a_5xx_stays_retryable`, `non_stale_park_errors_do_not_reobserve`, `stale_state_refusal_is_found_along_the_cause_chain`.
### 2. rust/worker/src/keeper.rs (I5' PARTLY)
Seat: "I5' PARTLY — `http_card_state_read_deadline_is_enforced` proves read only; D5's connect-deadline test was never added."
Design D5: "Real test: a non-routable connect is refused within the connect deadline."
Fix: add `http_card_state_connect_deadline_is_enforced`: `HttpKeeper::with_read_deadlines(200 ms connect, 5 s read)` at `http://192.0.2.1:9` (RFC 5737 TEST-NET, the scrub allows it; no other address), assert card_state returns Err within 2 s. Never an IP outside 127./192.0.2./198.51.100./203.0.113.
### 3. tests/test_keeper_oversight_scope.py (LOW)
Seat: "test name `..._exec_7_then_oversight_8` now asserts 1..9". Rename `test_all_pg_migrations_apply_in_order_exec_7_then_oversight_8` -> `test_all_pg_migrations_apply_in_order_exec_7_oversight_8_bus_9` and its docstring first line to "migrations 1..9 apply in order, no gaps". No assertion change.
### 4. tests/test_keeper_message_envelope.py (LOW)
Seat: "LOW api_routes vs contract.ERROR_STATUS — no parity test, so a new ERROR_STATUS code silently loses its status".
Fix: add `test_bus_error_statuses_match_keeper_status`: for every code in `plangraph.fleet_bus.contract.ERROR_STATUS` that the keeper can raise through post_message (find them: `git grep -n 'BusError(' src/plangraph`), `api_routes.STATUS[code] == ERROR_STATUS[code]`; name in a comment any ERROR_STATUS code you exclude and why.
### 5. CHANGELOG.md (LOW)
Seat: "the EXEC-OWNERSHIP bullet still calls card_state "best-effort" (false after D1a)". In the `RT-P1-EXEC-OWNERSHIP EXEC-OBSERVED-STATE` bullet replace "(best-effort; unreadable/`claimed`/terminal answers send no observation)" with "(a read failure is an error, retried, never \"no observation\"; `claimed`/terminal answers send no observation)".

## NOT to change (L1 checked; recorded for the re-review)
- "MED keeper.rs::card_state — a 2xx with an empty body ... is now Err(Decode)": the keeper's `show` is a read verb (api.py `_read`, name == "show"); an unknown key raises not_found (404); 204 is sent only for worker write verbs (api.py `if payload is None: # worker verbs: 204`). An empty 2xx from show is a protocol violation; Decode is correct per D1a. No change.
- "MED 91e47fec unscoped scrub allowances": allowances are keyed (path, rule) — tools/split/scrub_check.py `_allowed_shas(rel, rule, cfg)` -> `cfg["_allow_value"].get((rel, rule))`. No change.
- eprintln! in workflow.rs: the file's established pattern (lines ~217-1013 predate this lane); no replay-aware logger in this SDK. No change.
- replays_check_fails / replays_pre_observe_cap_park share check_fails: (a) now asserts marker-free + park present, which is the gap NEW-6 named. No change.

## Invariants (all must stay HOLDS; the tests named in the slice table must still exist and pass)
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

## Checks (foreground; then one commit)
- cd rust/worker && cargo fmt --check && cargo clippy --all-targets -- -D warnings && PG_JAIL=off PG_TEST_ALLOW_UNJAILED=1 PG_TEMPORAL_ADDR=http://127.0.0.1:17233 cargo test (whole crate; a Temporal dev server listens on 17233; if not, start `temporal server start-dev --headless --port 17233`)
- python3 -m pytest -q -p no:cacheprovider -n 4 (whole suite) -> 0 failed
- python3 tools/split/scrub_check.py rust/worker/src --rules tools/split/manifest.toml --rules tools/split/scrub_ci.toml -> 0 hits
Commit trailers exactly: `Plan-Item: plangraph:RT-P1-BUS`, `Writer: mimo/mimo-v2.6-pro <env PLANGRAPH_RUN_ID or plangraph-spawner>`.
Final report: per item 1-5 FIXED <exact test name> | WONTFIX reason; the check tails. Then stop.
