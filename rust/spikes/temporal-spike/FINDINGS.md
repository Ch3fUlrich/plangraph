# Temporal Rust SDK spike: findings

Run date: 2026-10-15. Goal: can a Rust worker run long agent attempts as Temporal
activities (heartbeats, retries, signals, durable timers, versioning, replay tests)?
Fallback: an own runner on Postgres `SKIP LOCKED` behind the same seam.

## Versions

| Item | Version |
|---|---|
| `temporalio-sdk`, `-client`, `-common`, `-macros`, `-workflow` | 1.1.0 (pinned with `=`) |
| `temporalio-sdk-core` (transitive) | 0.10.0 |
| Temporal CLI / dev server | CLI 1.9.1 (server 1.32.0), sha256 verified against release `checksums.txt` |
| rustc | 1.97 (crate MSRV 1.92) |
| protoc | 36.2 (build-time requirement, see gaps) |

Status: the Rust SDK was announced generally available on 2026-09-23 (Temporal blog);
crates are 1.x on crates.io, repo `temporalio/sdk-rust`. The 0.6 README on docs.rs
still says "public preview"; that text is stale.

## Demonstrated against the dev server (in-memory DB); all PASS

| Need | Result |
|---|---|
| Activity `attempt` heartbeats and returns a value | PASS |
| Failing first attempt, retry policy, attempt 2 succeeds | PASS |
| Heartbeat timeout detected (activity goes silent) | PASS: history has `TIMEOUT_TYPE_HEARTBEAT`, retry succeeds; with one attempt the workflow sees `ActivityExecutionError::Failed` ("Activity task timed out") |
| Signal `answer` raced against durable timer, signal wins | PASS (signal after 3 s resumes with the value) |
| Timer expiry takes the default answer | PASS (5 s timer, 6.0 s observed incl. activity) |
| Cancel via signal (during activity and during wait) | PASS: signal fires a child `WorkflowCancellationToken` handed to the activity |
| Native workflow cancellation | PASS: activity cancelled, workflow ends Cancelled |
| 24 h timer survives a worker restart | PASS: `startToFireTimeout 86400s`, worker killed and restarted, signal completes it |
| Worker versioning | PASS: deployment with build ids v1/v2/v3, `use_worker_versioning` + `Pinned`; after `set-current-version v2` the old run stayed pinned to v1 and finished on the v1 worker, a new run went to v2 |
| Offline replay in `cargo test` | PASS: two recorded histories replay with no server; a third test proves drifted code (extra timer) FAILS replay |

Heartbeat 30 s / timeout 2 min / start-to-close 90 min are plain `ActivityOptions`;
the spike used scaled-down values (250 ms / 2 s / 60 s).

## API shape

- `#[workflow]`, `#[workflow_methods]` (`#[run]`, `#[signal]`, `#[query]`, `#[update]`),
  `#[activities]`/`#[activity]`; serde types as JSON payloads.
- `temporalio_sdk::workflows::select!` races `wait_condition`, `timer`, `execute_activity`.
- `WorkflowReplayer` + `WorkflowHistory::from_json`; `fetch_history(..).to_json()` records fixtures.
- Typed client: `start_workflow(Card::run, ..)`, `handle.signal(Card::answer, ..)`, `handle.cancel(..)`.

## Gaps and sharp edges

1. protoc is required to compile (`prost-wkt-types` build script): CI and dev images need
   `protobuf-compiler` or `PROTOC=`.
2. Macro hygiene: `#[activities]` expands to `futures::..` and `#[workflow_methods]` to
   `temporalio_workflow::..`; both must be direct dependencies or the build fails opaquely.
3. Cancel latency (reasoned from SDK source, NOT measured at production values): activity
   cancellation reaches a running activity only in a heartbeat response, and the core
   throttles heartbeats to `min(0.8 * heartbeat_timeout, 60 s)` (`max_heartbeat_throttle_interval`
   default 60 s). With a 2 min timeout an operator cancel may take up to ~60 s to reach the
   attempt, while the workflow side resolves at once (default `TryCancel`). Lower the
   throttle or add a second channel if that is too slow.
4. A heartbeat-timed-out attempt is failed by the server, but the worker task is not killed:
   activities must watch `ctx.cancelled()` and clean up child processes themselves. Whether
   the worker is told about the timeout was not verified.
5. Determinism is the author's job: the nondeterminism detector cannot catch everything
   (`SystemTime::now()`, `rand`, I/O in workflow code). CI replay is mandatory.
6. `start_workflow` returns a handle typed to a private generated `Run` type; use
   `client.get_workflow_handle::<Card>(id)` for reusable helpers.
7. Versioned workers only receive tasks once their version is current or pinned-to: an
   unversioned worker hung my demo after v2 became current. Rollout needs CLI/API steps
   (`worker deployment set-current-version`).
8. Some README snippets use stale option names; the crate `examples/` dir is accurate.
9. Dependency weight: 322 unique crates, `Cargo.lock` ~2.9k lines.

## Measurements (6 cores, 16 GB host shared with other sessions)

| Metric | Value |
|---|---|
| Dev server (headless, in-memory) RSS idle | 149 MB |
| Dev server RSS after demo / peak (VmHWM) | 171 MB / 211 MB |
| Worker RSS, release, idle / peak under demo | 15.8 MB / 18.7 MB |
| Worker RSS, debug, after demo | 40 MB |
| Clean compile, debug, `CARGO_BUILD_JOBS=4` | ~75 s |
| Clean compile, release (thin LTO), 4 jobs | 2 min 20 s |
| Binary size, release (unstripped) | worker 30 MB, driver 21 MB |
| `free -m` used before / after (whole host) | 9319 / 9964 MB; MemAvailable stayed >= 4.9 GB |
| Replay tests | ~0.5 s for 3 tests, no server |

The CLI dev server binary is 156 MB on disk. A production Temporal server and its
persistence were not measured here.

## Recommendation: adopt with workarounds

The SDK covers every requirement tested: heartbeats with server-side timeout detection,
retries, signals, signal-vs-timer races with durable timers up to 24 h, cancellation,
deployment versioning with pinned old runs, and offline replay for CI. The worker is small.

Workarounds to carry into the real worker:
- Install protoc in CI and dev images; add `futures` and `temporalio-workflow` dependencies.
- Make every activity cancel-aware and responsible for killing its child processes; budget
  for up to ~60 s cancel latency (reasoned, not measured) or lower the heartbeat throttle.
- Keep recorded histories in CI (`tests/fixtures`) and replay on every change; use `Pinned`
  versioning so long attempts finish on the build that started them.
- The real cost is operating a Temporal server plus persistence versus the Postgres
  `SKIP LOCKED` fallback; that trade-off is outside this spike. The fallback stays viable
  behind the same activity/workflow seam.

## Reproduce

```
# protoc on PATH (or PROTOC=...)
temporal server start-dev --ip 127.0.0.1 --port 17233 --headless
SPIKE_ADDR=http://127.0.0.1:17233 SPIKE_DEPLOYMENT=d SPIKE_BUILD_ID=v1 cargo run --bin worker
temporal worker deployment set-current-version --deployment-name d --build-id v1
SPIKE_ADDR=http://127.0.0.1:17233 cargo run --bin driver -- all tests/fixtures
cargo test --test replay      # offline, no server
```
