# LE-RUN-WAITING-FOR-THE-CARGO-LEASE-ENDS-STALLED-NO-LEG

Outcome. A spawned run whose launch finds the shared Cargo lease held records `queued_on` with the lock path and holder PID, keeps waiting up to a queue bound, starts when the lease frees and is never ended `stalled_no_leg` while it waits.

- plan_source. Keeper item plangraph:LE-RUN-WAITING-FOR-THE-CARGO-LEASE-ENDS-STALLED-NO-LEG, 2026-11-27 13:46Z, priority 10.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 170ed4b31e0eef2531ba4e1185b1be93aa48e692455d7a6b2d1c3807af89fbbc
- tree SHA-256. cadba311f146bd371a3f3e61031160bd4101a48a582311fd46a39ff5fd5fe552
- evidence_after_deploy. On coding.vm, start one implement spawn while a `plangraph fast-checks --owned-worktree` run holds the lease. Record its `queued_on` (lock path, holder PID equal to the fast-check PID), its later `dispatch_started`, its first attempt, and the absence of a `stalled_no_leg` record. Then record 24 hours of `runs/*/record.json` on coding.vm with no `stalled_no_leg` whose `queued_on` was set. Rule 3.112 (1) applies: one host first.

## Checked against base

- Since #1329 (443bf493a, GO D-7078), the spawner runner takes the shared lease through `probe_cleanup.acquire_cargo_lease` for every launch without a review target. The wait is bounded to `SHARED_WAIT_S = 5` seconds, and a timeout raises `CargoLeaseTimeout` (`cargo_lease_timeout`). `stop_on_probe_fault` and `fail_local_job` then end the attempt with local evidence. So the 13:29Z/13:34Z behaviour, a supervisor blocked in `ep_poll` for 600 s and then `stalled_no_leg`, no longer happens. The run now fails after 5 s with `cargo_lease_timeout`, so a writer still cannot start while a fast check holds the lease. [src/plangraph/spawner/runner.py:478–504,1019–1023; src/plangraph/spawner/probe_cleanup.py:10–11,74–112,162–175; src/plangraph/spawner/exit_outcome.py:68–74; src/plangraph/spawner/host_ready.py:180–183,227–250]
- Lease timeout evidence (`LeaseWait.evidence`) carries the wait, the budget, the lock path and the lock identity (device and inode). It does not carry the holder PID, and `public()` drops the path and the identity. [src/plangraph/spawner/probe_cleanup.py:22–25,51–58; src/plangraph/spawner/host_ready.py:195–202]
- `no_leg_finding` ends a planned `running` record that has no `pid`, `job_id` or `dispatch_started` and only skipped attempts once `PLANGRAPH_WATCHDOG_NO_LEG_SECONDS` (600) have passed. `claim_first_dispatch` sets `dispatch_started` only after the lease is taken, and it reads the record and the save function from `prompt_record`, which the runner already has at the lease call. [src/plangraph/spawner/stall_watchdog.py:126–143; src/plangraph/spawner/service.py:32–44; src/plangraph/spawner/runner.py:186,483–487,1050]
- `runner.py` is over the 800-line limit on the baseline (1157), so it may not grow. [.filesize-baseline:53]
- NATIVE-CARGO-LEASE-PER-COMMAND-TRIM-AND-HOLDER-PID-R2 (priority 8, open) covers the native writer's whole-life lease in `rust/pg-spawnd/src/native_writer_session.rs` and holder-PID evidence for refusals. This item covers the Python spawner launch only. The holder-PID reader it adds to `LeaseWait.evidence` is the single Python implementation, which R2 reuses for probe refusals.

## Decisions

- D1. Queue, not fail. This applies to a launch with no probe ownership (`OWNED.get()` is `None`) that is given a `prompt_record`. If the first 5-second acquisition raises `CargoLeaseTimeout`, it sets `record['queued_on'] = {'lease': 'cargo', 'lock_path': <str>, 'holder_pid': <int or None>, 'since': <epoch seconds>}`, saves the record with the `prompt_record` save function, and retries in 5-second acquisitions. Each retry refreshes `holder_pid` and saves only when the holder changes. On success it removes `queued_on`, saves, and returns the lease. Dispatch then continues as today (`claim_first_dispatch` sets `dispatch_started`). Probe launches and launches without a `prompt_record` keep the 5-second bound unchanged.
- D2. Queue bound. `PLANGRAPH_CARGO_LEASE_QUEUE_S` (default 3600, finite and positive; an invalid value refuses the launch as `ValueError`). When it passes, the launch raises `CargoLeaseTimeout`, whose public evidence includes `holder_pid` and the total `waited_ms`. The existing local-fault path then ends the run `cargo_lease_timeout`, and the parent session receives that reason and evidence in its completion event. One hour covers the longest observed fast check. A run that finds the lease still held after an hour is a fault to report.
- D3. Watchdog. `no_leg_finding` also returns `None` for a record with `queued_on`. A planned run with only skipped attempts and no `queued_on` still ends `stalled_no_leg`, as today.
- D4. Holder PID. `holder_pid(lock_path)` lives in `probe_cleanup.py`. On Linux it reads `/proc/locks` and returns the PID of the first lock line whose `major:minor:inode` matches `os.stat(lock_path)`. On any other platform, or when no line matches or the file cannot be read, it returns `None`. `LeaseWait.evidence` adds `holder_pid` when contention was observed, and `public()` keeps it, because a PID is not secret.
- D5. Cancellation. `claim_first_dispatch` keeps its current checks. A cancel while queued stops the retry loop through the existing `CancelledError` path and closes no lease, because none is held. `queued_on` stays on the record with its terminal status, as evidence.

