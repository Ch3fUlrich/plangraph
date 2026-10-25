# R2a' slice S5: phase/rt-p1-bus fix, Rust exec (D-2843, replan v2)
You are the WRITER. cwd = git worktree on branch lane/phase-fix-r2a (earlier slices already committed). Edit files in place, run the checks, commit. Do NOT push.
Scope: ONLY this slice, rust/worker/** only. No .github, no Python, no site IP. <= 150 changed lines (generated fixtures excluded). Edit only the needed hunks; never print whole files.
Order: (1) write the red test named below and SAVE it to disk within 5 minutes; (2) run it, see it fail; (3) implement; (4) checks; (5) one commit.
Checks (run them in the FOREGROUND, never as a background shell; commit before you finish your turn): cd rust/worker && cargo fmt --check && cargo clippy --all-targets -- -D warnings && PG_JAIL=off PG_TEST_ALLOW_UNJAILED=1 PG_TEMPORAL_ADDR=http://127.0.0.1:17233 cargo test (whole crate, ~2 min) with a Temporal dev server running (start `temporal server start-dev --headless --port 17233` if none listens) so e2e tests run, not skip.
Commit trailers exactly: 'Plan-Item: plangraph:RT-P1-BUS' and 'Writer: mimo/mimo-v2.6-pro <env PLANGRAPH_RUN_ID or plangraph-spawner>'.
Final report: the exact test fn names you added (build-L1-bus greps them; a name that does not exist rejects the run), the invariant(s) they prove, and the check tails. Then stop.

## This slice (from replan v2)
| S5 | D6 + I3c: env_or_skip panics under CI without PG_TEMPORAL_ADDR; StubKeeper ordered verb log `calls()`; I1 asserts card_state before park and the park's own expected_state | tests/e2e.rs, keeper.rs (stub) | e2e `non_stale_park_errors_do_not_reobserve` (I3c: 403/422/transport -> 0 extra card_state). I1 re-asserted in the existing fresh_run test | ~100 |

## Design item(s) (authoritative). Implement ONLY the parts the slice row above names; the other parts belong to later slices, do not start them.
D6. e2e honesty.
  - `env_or_skip` panics when env `CI` is set and PG_TEMPORAL_ADDR is missing (a skip in CI = a failure).
  - StubKeeper gets an ordered verb log, `calls() -> Vec<&'static str>` (card_state, park, alarm, ...).
  - I1/I3 assert the order `card_state` before `park`, and that the park's exec.expected_state == ["waiting"] (from the park entry, not the first exec that has Some).

## Invariant(s) this slice proves
| # | Invariant | Input/state | Expected |
|---|---|---|---|
| I1 | A fresh run that parks records OBSERVE_PATCH and calls card_state BEFORE park | new workflow, usage_limit -> waiting | verb log [.., card_state, park]; the park's exec.expected_state == ["waiting"] |
| I3c | No re-observe on other errors | park 403 / 422 / transport | returned at once, 0 extra card_state calls |
| I6' | e2e never silently skips in CI | CI=1, no PG_TEMPORAL_ADDR | panic |

## SDK API notes (pre-researched by build-L1-bus; temporalio-* 1.1.0 in ~/.cargo/registry; do not re-research)
- Typed non-retryable activity failure (temporalio-common-wasm src/error.rs:250-295, test at ~1448):
  `ApplicationFailure::builder(anyhow::anyhow!(msg)).type_name("exec_stale_state".to_owned()).non_retryable(true).build()`; wrap it as the activity code already does with `ActivityError::application(..)`. Accessor: `ApplicationFailure::type_name() -> Option<&str>`.
- Workflow side: `execute_activity(..).await` errors carry `ActivityExecutionError::Failed(ActivityFailureError)` (error.rs:1007, 839). `ActivityFailureError` holds the `Failure` proto; the activity's ApplicationFailure is its cause: `failure.cause` -> `failure_info == FailureInfo::ApplicationFailureInfo(info)` -> `info.r#type == "exec_stale_state"`. Write one helper `fn is_stale_state_refusal(e) -> bool` that walks the cause chain, with a unit test.
- Retryable activity failure (D1 observe): `ApplicationFailure::new(err)` (retryable by default); the existing `activity_error(..)` helper already produces a retryable one.
