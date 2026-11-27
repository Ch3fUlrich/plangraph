# NATIVE-CARGO-COMMAND-LEASE

Outcome. Bound shared Cargo lease acquisition and classify a pre-dispatch probe deadline as a local host fault.

- plan_source. D-6958 and D-6959 incident decisions, D-6960 rollout scope, validated design packet SHA-256 8312f0012a57ce3d5a94ed0fb9a235145ea8b9ffbdd0763e00ecd4b954bcd05a.
- base commit. d948affa1da9565f12c58fb981bbf2dbc04974ad
- card SHA-256. dd39ddc6d1413fc4fc8e760b1d94586ac3ba5aa1453a3286cee0012495a68ca8
- tree SHA-256. ff04a23599bf0a33b6441c90514ecf7aac26f322acae351f7429d1e399da1ea6
- evidence_after_deploy. Live check under separate rule 3.85 authority. Run the owned fast-check against a private target and show the mocked nested launch completing. Read the canonical probe receipt for a deliberately local waiting fault and verify it creates no provider booking. Any production provider probe needs its own current authority and obeys the Neon Medium hold.

## Decisions

- D1. D-6958 and D-6959 record a recursive shared Cargo lease behind an exclusive fast-check and a Hydrogen timeout artifact. D-6960 assigns one writer and a private target for verification. Maintain actual cargo protection around real commands.
- D2. Preserve the distinction between a local pre-dispatch failure and a provider timeout after process creation. Do not mark a provider healthy, down, or quota-limited from a known local lease wait.
- D3. Use the existing sibling lock on the selected target. Preserve immediate exclusive attempts and distinguish OS contention from other errors. Bound shared acquisition by a monotonic deadline and preserve concurrent readers and admitted isolated reviewers.

## Entry points

- E1. host_ready._cargo_lease_for and cargo_lease, plus existing preflight policy cache reuse and invocation.
- E2. SpawnerRunner.spawn and overflow.native_spawn with the existing mocked native_evidence harness.
- E3. leg_probe.probe and before_launch, existing probe_cleanup, and pre-dispatch outcome persistence.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A .2 second shared request behind an exclusive owner ends with a local contention error within its bounded wait. Concurrent shared readers coexist, cache reuse on a busy target misses promptly and recovery succeeds after release. | X1 X2 X8 |
| B2 | E2 | The nested mock launcher reaches its assertion while the parent target is locked. Its separate target does not bypass a real writer lease. Isolated reviewers keep their target and build-directory isolation. | X3 X4 |
| I1 | E3 | A cancelled or expired probe waiting for its local lease cannot start a provider if the lock is released during cleanup. It reports a local reason and creates no provider liveness or quota booking. | X5 |
| S1 | E3 | A genuine timeout after provider creation follows the normal path. Successful probes, limited legs, and cancellation cleanup retain their existing meaning. | X6 X7 |

## Verification

- X1. E1, changed. tests/test_cargo_data_drive_target.py::test_shared_cargo_lease_has_a_deadline and tests/test_cargo_data_drive_target.py::test_shared_cargo_lease_preserves_parallel_readers.
- X2. E1, changed. tests/test_preflight_policy_prebuild.py::test_cached_policy_reuse_misses_under_an_outer_exclusive_lease.
- X3. E2, changed. native_evidence::merged_review_seats_ignore_native_writers_and_origin_receipts. Run it under an outer exclusive fixture lock with distinct compile artifacts, and require exact mock launch and provenance assertions.
- X4. E2, changed. tests/test_claude_child_env.py::test_cargo_target_and_shared_lease_at_spawn and tests/test_leg_probe_budget.py::test_isolated_review_dispatch_ignores_shared_cargo_contention.
- X5. E3, changed. tests/test_leg_probe_budget.py::test_probe_cargo_lease_deadline_never_dispatches and tests/test_leg_probe_budget.py::test_cancelled_probe_cargo_wait_never_dispatches.
- X6. E3, preserved. tests/test_leg_probe_w1.py::test_timeout_mid_spawn_still_cancels_child and tests/test_leg_probe_w1.py::test_probe_writes_through_the_reader_store.
- X7. E3, preserved. tests/test_leg_probe_w1.py::test_probe_many_skips_a_limited_leg.
- X8. E1, changed. Verify Windows shared contention from a separate process using the actual supported lock primitive. Verification is by the X1 fixture under native Python, with a bounded test subprocess.

Expected new test anchors are provisional until implementation. Quote the actual red and green commands and exact output summaries. Keep the independent holdout checks in the execution card.

## Scope and reuse

- R1. Serves B1, B2, I1 and S1. Reuse the existing job, run and attempt evidence, private review target, receipt and cancellation paths. Gate first provider dispatch on ownership of a non-expired launch. Propagate the local timeout through terminal routing without provider fallback or liveness mutation.
- N1. Scope is the tree and card. Preserve the existing clock, provider priority, live quota, workflows, gate logic, host service configuration and protected release flow. No new Python source file, environment bypass marker or clearing the existing provider ledger.
- N2. The item also records a broader lease-per-command design history. The approved slice covers this deadlock, bounded acquisition and pre-dispatch probe classification. Record any remaining wider lease work against existing acceptance and route it separately.

## Open points for design approval

None.
