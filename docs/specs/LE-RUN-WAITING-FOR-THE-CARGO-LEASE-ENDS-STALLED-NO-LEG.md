# LE-RUN-WAITING-FOR-THE-CARGO-LEASE-ENDS-STALLED-NO-LEG

Outcome. A spawned run whose plain launch finds the shared Cargo lease held by another holder records `queued_on` with the lock path, holder PID, deadline and owner, waits up to a queue bound, starts when the lease frees, honours cancels from other processes, and is never ended `stalled_no_leg` while its wait is live.

- plan_source. Keeper item plangraph:LE-RUN-WAITING-FOR-THE-CARGO-LEASE-ENDS-STALLED-NO-LEG, 2026-11-27 13:46Z, priority 10.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 291a9f6fbe9065c352ebd35f8d28c466a00cabd33c68959b1d33f1d83b4cb6dd
- tree SHA-256. 3ce65e612a5e9711010a28923f62213ae3c8e9c4ca622360c15432286ff6239d
- evidence_after_deploy. On coding.vm, start one implement spawn while a `plangraph fast-checks --owned-worktree` run holds the lease. Record its `queued_on` (lock path, holder PID equal to the fast-check PID), its later `dispatch_started`, its first attempt, and the absence of a `stalled_no_leg` record. Then record 24 hours of `runs/*/record.json` on coding.vm with no `stalled_no_leg` whose `queued_on` was set. Rule 3.112 (1) applies: one host first.

## Checked against base

- Since #1329 (443bf493a, GO D-7078), the spawner runner takes the shared lease through `probe_cleanup.acquire_cargo_lease` for every launch without a review target. The wait is bounded to `SHARED_WAIT_S = 5` seconds, and a timeout raises `CargoLeaseTimeout` (`cargo_lease_timeout`). `stop_on_probe_fault` and `fail_local_job` then end the attempt with local evidence. So the 13:29Z/13:34Z behaviour, a supervisor blocked in `ep_poll` for 600 s and then `stalled_no_leg`, no longer happens. The run now fails after 5 s with `cargo_lease_timeout`, so a writer still cannot start while a fast check holds the lease. [src/plangraph/spawner/runner.py:478–504,1019–1023; src/plangraph/spawner/probe_cleanup.py:10–11,74–112,162–175; src/plangraph/spawner/exit_outcome.py:68–74; src/plangraph/spawner/host_ready.py:180–183,227–250]
- Lease timeout evidence (`LeaseWait.evidence`) carries the wait, the budget, the lock path and the lock identity (device and inode). It does not carry the holder PID, and `public()` drops the path and the identity. [src/plangraph/spawner/probe_cleanup.py:22–25,51–58; src/plangraph/spawner/host_ready.py:195–202]
- `no_leg_finding` ends a planned `running` record that has no `pid`, `job_id` or `dispatch_started` and only skipped attempts once `PLANGRAPH_WATCHDOG_NO_LEG_SECONDS` (600) have passed. `claim_first_dispatch` sets `dispatch_started` only after the lease is taken, and it reads the record and the save function from `prompt_record`, which the runner already has at the lease call. [src/plangraph/spawner/stall_watchdog.py:126–143; src/plangraph/spawner/service.py:32–44; src/plangraph/spawner/runner.py:186,485,1050]
- `runner.py` is over the 800-line limit on the baseline (1157), so it may not grow. [.filesize-baseline:53]
- NATIVE-CARGO-LEASE-PER-COMMAND-TRIM-AND-HOLDER-PID-R2 (priority 8, open) covers the native writer's whole-life lease in `rust/pg-spawnd/src/native_writer_session.rs` and holder-PID evidence for refusals. This item covers the Python spawner launch only. The holder-PID reader it adds to `LeaseWait.evidence` is the single Python implementation, which R2 reuses for probe refusals.
- The plain runner's lease call is `runner.py:485`. The native launch (`overflow.native_spawn`) also calls `acquire_cargo_lease` at `overflow.py:647`, with no `prompt_record`. The pre-launch writer probe (`leg_probe.before_launch`) runs before `prepare_worktree` and `admit_base`, inside the detached CLI's base admission wait (`ADMISSION_TIMEOUT`, 180 s). A probe whose lease wait times out returns its `local_fault`, and `stop_on_probe_fault` ends the run. [src/plangraph/spawner/overflow.py:609,647; src/plangraph/spawner/leg_probe.py:509–533; src/plangraph/spawner/runner.py:1019–1041; src/plangraph/spawner/base_fresh.py:211,302–316]
- A cross-process cancel (`cancel_recorded` in another process) finds no job, pid or task for a run that has not dispatched yet. It only saves the record as `cancelled`. `claim_first_dispatch` checks a `cancelled` marker only for review checkouts. [src/plangraph/spawner/cancel_recorded.py:25–38; src/plangraph/spawner/service.py:32–44]
- Completion events carry `reason` but not `local_fault`. [src/plangraph/spawner/notify.py:419–445; src/plangraph/spawner/exit_outcome.py:63–65]
- LE-FAST-CHECKS-POOL-SLOT-WAIT-OF-30-S-REFUSES-THE-STAMP (lane a, its D5) owns the native driver's rerun of busy fast checks. This spec does not touch it.

