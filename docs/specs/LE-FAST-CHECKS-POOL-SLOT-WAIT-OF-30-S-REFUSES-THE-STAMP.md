# LE-FAST-CHECKS-POOL-SLOT-WAIT-OF-30-S-REFUSES-THE-STAMP

Outcome. A fast-checks run waits for a Cargo pool slot as long as its caller sets (default 900 s) and prints who holds the slots. A wait that runs out exits with its own busy code, writes no receipt, and the native writer driver retries it instead of starting a fix turn.

- plan_source. Keeper item plangraph:LE-FAST-CHECKS-POOL-SLOT-WAIT-OF-30-S-REFUSES-THE-STAMP, 2026-11-25 (incident 2026-11-27 12:40Z), priority 10, D-7052.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. e4d87d6228c3e9fd775a5f8beb0d2e823e4786d2d62bc77989cb7e2d063cb468
- tree SHA-256. f11e127c066b3776132942727dfc2600f6d8fd9133005749c9c874347e7d69d0
- evidence_after_deploy. After the CLI and pg-spawnd deploy, record one lane push or native writer log on the workstation or coding.vm that shows `waiting up to 900s for a slot` with the holder lines. If a wait runs out, record exit 75, no receipt for that head and the driver's `fast_checks.status` `busy` without a writer fix turn.

## Checked against base

- The wait is a constant: `SLOT_WAIT_LIMIT = 30 s` (`rust/plangraph-cli/src/fast_check_target.rs:12`, used at `:114`). Its timeout returns the error `cargo pool: all N cargo pool slots are busy; timed out after 30s waiting for a slot` (`fast_check_target.rs:117-146`).
- Every error leaves `fast_checks::run` as exit 1 (`rust/plangraph-cli/src/fast_checks.rs:321-329`), the same code as a failed check.
- The owned parent adds a synthetic `fast-checks` row with the child's code when the child listed no failure, and publishes a red receipt (`rust/plangraph-cli/src/fast_check_worktree.rs:73-91`). So a busy pool produces a red receipt for a head that nothing checked.
- The native writer host maps any non-zero exit to `plangraph fast-checks failed (...)` (`rust/pg-spawnd/src/native_writer_host.rs:391-425`). The driver loop then resumes the writer with "The fast checks failed ... Fix the cause" and counts it against `FAST_CHECK_FIXES = 2` (`rust/pg-spawnd/src/native_writer.rs:64,496-512`).
- Slot holders are recorded per slot as `SlotLeaseRecord {run_id, pid, acquired_at}` in `<slot>.lease.json` (`rust/worker-core/src/cargo_pool.rs:33-37,307-313,390-425`). fast-checks labels every lease `fast-checks` (`fast_checks.rs:426`), so the holder lines cannot tell lanes apart.
- The slot count is already host-derived: `configured_slot_count` fits measured slot sizes into free disk, clamped to `[1, DEFAULT_POOL_SLOTS = 3]` (`cargo_pool.rs:29,97-124`). coding.vm's single slot is its disk bound.

## Decisions

- D1. The caller sets the slot wait through the environment variable `PG_FAST_CHECK_SLOT_WAIT_S`, whole seconds, `0` allowed (no wait). An unset variable means 900 s. The value must be whole seconds from 0 to 7200 (two hours, so `Instant::now() + wait` cannot overflow and a typo cannot park a lane for days). A non-integer, negative or larger value fails with `PG_FAST_CHECK_SLOT_WAIT_S must be whole seconds from 0 to 7200`. The setting is read and validated only on the pool-acquisition path, immediately before `resolve_cargo_target`. `--receipt-ref`, `--check-notes`, `--stamp-green`, `--plan` and runs that take no pool slot (the foreign modes of LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY) ignore it, even when it is invalid. 900 s covers one stamp run of 5-8 minutes (item measurement) by the current holder. A deeper queue returns busy and the driver retries (D5). The owned parent passes its environment to the child, so drivers and lane scripts need no argv change.
- D2. When the first attempt finds every slot busy, print `fast-checks: all N cargo pool slots are busy; waiting up to Ws for a slot` and one line per slot: `fast-checks: slot I held by RUN pid=P for S s`, or `slot I lease unreadable`. Print them again on timeout. Read the holders with a new `cargo_pool::slot_holders(primary, count) -> io::Result<Vec<(usize, Option<SlotLeaseRecord>)>>`. Label each fast-checks lease `fast-checks <first 12 of HEAD>` instead of `fast-checks`.
- D3. A timeout returns an error that starts with the constant `POOL_BUSY = "cargo pool busy"`, followed by the existing busy text, the wait and the holders. `fast_checks::run` maps an error with that prefix to exit code `pg_worker_core::fast_check_stamp::BUSY_EXIT = 75` (EX_TEMPFAIL). Every other error stays exit 1.
- D4. Owned parent: a child exit of 75 that listed no check row publishes no receipt and returns `cargo pool busy: nothing checked; no receipt for HEAD`. The parent also exits 75. Any other non-zero exit keeps today's red receipt.
- D5. Native writer: `Host::fast_checks` maps exit 75 to an error that starts with `FAST_CHECKS_BUSY = "fast_checks_busy"`. The driver loop reruns the fast checks on the same head while they report busy, up to `FAST_CHECK_BUSY_TRIES = 3` runs. Each busy run records `fast_checks = {head, status: "busy", output}` and saves. A busy run never resumes the writer and never counts against `FAST_CHECK_FIXES`. After the third busy run the run ends with exit 7 and reason `fast_checks_busy: <output>`.
- D6. The slot count stays disk-derived. Raising the ceiling of 3 by RAM or cores would only add slots on hosts with spare disk. coding.vm is disk-bound and under the operator order of 2026-11-27 no builds run there. This spec does not implement How point 3. No keeper approval of dropping it exists yet; the planner decides (see Open points).

