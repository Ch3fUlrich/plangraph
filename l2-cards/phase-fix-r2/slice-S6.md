# R2a' slice S6: phase/rt-p1-bus fix, Rust exec (D-2843, replan v2)
You are the WRITER. cwd = git worktree on branch lane/phase-fix-r2a (earlier slices already committed). Edit files in place, run the checks, commit. Do NOT push.
Scope: ONLY this slice, rust/worker/** only. No .github, no Python, no site IP. <= 150 changed lines (generated fixtures excluded). Edit only the needed hunks; never print whole files.
Order: (1) write the red test named below and SAVE it to disk within 5 minutes; (2) run it, see it fail; (3) implement; (4) checks; (5) one commit.
Checks (run them in the FOREGROUND, never as a background shell; commit before you finish your turn): cd rust/worker && cargo fmt --check && cargo clippy --all-targets -- -D warnings && PG_JAIL=off PG_TEST_ALLOW_UNJAILED=1 PG_TEMPORAL_ADDR=http://127.0.0.1:17233 cargo test (whole crate, ~2 min) with a Temporal dev server running (start `temporal server start-dev --headless --port 17233` if none listens) so e2e tests run, not skip.
Commit trailers exactly: 'Plan-Item: plangraph:RT-P1-BUS' and 'Writer: mimo/mimo-v2.6-pro <env PLANGRAPH_RUN_ID or plangraph-spawner>'.
Final report: the exact test fn names you added (build-L1-bus greps them; a name that does not exist rejects the run), the invariant(s) they prove, and the check tails. Then stop.

## This slice (from replan v2)
| S6 | D7: replay fixtures. (a) a pre-OBSERVE parking history: NONE of the 11 committed fixtures contains a park (L1 grep), so record one at ea21b539 via e2e PG_RECORD_DIR; (b) post-OBSERVE pre-REOBSERVE: recorded 59c1287e history from run 2, build-L1-bus-seats/r2a-prime-run2-partial/observe_park.json (scrub-checked: identity e2e-worker only); (c) replace the duplicate old_history_... test | tests/replay.rs, tests/fixtures/*.json | `replays_pre_observe_cap_park` + `replays_pre_reobserve_cap_park` (assert the markers are absent and the park activity is present, then replay clean) | ~60 code + fixtures |

## Design item(s) (authoritative). Implement ONLY the parts the slice row above names; the other parts belong to later slices, do not start them.
D7. I2 proof: replay a committed pre-marker history that PARKS.
  - Use the usage_limit history (replays_usage_limit_reroute fixture) if it holds a park, else record one at ea21b539.
  - The test asserts the fixture has no `park-observe-v1` / `park-reobserve-v1` marker and does contain the park activity, then replays it clean.
  - Also replay one post-OBSERVE, pre-REOBSERVE history (recorded at 59c1287e) -> clean.


## Invariant(s) this slice proves
| # | Invariant | Input/state | Expected |
|---|---|---|---|
| I2 | Pre-marker histories replay unchanged | pre-OBSERVE parking history; pre-REOBSERVE history | replay clean; fixtures asserted marker-free |

## SDK API notes (pre-researched by build-L1-bus; temporalio-* 1.1.0 in ~/.cargo/registry; do not re-research)
- Typed non-retryable activity failure (temporalio-common-wasm src/error.rs:250-295, test at ~1448):
  `ApplicationFailure::builder(anyhow::anyhow!(msg)).type_name("exec_stale_state".to_owned()).non_retryable(true).build()`; wrap it as the activity code already does with `ActivityError::application(..)`. Accessor: `ApplicationFailure::type_name() -> Option<&str>`.
- Workflow side: `execute_activity(..).await` errors carry `ActivityExecutionError::Failed(ActivityFailureError)` (error.rs:1007, 839). `ActivityFailureError` holds the `Failure` proto; the activity's ApplicationFailure is its cause: `failure.cause` -> `failure_info == FailureInfo::ApplicationFailureInfo(info)` -> `info.r#type == "exec_stale_state"`. Write one helper `fn is_stale_state_refusal(e) -> bool` that walks the cause chain, with a unit test.
- Retryable activity failure (D1 observe): `ApplicationFailure::new(err)` (retryable by default); the existing `activity_error(..)` helper already produces a retryable one.