## Decisions

- D1. Queue, not fail, on real contention. This applies to a launch with no probe ownership (`OWNED.get()` is `None`) that is given a `prompt_record`; in practice that is the plain runner call at `runner.py:485`. The native call at `overflow.py:647` and probe launches keep the 5-second bound unchanged. When one bounded acquisition raises `CargoLeaseTimeout` and its evidence has `contention_observed: true` (another holder was seen on the lock), the launch queues. A timeout without observed contention, such as executor saturation, keeps today's `cargo_lease_timeout` failure. To queue, it sets `record['queued_on'] = {'lease': 'cargo', 'lock_path': <str>, 'holder_pid': <int or None>, 'since': <epoch s>, 'until': <epoch s>, 'owner': {'pid': os.getpid(), 'start': <proc_kill.root_identity(os.getpid())>}}`, saves the record with the `prompt_record` save function, and retries acquisitions. Each retry waits `min(5 s, time left before the monotonic queue deadline)`. Each retry refreshes `holder_pid` and saves only when the holder changes. On success it returns the lease without a separate save (D3).
- D2. Queue bound and evidence. `PLANGRAPH_CARGO_LEASE_QUEUE_S` (default 3600, finite and positive; an invalid value refuses the launch with `ValueError`) sets the monotonic deadline and `queued_on.until`. When the deadline passes, the launch raises `CargoLeaseTimeout`. Its public evidence includes `holder_pid` and the total `waited_ms`, and `waited_ms` never exceeds the bound by more than one 5-second retry. The existing local-fault path ends the run `cargo_lease_timeout`. `notify.completion_event` adds `local_fault` (the public evidence) to the parent's completion payload whenever the record has one.
- D3. Dispatch and the watchdog. `claim_first_dispatch` removes `queued_on` in the same save that sets `dispatch_started`, so a record always carries one of the two, and the watchdog never sees a gap. `no_leg_finding` skips a record whose `queued_on` is live, which `probe_cleanup.queued_wait_live(record, now)` decides: `now < until` and the owner is not proven dead (`proc_kill.alive(pid, start)` is not `False`). A `queued_on` whose owner is proven dead, or whose `until` has passed, is an abandoned wait. The finding then carries `reason: 'lease_wait_abandoned'` and the `queued_on` evidence, and the sweep ends the record `stalled_no_leg` with that reason instead of `stalled_no_leg`. A planned run with only skipped attempts and no `queued_on` still ends `stalled_no_leg` as today.
- D4. Holder PID. `holder_pid(lock_path)` lives in `probe_cleanup.py`. On Linux it reads `/proc/locks` and returns the PID of the first lock line whose `major:minor:inode` matches `os.stat(lock_path)`. On any other platform, or when no line matches or the file cannot be read, it returns `None`. `LeaseWait.evidence` adds `holder_pid` when contention was observed, and `public()` keeps it, because a PID is not secret.
- D5. Cancellation across processes. Before each retry, and once more after acquiring and before returning the lease, the queue loop re-reads `record.json` beside `prompt_record[0]`. If its status is no longer `running`, or a `cancelled` file exists in the run directory, it closes any lease it holds and raises `asyncio.CancelledError`, which the runner already handles, so no process is started. An in-process cancel keeps working through task cancellation.
- D6. Contended probe (cold path). In `before_launch`, when the probe result's `local_fault` is `cargo_lease_timeout` with `contention_observed: true`, it does not return the fault. It logs one warning (`probe deferred: cargo lease busy, holder <pid>`) and returns the ordinary admission result (`skip`, normally `None`). The run then continues to `admit_base` and the runner launch, where D1 queues it. The probe timestamp is not booked, so the next spawn probes the leg again. A warm path (probe evidence within the TTL) reaches the runner queue without probing.