## Entry points

- E1. `fast_check_target::acquire_slot_with_wait` and the wait setting.
- E2. `fast_checks::run` exit code and `fast_check_worktree::settle`.
- E3. `cargo_pool::slot_holders`.
- E4. pg-spawnd `native_writer_host::Host::fast_checks` and the `native_writer` driver loop.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 E3 | With every slot held, the wait lasts the configured time, and the message starts with `cargo pool busy` and names each holder's run label and `pid=`. The CLI prints the wait line and one holder line per slot, then exits 75. Out-of-range settings are refused only where a slot is taken. | X1 X2 X3 X9 |
| B2 | E2 | A busy error exits 75. A failed check still exits 1. | X4 |
| B3 | E2 | A busy child leaves neither the stamp file nor the local receipt ref for the head. A child failing with exit 1 still writes a red stamp file and a red receipt ref. | X5 |
| B4 | E4 | Exit 75 maps to `fast_checks_busy`, and every other non-zero exit to `plangraph fast-checks failed`. | X6 |
| B5 | E4 | Busy then green proceeds to review with no extra writer turn. Three busy runs end the run with exit 7 and `fast_checks_busy`, with no fix turn. | X7 |

## Verification

Write X1-X7 and X9 first. On the base they fail to compile (new symbols) or show exit 1 and a red receipt.