## Entry points

- E1. Spawner launch: `SpawnerRunner` launch → `probe_cleanup.acquire_cargo_lease(target, prompt_record)` (runner.py:483, the one call site; the edit changes its arguments in place).
- E2. Stall watchdog sweep: `stall_watchdog.no_leg_finding`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | With the lease held exclusively by another open file, a launch given a `prompt_record` saves `queued_on` with the lock path and `holder_pid == os.getpid()`. When the holder releases, it returns a lease and saves the record without `queued_on`. | X1 |
| B2 | E1 | With the queue bound set to 0.3 s and the lease never released, the launch raises `CargoLeaseTimeout`, whose public evidence has `holder_pid` and `waited_ms >= 300`. A probe launch (`OWNED` set) still raises after its 5-second budget with no `queued_on`. | X1 |
| B3 | E2 | A planned running record with only skipped attempts, started beyond the no-leg bound and carrying `queued_on`, gets no finding. The same record without `queued_on` gets the `stalled_no_leg` finding. | X2 |
| S1 | E1 E2 | On coding.vm a writer spawned during a fast check starts after the check ends, and there is no `stalled_no_leg` record for 24 hours. | X3 |

## Verification

- X1. E1, changed. `tests/test_cargo_lease_queue.py::test_a_queued_launch_records_the_holder_and_starts_after_release`, `test_the_queue_bound_fails_cargo_lease_timeout_with_the_holder_pid`, `test_a_probe_launch_keeps_the_five_second_bound` and `test_holder_pid_reads_proc_locks` (skipped where `/proc/locks` is absent). Use a temporary target and the isolation fixture of `tests/test_cargo_data_drive_target.py`, hold the exclusive lease with `host_ready.cargo_lease(exclusive=True)`, and set `probe_cleanup.SHARED_WAIT_S` and `host_ready.SHARED_WAIT_S` to 0.05 as `test_async_lease_deadline_includes_executor_queueing` does. Check: `python3 -m pytest -q tests/test_cargo_lease_queue.py`.
- X2. E2, changed. `tests/test_cargo_lease_queue.py::test_a_queued_run_is_not_stalled_no_leg_and_an_unqueued_one_is`, calling `stall_watchdog.no_leg_finding(record, now)` directly. Same check command.
- X3. E1 E2, live only. The evidence_after_deploy observation.
- X4. Held out. `python3 -m pytest -q tests/test_cargo_data_drive_target.py tests/test_leg_probe_budget.py tests/test_stall_watchdog.py`, unchanged and green.

Write X1 and X2 first. B1, B2's queue half and B3 fail on the base.

## Scope and reuse

- R1. Reuse `acquire_cargo_lease`, `LeaseWait`, `CargoLeaseTimeout`, the local-fault path and the `prompt_record` save. Add no second lease reader and no new record writer.
- N1. The native writer's whole-life lease, per-command Cargo leases and trim between commands are NATIVE-CARGO-LEASE-PER-COMMAND-TRIM-AND-HOLDER-PID-R2.

### File budgets

`runner.py` stays at 1157 lines: the one call at line 483 gains its second argument in place. `probe_cleanup.py` 221 → at most 275 lines. `stall_watchdog.py` stays at 548 lines: `or record.get('queued_on')` is added to the existing skip condition at lines 129–132 without adding a line. The change is documented in the fragment `docs/spawner/LE-RUN-WAITING-FOR-THE-CARGO-LEASE-ENDS-STALLED-NO-LEG.md`.

### Implementation

- `probe_cleanup.py`: `acquire_cargo_lease(target=None, prompt_record=None)`. Keep the current body as the single-attempt helper. When `OWNED.get()` is `None` and `prompt_record` is not `None`, loop over that helper per D1 and D2 until it succeeds or the queue bound passes. Read `PLANGRAPH_CARGO_LEASE_QUEUE_S` once per call. Add `holder_pid` (D4) and extend `evidence`.
- `runner.py:483`: `self._cargo_leases[job_id] = await acquire_cargo_lease(child_env.get('CARGO_TARGET_DIR'), prompt_record)`.
- `stall_watchdog.py`: D3.

## Open points for design approval

None.
