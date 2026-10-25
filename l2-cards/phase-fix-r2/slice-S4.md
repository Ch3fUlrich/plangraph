# R2a' slice S4: phase/rt-p1-bus fix, Rust exec (D-2843, replan v2)
You are the WRITER. cwd = git worktree on branch lane/phase-fix-r2a (earlier slices already committed). Edit files in place, run the checks, commit. Do NOT push.
Scope: ONLY this slice, rust/worker/** only. No .github, no Python, no site IP. <= 150 changed lines (generated fixtures excluded). Edit only the needed hunks; never print whole files.
Order: (1) write the red test named below and SAVE it to disk within 5 minutes; (2) run it, see it fail; (3) implement; (4) checks; (5) one commit.
Checks: cd rust/worker && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test (whole crate) with a Temporal dev server running and PG_TEMPORAL_ADDR=http://127.0.0.1:17233 exported (start `temporal server start-dev --headless --port 17233` if none listens) so e2e tests run, not skip.
Commit trailers exactly: 'Plan-Item: plangraph:RT-P1-BUS' and 'Writer: mimo/mimo-v2.6-pro <env PLANGRAPH_RUN_ID or plangraph-spawner>'.
Final report: the exact test fn names you added (build-L1-bus greps them; a name that does not exist rejects the run), the invariant(s) they prove, and the check tails. Then stop.

## This slice (from replan v2)
| S4 | D2+D3+D4: typed `exec_stale_state` non-retryable failure on park 409; `is_stale_state_refusal` cause-chain helper; replace R2a's untyped loop with a PARK_REOBSERVE_PATCH-gated loop (stale only, max 2, cause in the final error, logs each retry); const-name test | activities.rs, workflow.rs | e2e `stale_state_refusal_reobserves_at_most_twice` (I3b: land on re-observe 1; 409 forever -> error after 2, message has the cause) | ~140 |

## Design item(s) (authoritative). Implement ONLY the parts the slice row above names; the other parts belong to later slices, do not start them.
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

## Invariant(s) this slice proves
| # | Invariant | Input/state | Expected |
|---|---|---|---|
| I3b | A stale-state refusal re-observes, bounded | first park 409 exec_stale_state, then observe "waiting" | lands on re-observe 1; 409 forever -> error after exactly 2 re-observes, with the cause in the message |

## SDK API notes (pre-researched by build-L1-bus; temporalio-* 1.1.0 in ~/.cargo/registry; do not re-research)
- Typed non-retryable activity failure (temporalio-common-wasm src/error.rs:250-295, test at ~1448):
  `ApplicationFailure::builder(anyhow::anyhow!(msg)).type_name("exec_stale_state".to_owned()).non_retryable(true).build()`; wrap it as the activity code already does with `ActivityError::application(..)`. Accessor: `ApplicationFailure::type_name() -> Option<&str>`.
- Workflow side: `execute_activity(..).await` errors carry `ActivityExecutionError::Failed(ActivityFailureError)` (error.rs:1007, 839). `ActivityFailureError` holds the `Failure` proto; the activity's ApplicationFailure is its cause: `failure.cause` -> `failure_info == FailureInfo::ApplicationFailureInfo(info)` -> `info.r#type == "exec_stale_state"`. Write one helper `fn is_stale_state_refusal(e) -> bool` that walks the cause chain, with a unit test.
- Retryable activity failure (D1 observe): `ApplicationFailure::new(err)` (retryable by default); the existing `activity_error(..)` helper already produces a retryable one.