- X1. `fast_check_target.rs` test `busy_pool_message_names_each_holder`. Hold slot 0 with run `holder-run` and call `acquire_slot_with_wait(&primary, 1, "waiting-run", 25 ms)`. Assert the error starts with `POOL_BUSY` and contains `holder-run` and `pid=`. Keep `wait_for_pool_slot_times_out_if_every_lease_stays_busy` (its two `contains` checks still hold).
- X2. `fast_check_target.rs` test `slot_wait_for_a_busy_pool_defaults_to_900_seconds`. A pure `slot_wait(value: Option<&str>) -> Result<Duration>` gives `None` 900 s, `"0"` 0 s, `"45"` 45 s and `"7200"` 7200 s, and errors on `"x"`, `"-1"`, `"1.5"`, `"7201"` and `"18446744073709551615"`.
- X3. `rust/worker-core/tests/cargo_pool.rs::busy_pool_lists_each_slot_holder`. Two leases with distinct run ids give two `Some` records in slot order. A corrupt lease file gives `None` for that slot.
- X4. `fast_checks.rs` tests module `busy_error_exits_75_and_other_errors_exit_1`. A pure `exit_code(&Result<()>) -> i32` returns 75 for an error with the `POOL_BUSY` prefix, 1 for `failed: x` and 0 for `Ok`.
- X5. `fast_checks.rs` tests module `busy_child_publishes_no_receipt`. Use `repo()`, an empty results file and the status of `sh -c 'exit 75'`. `owned::settle` returns an error with the `POOL_BUSY` prefix. The stamp file at `worktree::fast_check_stamp(root, head)` does not exist, and `git rev-parse --verify refs/plangraph/receipts/<head>` fails. The same setup with exit 1 writes the stamp file with `result red` and creates the ref (existing behaviour, `fast_check_stamp.rs:189-213`).
- X6. `rust/pg-spawnd/src/native_writer_host_review_tests.rs::busy_exit_maps_to_fast_checks_busy_and_other_exits_to_failed`. Test a pure `shared_check_error(status: &ExitStatus, stderr: &[u8]) -> Option<String>` extracted from `Host::fast_checks`, with statuses from `sh -c 'exit 75'`, `sh -c 'exit 1'` and `true`.
- X7. `rust/pg-spawnd/src/native_writer_stop_tests.rs`: `busy_fast_checks_rerun_without_a_writer_turn` and `busy_fast_checks_end_the_run_after_three_tries`. Use a `Busy { inner: Fake, busy: usize }` wrapper that delegates every `Driver` method to `Fake` and returns `fast_checks_busy: all 1 cargo pool slots are busy` for the first `busy` calls of `fast_checks`, following the `Idle(Fake)` pattern in `native_writer_idle_tests.rs`. With `busy = 1`: exit 0, `inner.calls.len() == 1` and one review. With `busy = 3`: exit 7, the reason starts with `fast_checks_busy`, `inner.calls.len() == 1` and no review.
- X9. `rust/plangraph-cli/tests/fast_checks.rs::a_busy_pool_exits_75_and_prints_the_holders`. Build a temp git repository with a base commit and a head commit, and a stub `tools/ci_select.py` that prints every suite `=false`. Set `PLANGRAPH_CARGO_TARGET_DIR` to a temp primary and hold every slot of `cargo_pool::configured_slot_count(primary)` with run ids `busy-<i>` through `pg_worker_core::cargo_pool::acquire_slot`. Run `plangraph fast-checks --base <base>` with `PG_FAST_CHECK_SLOT_WAIT_S=1`. Assert exit 75, stderr containing `waiting up to 1s for a slot`, one `held by busy-<i> pid=` line per slot, and stderr starting its final line with `fast-checks: cargo pool busy`. With `PG_FAST_CHECK_SLOT_WAIT_S=99999` the same run exits 1 with the range message. `fast-checks --plan --files README.md` with that value exits 0.
- X8. Held out: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p pg-spawnd --lib native_writer`.

## Scope and reuse

- R1. One busy marker per layer. `POOL_BUSY` is the single prefix in plangraph-cli, `BUSY_EXIT` the single code shared through `pg_worker_core::fast_check_stamp`, and `FAST_CHECKS_BUSY` the single driver marker, matched like `writer_control::STOPPED`.
- R2. Holders come from the existing lease records. No new state file.
- N1. Lane push scripts outside this repository read exit 75 as "retry later" from the contributing note. Changing them is not part of this item.
- N2. The 300 s wall budget line keeps its meaning. The slot wait is printed on its own line, `fast-checks slot wait seconds: S`, and is not subtracted.

### File budgets

- `rust/plangraph-cli/src/fast_check_target.rs`: 336 lines, at most 60 lines added.
- `rust/plangraph-cli/src/fast_checks.rs`: 500 lines, at most 45 lines added. The overlapping card LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY adds at most 45 more, and the sum stays under 600.
- `rust/plangraph-cli/src/fast_check_worktree.rs`: 135 lines, at most 15 lines added.
- `rust/worker-core/src/cargo_pool.rs`: 426 lines, at most 25 lines added.
- `rust/worker-core/src/fast_check_stamp.rs`: 774 lines, at most 3 lines added.
- `rust/worker-core/tests/cargo_pool.rs`: 283 lines, at most 30 lines added.
- `rust/plangraph-cli/tests/fast_checks.rs`: 688 lines, at most 70 lines added.
- `rust/pg-spawnd/src/native_writer.rs`: 627 lines, at most 25 lines added.
- `rust/pg-spawnd/src/native_writer_host.rs`: 550 lines, at most 15 lines added.
- `rust/pg-spawnd/src/native_writer_host_review_tests.rs`: 449 lines, at most 25 lines added.
- `rust/pg-spawnd/src/native_writer_stop_tests.rs`: 46 lines, at most 90 lines added.
- `docs/contributing/LE-FAST-CHECKS-POOL-SLOT-WAIT-REFUSES-THE-STAMP.md`: new, at most 10 lines: the setting, the default, exit 75 and what callers do with it.
- `changelog.d/LE-FAST-CHECKS-POOL-SLOT-WAIT-OF-30-S-REFUSES-THE-STAMP.md`: new, at most 5 lines.

### Implementation

- `resolve_cargo_target` gains a `wait: Duration` parameter. `fast_checks::execute` reads `slot_wait(env)` right before `resolve_cargo_target`, and nowhere else (D1). The tests that call `resolve_cargo_target` pass `Duration::from_secs(30)`.
- `acquire_slot_with_wait` prints the D2 lines and builds the D3 error. After a successful wait it prints `fast-checks: slot I after S s wait`.
- `fast_checks::run` becomes `exit_code(&execute(args))` after printing the error.
- `settle` checks `status.code() == Some(BUSY_EXIT) && checks.is_empty()` before the synthetic row.
- `Host::fast_checks` uses `shared_check_error`. The driver loop wraps `driver.fast_checks(&head)` in the bounded busy rerun of D5, after the `STOPPED` check.

## Open points for design approval

None for the writer. The planner must either approve dropping How point 3 (slot count from RAM and cores) on the item, or split it into its own item. This spec does not implement it (D6).