Review r1 LE-RUN-WAITING D1(probe): rejected the part "retry the probe after release". The probe runs before `admit_base`, inside the 180 s base admission wait (src/plangraph/spawner/base_fresh.py:302–316; src/plangraph/spawner/runner.py:1021–1041), so waiting for the lease there would end the run `base_admission_timeout`. D6 defers the probe instead, and the queued launch is the first real use of the leg.

## Entry points

- E1. Plain spawner launch: `SpawnerRunner` launch → `probe_cleanup.acquire_cargo_lease(target, prompt_record)` (`runner.py:485`).
- E2. Pre-launch probe: `leg_probe.before_launch` (cold and warm paths).
- E3. Stall watchdog sweep: `stall_watchdog.no_leg_finding` and the sweep update in `_read_runs`.
- E4. Completion delivery: `notify.completion_event`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | With the lease held exclusively by another open file, a launch given a `prompt_record` saves `queued_on` with the lock path, `holder_pid == os.getpid()`, `until` and `owner`. When the holder releases, it returns a lease, and the next save carries `dispatch_started` without `queued_on`. A timeout with no contention (an executor stand-in that delays delivery) fails `cargo_lease_timeout` without `queued_on`. | X1 |
| B2 | E1 E4 | With the queue bound set to 0.3 s and the lease never released, the launch raises `CargoLeaseTimeout` within 0.3 s plus one retry wait. Its public evidence has `holder_pid` and `waited_ms >= 300`, and the completion event delivered to the parent carries that `local_fault`. A probe launch (`OWNED` set) still raises after its 5-second budget with no `queued_on`. | X1 |
| B3 | E1 | A run waiting in the queue whose `record.json` another process has saved as `cancelled` raises `CancelledError` and never calls the process factory. | X1 |
| B4 | E2 | A cold probe that hits a contended lease returns the ordinary admission result with no fault, and the run reaches the runner queue. A warm probe path reaches the queue without probing. A probe timeout without contention still ends the run `cargo_lease_timeout`. | X2 |
| B5 | E3 | A planned running record with only skipped attempts, started beyond the no-leg bound, with a live `queued_on`, gets no finding. With an expired `until`, or an owner proven dead, the finding has `reason: 'lease_wait_abandoned'`. Without `queued_on`, it gets the `stalled_no_leg` finding. | X3 |
| S1 | E1 E3 | On coding.vm a writer spawned during a fast check starts after the check ends, and there is no `stalled_no_leg` record for 24 hours. | X4 |

## Verification

