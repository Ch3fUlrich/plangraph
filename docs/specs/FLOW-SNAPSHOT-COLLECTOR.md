# FLOW-SNAPSHOT-COLLECTOR

Outcome. Produce the existing watchdog snapshot from the configured read-only sources and publish it atomically.

- plan_source. SPEC-CARD-PLAN draft 10, section 4 P1–P3, SHA-256 cae44c2a1179c397a4602789b70aadba05999ac71e396b7b0c4330cbcd100909.
- base commit. 22d34b76a8572b80eb19a4fc6c18327b78d7feda
- card SHA-256. 45eaa74db3cae907accac0873bf75f8818bd618f7e107206def20babbffb21b6
- tree SHA-256. 66179fc807401838ca8554b9100e1766b4440d2fd050f2106b6162ecd3e38b5a
- evidence_after_deploy. Live: From a separately authorized plangraph CLI release, record an atomic snapshot that the installed watchdog accepts, using configured read-only sources and reporting each unavailable input. Activation of a unit is outside scope.

## Decisions

- D1. Rule 3.60 (D-6381) requires stuck-work alerts and one source of truth. Rule 3.63 (D-6515) keeps stall handling in code. D-6740 raises the priority and defines no scope.
- D2. The existing collector plan defines the source fields. FLEET-SERIES-SAMPLER and STALL-DELIVERY-FIX are complete.

## Entry points

- E1. Planned rust/plangraph-cli/src/flow_snapshot/mod.rs CLI entry point; existing rust/plangraph-cli/src/main.rs dispatch changes.
- E2. Planned rust/plangraph-cli/src/flow_snapshot/live.rs source reader.
- E3. Planned rust/worker-core/src/flow_collect/derive.rs telemetry derivation.
- E4. Existing rust/worker-core/src/bin/flow-watchdog.rs::Snapshot; compatibility must be preserved.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | Valid fixtures produce a complete snapshot by one atomic publication. | X1 |
| I1 | E2 | Request JSON for readiness. Ready-source failure retains independent Keeper facts and exposes missing readiness. | X2 |
| I2 | E3 | Unreasonably future or old host samples cannot mark an idle lane current. Reopened items use the current review epoch. | X3 |
| S1 | E1 | A required input failure leaves the last valid output intact. Concurrent writes cannot leave partial JSON. | X4 |
| I3 | E4 | Removing any required field makes the existing watchdog report invalid snapshot. | X5 |

## Verification

- X1. E1, changed. cargo target flow_snapshot::fixture_cli_publishes_snapshot. Asserts B1's observable result.
- X2. E2, changed. cargo target flow_snapshot::ready_failure_keeps_keeper_snapshot. Asserts I1's observable result.
- X3. E3, changed. cargo target flow_collect::bounded_samples_and_current_review_epoch. Asserts I2's observable result.
- X4. E1, changed. cargo target flow_snapshot::failed_collection_preserves_valid_output. Asserts S1's observable result.
- X5. E4, preserved. cargo target flow_snapshot::watchdog_rejects_each_missing_required_field. Asserts I3's observable result.

Anchors for new or changed fixtures are predicted until checked on the implementation head. Record the actual invoked nodes, results and exact-head receipt.

## Scope and reuse

- R1. Serves B1, I1, I2, I3, S1. Reuse the existing Snapshot schema, Keeper JSON snapshot, protected CI/Git ancestry, configured host sampler, usage, leg-state and unit inventory readers. Route any remaining typed operator-order/idle data gap before approval.
- N1. No host install, watchdog activation, second collector, or b181 test changes. Do not substitute the legacy Python acceptance command until routing accepts its Rust equivalent.

Deliver one Rust `plangraph flow-snapshot --json` collector that produces the existing watchdog input from the existing data sources and emits explicit failure evidence.

- Scope. Add the collector under `rust/plangraph-cli/src/flow_snapshot/{mod.rs,live.rs,live_tests.rs}`, the pure collection modules under `rust/worker-core/src/flow_collect/{mod.rs,derive.rs,stuck.rs}`, tests at `rust/plangraph-cli/tests/flow_snapshot.rs` and `rust/worker-core/tests/flow_collect.rs`; wire from the two crate library roots and the CLI main. Update `docs/flow-watchdog.md` and `changelog.d/FLOW-SNAPSHOT-COLLECTOR.md`. Reuse the current watchdog input types and reader contract. Historical b5b043ac is evidence only.
- Inputs and ownership. Use Keeper snapshot, JSON ready response and current item rounds/epochs, Git ancestry and exact CI evidence, the existing usage and leg-state readers, unit SHA facts, and FLEET-SERIES-SAMPLER rows by configured host. Collect ready families and pending work by role from the corresponding typed source. Include the watchdog's operator-order and idle-lane evidence only when supported by its published typed contract. Use one collection pass and same-window counts. No second authoritative store. The collector owns only its output; publish atomically, preserving the last valid output if required data cannot be read. A ready subquery failure must not discard independently valid snapshot data. Preserve explicit unknowns for optional readers.
- Tests. Fixture CLI output must pass the current checker. Removing each required field yields invalid-snapshot. Ready must ask for JSON; its failure preserves snapshot facts. A sample outside both sides of the accepted time window cannot count as current. Reopening an item cannot retain an earlier review epoch. Tests exercise injected Rust fixture inputs, no tests using live services.
- Checks. `cargo test --manifest-path rust/Cargo.toml -p plangraph-cli --test flow_snapshot` and `cargo test --manifest-path rust/Cargo.toml -p pg-worker-core --test flow_collect`, plus the existing watchdog regression test. Report exact input sources, missing reasons and raw summaries. No live unit activation.

## Open points for design approval

- Confirm the Rust integration check as the replacement for the keeper's historical Python harness criterion. Rule 9.14 governs new test source.
- Confirm how the collector must report operator-order evidence if the typed source is unavailable. The placement and ready-lane evidence remains in scope.
