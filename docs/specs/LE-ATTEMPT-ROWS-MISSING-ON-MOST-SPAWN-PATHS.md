# LE-ATTEMPT-ROWS-MISSING-ON-MOST-SPAWN-PATHS

Outcome. Verify one complete attempt row per admitted terminal run using the existing lifecycle producer.

- plan_source. SPEC-CARD-PLAN draft 10, section 4 P1–P3, SHA-256 cae44c2a1179c397a4602789b70aadba05999ac71e396b7b0c4330cbcd100909.
- base commit. 22d34b76a8572b80eb19a4fc6c18327b78d7feda
- card SHA-256. 2bae7c841067ba55e74be73f2fc91195d49c775bbffd2215a6968315fa9a3549
- tree SHA-256. 8eb10593d014d194d3dac7308d8762d98a4039961899f2ae17d22da31af5d69b
- evidence_after_deploy. Live: After authorized rollout, compare one full UTC day of terminal registrations and Keeper attempt rows by run and host, retaining evidence of the 95% threshold and the classified unpinned attempts consumed by route shadow.

## Decisions

- D1. D-6661 permits the existing telemetry evidence path. D-6741 explains the evidence need and grants no launch. D-6677 and D-6749 were applied to the plan graph.
- D2. Daily coverage, fleet registration and lifecycle delivery child items are complete.

## Entry points

- E1. Existing rust/pg-spawnd/tests/attempt_outcome.rs exercises production terminal projection through pg-spawnd.
- E2. Existing rust/pg-spawnd/tests/spawn_attempt_delivery/mod.rs shared delivery fixture called by hand_run and fleet_launch_parity.
- E3. Existing rust/pg-spawnd/src/attempt_coverage.rs::execute read-only coverage check; preserve threshold.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | One complete terminal row per registered run identity, with class, leg, harness, host and actual terminal outcome. | X1 |
| S1 | E2 | A fixture with an acknowledgement interruption redelivers under the same identity and never double-counts. | X2 |
| I1 | E3 | Missing or conflicting attribution cannot count as coverage. Daily threshold and UTC day boundary remain stable. | X3 |

## Verification

- X1. E1, preserved. cargo target attempt_outcome::terminal_spawn_projects_final_leg_once. Asserts B1's observable result.
- X2. E2, changed. cargo target hand_run::hand_writer_and_external_review_record_terminal_outcomes. Asserts S1's observable result.
- X3. E3, preserved. cargo target attempt_outcome::daily_coverage_joins_by_run_and_host_at_the_exact_boundary. Asserts I1's observable result.

Anchors for new or changed fixtures are predicted until checked on the implementation head. Record the actual invoked nodes, results and exact-head receipt.

## Scope and reuse

- R1. Serves B1, I1, S1. Reuse the existing lifecycle producer, attempt row schema, run identity, daily coverage calculation and shared delivery fixture. Cross-check seat, spawner, native success/crash, fleet and workstation coverage before marking the scope complete.
- N1. No new transport, producer, live activation or b181 test ownership. If the remaining concrete case needs another production file, obtain a revised exact scope before assigning it.

Show that each registered terminal run emits one complete keeper attempt row through the existing lifecycle producer.

- Scope. Extend existing fixtures in `rust/pg-spawnd/tests/attempt_outcome.rs` and `rust/pg-spawnd/tests/spawn_attempt_delivery/mod.rs` and the item's changelog. Compare actual gaps against `rust/pg-spawnd/src/attempt_coverage.rs` and the existing lifecycle delivery code first. Any remaining production-path changes need separately enumerated per-file scope before implementation.
- Inputs and ownership. Reuse terminal transition identity, card task class or `adhoc`, leg, harness, host and exit class. Preserve exactly-once receipt identity on retry or lost acknowledgement. Preserve day and host attribution and the existing daily coverage alert below 95%. No second producer or transport. Retain the shared `rust/pg-spawnd/tests/native_writer.rs` under b181 for this wave.
- Checks and evidence. Exercise seat, implement, native success and crash, registered fleet launch and workstation attribution against fixture state, including replay with lost acknowledgement. Keep log evidence for actual run/row coverage after separately authorized deployment. Local fixtures and any subsequent live witness are distinct.

## Open points for design approval

None.
