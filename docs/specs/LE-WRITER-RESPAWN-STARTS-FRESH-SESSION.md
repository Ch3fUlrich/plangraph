# LE-WRITER-RESPAWN-STARTS-FRESH-SESSION

Outcome. Respawn an offline numbered writer with the saved session from the selected destination.

- plan_source. SPEC-CARD-PLAN draft 10, section 4 P1–P3, SHA-256 cae44c2a1179c397a4602789b70aadba05999ac71e396b7b0c4330cbcd100909.
- base commit. 22d34b76a8572b80eb19a4fc6c18327b78d7feda
- card SHA-256. 4570a7442545bea650db8560f948ccd142eb1216d3869dcb63e3c79a9db5bbef
- tree SHA-256. c6685445419d6a44233590886462271a808ca75eaf0376e6401a3680ca791a0c
- evidence_after_deploy. Live: After authorized cutover to the Rust launcher, witness an offline numbered writer resume with its original destination identity and cite the next status HEAD. The launcher cutover and witness have separate deployment authority.

## Decisions

- D1. The destination session record defines identity. Rule 3.85 governs install and live evidence independently of source delivery.

## Entry points

- E1. Existing rust/pg-spawnd/src/session/remote.rs::launch forwarding path and destination admission.
- E2. Existing rust/pg-spawnd/src/session/mod.rs local launch and result handling.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A forwarded dry-run/fixture launch uses the destination-saved numbered writer ID and exact session name. | X1 |
| I1 | E1 | Dry-run and live fixture transport refuse when the destination has no matching record, including when the sender has one. | X2 |
| S1 | E2 | A successful resume with no new ID keeps the previously selected ID for the next resume. | X3 |

## Verification

- X1. E1, changed. cargo target fleet_launch_parity::resume::forwarded_respawn_uses_destination_record. Asserts B1's observable result.
- X2. E1, changed. cargo target fleet_launch_parity::resume::source_only_record_is_refused. Asserts I1's observable result.
- X3. E2, changed. cargo target fleet_launch_parity::resume::success_without_id_preserves_selected_id. Asserts S1's observable result.

Anchors for new or changed fixtures are predicted until checked on the implementation head. Record the actual invoked nodes, results and exact-head receipt.

## Scope and reuse

- R1. Serves B1, I1, S1. Reuse configured host aliases, storage record identity, existing forwarding and result persistence. Do not copy origin authorization onto the destination.
- N1. No launcher cutover, real session restart, Claude run or unit change. Record the live witness only after the current owning keys and a separate DEPLOY authorization.

Use the destination's previously recorded numbered writer session for an offline respawn and retain that identity across successful resume.

- Scope. `rust/pg-spawnd/src/session/{mod.rs,remote.rs,request.rs,placement.rs,tests.rs}`, launcher fixtures `rust/pg-spawnd/tests/fleet_launch/{remote.rs,resume.rs,sandbox.rs}` and `rust/pg-spawnd/tests/fleet_launch_parity.rs`, plus its changelog. Joint b181 documentation, if a behavior statement changes, belongs in `docs/SPAWNER.md` and `docs/native-codex-writer.md`.
- Inputs and ownership. The destination worktree and local saved session record bind identity. The prompt travels through the existing forward path. Both dry run and live admission refuse a missing target record; neither creates one from origin state. A successful resume with no returned ID retains the known ID. Host aliases use the existing canonical resolver.
- Tests and evidence. Fake transport verifies target-only record, same session/name, missing-record refusal and no-ID success, including forwarded and target-host dry-run paths. Use `cargo test --manifest-path rust/Cargo.toml -p pg-spawnd --test fleet_launch_parity` and current session unit tests. Live evidence of an offline writer's next status HEAD is deferred to separate authorization after the existing Rust launcher cutovers.

## Open points for design approval

None.