- X1. E1 E4, changed. `tests/test_cargo_lease_queue.py`: `test_a_contended_launch_records_the_holder_and_starts_after_release`, `test_a_timeout_without_contention_does_not_queue`, `test_the_queue_bound_fails_within_its_deadline_with_the_holder_pid_in_the_completion`, `test_a_probe_launch_keeps_the_five_second_bound`, `test_a_run_cancelled_by_another_process_never_launches` and `test_holder_pid_reads_proc_locks` (skipped where `/proc/locks` is absent). Use a temporary target and the isolation fixture of `tests/test_cargo_data_drive_target.py`. Hold the exclusive lease with `host_ready.cargo_lease(exclusive=True)`, and set `probe_cleanup.SHARED_WAIT_S` and `host_ready.SHARED_WAIT_S` to 0.05 as `test_async_lease_deadline_includes_executor_queueing` does. Check: `python3 -m pytest -q tests/test_cargo_lease_queue.py`.
- X2. E2, changed. `tests/test_cargo_lease_queue.py::test_a_cold_probe_on_a_contended_lease_defers_to_the_queue` and `test_a_warm_probe_path_reaches_the_queue`. Stub the probe result's `local_fault` (with and without `contention_observed`) and the leg-state store as `tests/test_leg_probe_budget.py` does. Same check command.
- X3. E3, changed. `tests/test_cargo_lease_queue.py::test_live_abandoned_and_absent_queue_markers_in_the_no_leg_rule`, calling `stall_watchdog.no_leg_finding(record, now)` directly. Same check command.
- X4. E1 E3, live only. The evidence_after_deploy observation.
- X5. Held out. `python3 -m pytest -q tests/test_cargo_data_drive_target.py tests/test_leg_probe_budget.py tests/test_stall_watchdog.py`, unchanged and green. `test_async_lease_deadline_includes_executor_queueing` is the executor-saturation regression.

Write X1–X3 first. B1, B2's queue half, B3, B4 and B5 fail on the base.

## Scope and reuse

- R1. Reuse `acquire_cargo_lease`, `LeaseWait`, `CargoLeaseTimeout`, the local-fault path, the `prompt_record` save, `proc_kill.root_identity` and `proc_kill.alive`. Add no second lease reader and no new record writer.
- N1. The native writer's whole-life lease, per-command Cargo leases and trim between commands are NATIVE-CARGO-LEASE-PER-COMMAND-TRIM-AND-HOLDER-PID-R2. The native driver's rerun of busy fast checks is lane a's LE-FAST-CHECKS-POOL-SLOT-WAIT-OF-30-S-REFUSES-THE-STAMP.

### File budgets

`runner.py` stays at 1157 lines: the call at line 485 gains its second argument in place. `probe_cleanup.py` 221 → at most 300 lines. `stall_watchdog.py` 548 → at most 552 lines: the skip condition and the two sweep `reason=` arguments are edited in place, and the finding's reason is added. `leg_probe.py` 638 → at most 648 lines. `service.py` 634 → at most 635 lines. `notify.py` 455 → at most 456 lines. The change is documented in the fragment `docs/spawner/LE-RUN-WAITING-FOR-THE-CARGO-LEASE-ENDS-STALLED-NO-LEG.md`.

### Implementation

- `probe_cleanup.py`: `acquire_cargo_lease(target=None, prompt_record=None)`. Keep the current body as the single-attempt helper. When `OWNED.get()` is `None` and `prompt_record` is not `None`, loop per D1, D2 and D5. Add `holder_pid` (D4), `queued_wait_live` (D3) and the `evidence` field.
- `runner.py:485`: `self._cargo_leases[job_id] = await acquire_cargo_lease(child_env.get('CARGO_TARGET_DIR'), prompt_record)`.
- `service.py` `claim_first_dispatch`: `record.pop('queued_on', None)` before the existing `save(record)` that sets `dispatch_started`.
- `leg_probe.py` `before_launch`: D6, at the existing `if fault := result.get('local_fault')` branch.
- `notify.py` `completion_event`: `**({'local_fault': record['local_fault']} if 'local_fault' in record else {})`.
- `stall_watchdog.py`: D3.

## Open points for design approval

None.
