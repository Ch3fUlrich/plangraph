# LE-OG-MEMORY-HTTP-TEST-TIMES-OUT-ON-A-LOADED-HOST

Outcome. The og-memory HTTP tests give a short client timeout only to the cases that test a timeout, so a loaded host cannot fail a success or status case.

- plan_source. Keeper item plangraph:LE-OG-MEMORY-HTTP-TEST-TIMES-OUT-ON-A-LOADED-HOST, 2026-11-27 17:00Z, priority 10.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 6157dc5ae7597aea34d2bb5a45c73dc579bf7febc1e5231353a5da17053bd6a2
- tree SHA-256. 3745b180dfec390987a9afe631541fa938cd664772bd37f0a9ec013dab1466b6
- evidence_after_deploy. No deployment (test-only). Over 7 days, record that fast-check logs on coding.vm, dell and the workstation show no `og-memory/test` failure of `http::tests::*`.

## Checked against base

- `exercise()` builds every client with `Http::new(.., Duration::from_millis(100))` (`rust/og-memory/src/http_tests.rs:40`). `Http::new` sets this as ureq's `timeout_global`, the bound of the whole call (`rust/og-memory/src/http.rs:63-84`).
- The fixture server reads the request headers one byte per `read_exact` (`http_tests.rs:51-58`). Under load the client's 100 ms elapse before the response, and the success case `query_sends_typed_params_and_bearer_header` returns `Error::Unknown` (ws failure at `http_tests.rs:152`).
- Only `timeout_is_unknown_without_retry` needs the short timeout: its server sleeps 200 ms before replying (`http_tests.rs:84-86,130-135`). The other `exercise` callers are success, status, malformed and lost-response cases.
- The no-retry watch sleeps 150 ms after the reply, "beyond the client's entire timeout budget" (`http_tests.rs:94-101`). With a long client timeout, that comment no longer holds for the lost and malformed cases.
- `access_checks_health_and_authenticated_project_hub` uses one 300 ms client (`http_tests.rs:293`) for success rows and for the two timeout rows (server sleeps 450 ms). Its header reader `access_headers` also reads byte by byte (`http_tests.rs:351-361`). This is the same class as the item.
- Class search over `rust/` for sub-second client timeouts: `rust/pg-spawnd/tests/review_diff_split.rs:277` is a timeout-only case. `rust/worker/tests/support/harness_server_regressions.rs:102` (`cold_start_has_independent_run_budget`) pairs a 100 ms run timeout with a success assertion. That is a different crate and harness, so it goes to its own item (see Open points).

## Decisions

- D1. `exercise(status, body, delay: Duration, timeout: Duration, write: bool)`. `delay` is the server's sleep before the reply (`Duration::ZERO` for none) and `timeout` is the client's. Add two constants: `TIMEOUT_CASE = 100 ms` for the timeout case only, and `PATIENT = 10 s` for every other case. 10 s is 33 times the 300 ms delay that X1 injects and 100 times the timeout that failed on the loaded workstation, yet it still bounds a hung test.
- D2. The fixture server wraps the accepted stream in one `BufReader`. It reads header lines with `read_until(b'\n')` until the blank line, then reads the body from the same reader. The access test's `access_headers` does the same and returns the reader with the body bytes still buffered.
- D3. The no-retry check runs after the client call returns, on a listener clone (`listener.try_clone()` before the server thread takes it): set it nonblocking and assert `accept()` is `WouldBlock`. The client call is synchronous, so any retry has connected before it returns. The server thread no longer sleeps 150 ms.
- D4. In the access test, rows expecting `HealthTimeout` or `ReadTimeout` keep the 300 ms client and the 450 ms server sleep. All other rows use `PATIENT`. Select this with `matches!` on the expected value, not a new tuple column.

## Entry points

- E1. `rust/og-memory/src/http_tests.rs::exercise` and its callers.
- E2. `rust/og-memory/src/http_tests.rs::access_checks_health_and_authenticated_project_hub`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A query success case passes with a 300 ms server delay before the response. | X1 |
| B2 | E1 | The timeout case (200 ms delay, 100 ms client) still returns `Error::Unknown`, and no second connection arrives. | X2 |
| B3 | E1 | Conflict, malformed, lost-response and non-success status cases keep their results with the patient client. | X3 |
| B4 | E2 | The access rows keep their expected results, with success rows on the patient client. | X4 |

## Verification

Write X1 first. On the base it fails with `Error::Unknown` (300 ms delay against the 100 ms client).

- X1. `http::tests::success_survives_a_slow_server`: `exercise("200 OK", r#"{"graphCommitId":"before","results":[]}"#, 300 ms, PATIENT, false)` is `Ok`.
- X2. `http::tests::timeout_is_unknown_without_retry`, changed to pass `200 ms, TIMEOUT_CASE`.
- X3. The other existing `exercise` tests, changed to `Duration::ZERO, PATIENT`, with unchanged assertions.
- X4. `http::tests::access_checks_health_and_authenticated_project_hub`, changed per D2 and D4, with unchanged expected values.
- X5. Held out: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p og-memory`.

## Scope and reuse

- R1. One file: `rust/og-memory/src/http_tests.rs` (365 lines; at most 60 changed lines, at most 20 lines net growth).
- N1. No production change to `http.rs`. Test-only: commit with `Changelog: skip (test-only fix)`, no fragment.

## Open points for design approval

None for this item. File the class-search hit `rust/worker/tests/support/harness_server_regressions.rs::cold_start_has_independent_run_budget` (100 ms run timeout with a success assertion) as its own LE item. That is planner work, not writer work.
