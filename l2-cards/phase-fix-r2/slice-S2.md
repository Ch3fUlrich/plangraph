# R2a' slice S2: phase/rt-p1-bus fix, Rust exec (D-2843, replan v2)
You are the WRITER. cwd = git worktree on branch lane/phase-fix-r2a (earlier slices already committed). Edit files in place, run the checks, commit. Do NOT push.
Scope: ONLY this slice, rust/worker/** only. No .github, no Python, no site IP. <= 150 changed lines (generated fixtures excluded). Edit only the needed hunks; never print whole files.
Order: (1) write the red test named below and SAVE it to disk within 5 minutes; (2) run it, see it fail; (3) implement; (4) checks; (5) one commit.
Checks: cd rust/worker && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test (whole crate) with a Temporal dev server running and PG_TEMPORAL_ADDR=http://127.0.0.1:17233 exported (start `temporal server start-dev --headless --port 17233` if none listens) so e2e tests run, not skip.
Commit trailers exactly: 'Plan-Item: plangraph:RT-P1-BUS' and 'Writer: mimo/mimo-v2.6-pro <env PLANGRAPH_RUN_ID or plangraph-spawner>'.
Final report: the exact test fn names you added (build-L1-bus greps them; a name that does not exist rejects the run), the invariant(s) they prove, and the check tails. Then stop.

## This slice (from replan v2)
| S2 | D5: rename `card_state_agent` -> `read_agent`; add `HttpKeeper::with_read_deadlines(connect, read)`; new() keeps 2 s / 5 s | keeper.rs | `http_card_state_read_deadline_is_enforced` (accepting-but-silent TcpListener, 200 ms -> Err within 1 s) | ~50 |

## Design item(s) (authoritative). Implement ONLY the parts the slice row above names; the other parts belong to later slices, do not start them.
D5. Rename `card_state_agent` -> `read_agent`; the doc says it is used only by card_state.
  - Add `HttpKeeper::with_read_deadlines(connect, read)`; `new()` keeps 2 s / 5 s.
  - Real test: a TcpListener that accepts and never answers. card_state with a 200 ms read deadline returns Err within 1 s.
  - Real test: a non-routable connect is refused within the connect deadline.

## Invariant(s) this slice proves
| # | Invariant | Input/state | Expected |
|---|---|---|---|
| I5' | card_state deadlines are real | accepting-but-silent socket | Err within 1 s at a 200 ms deadline |

## SDK API notes (pre-researched by build-L1-bus; temporalio-* 1.1.0 in ~/.cargo/registry; do not re-research)
- Typed non-retryable activity failure (temporalio-common-wasm src/error.rs:250-295, test at ~1448):
  `ApplicationFailure::builder(anyhow::anyhow!(msg)).type_name("exec_stale_state".to_owned()).non_retryable(true).build()`; wrap it as the activity code already does with `ActivityError::application(..)`. Accessor: `ApplicationFailure::type_name() -> Option<&str>`.
- Workflow side: `execute_activity(..).await` errors carry `ActivityExecutionError::Failed(ActivityFailureError)` (error.rs:1007, 839). `ActivityFailureError` holds the `Failure` proto; the activity's ApplicationFailure is its cause: `failure.cause` -> `failure_info == FailureInfo::ApplicationFailureInfo(info)` -> `info.r#type == "exec_stale_state"`. Write one helper `fn is_stale_state_refusal(e) -> bool` that walks the cause chain, with a unit test.
- Retryable activity failure (D1 observe): `ApplicationFailure::new(err)` (retryable by default); the existing `activity_error(..)` helper already produces a retryable one.
