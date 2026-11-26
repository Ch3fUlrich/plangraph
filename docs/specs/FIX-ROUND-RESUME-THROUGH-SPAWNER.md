# FIX-ROUND-RESUME-THROUGH-SPAWNER

Outcome. Resume a fix round on the original host and writer session with the admitted worktree and fresh round state.

- plan_source. SPEC-CARD-PLAN draft 10, section 4 P1–P3, SHA-256 cae44c2a1179c397a4602789b70aadba05999ac71e396b7b0c4330cbcd100909.
- base commit. 22d34b76a8572b80eb19a4fc6c18327b78d7feda
- card SHA-256. b08d9b95170dc25af083bb82a6b7a52658100b7ce6e6909b7b79ebf774b1685a
- tree SHA-256. 3a31831daee65de0e837d55715fc420493d4eff2e415942c71367b143e78d246
- evidence_after_deploy. Live: When the pg-spawnd binary is separately deployed, record an authorized canary showing a fix-round commit under the previously recorded writer session, host and worktree. Preserve the exact source head and canary result.

## Decisions

- D1. The existing prerequisite LE-NATIVE-WRITER-FIX-ROUND-RESUME-HAS-NO-TOOL is complete. The current run record defines the resumed session, admitted worktree and host.

## Entry points

- E1. Existing rust/pg-spawnd/src/native_writer_commands.rs::resume and ::resume_saved plus native writer session handoff.
- E2. Existing src/plangraph/spawner/remote_ps.py::native_receipt and remote forwarding receipt refresh.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | An admitted fake Codex follow-up commits in the original recorded session, worktree and host using the existing writable Git metadata scope. | X1 |
| I1 | E1 | An absent or mismatched host/session/worktree pin fails without launching or opening a fresh writer session. | X2 |
| S1 | E2 | After acknowledged resume, failed refresh keeps the new round running without the prior head and exit code. | X3 |

## Verification

- X1. E1, changed. cargo target native_resume_route::resumes_same_session_and_produces_new_commit. Asserts B1's observable result.
- X2. E1, changed. cargo target native_resume_route::refuses_mismatched_pins_before_start. Asserts I1's observable result.
- X3. E2, changed. pytest tests/test_spawner_remote_ps.py::test_remote_resume_ack_clears_previous_outcome. Asserts S1's observable result.

Anchors for new or changed fixtures are predicted until checked on the implementation head. Record the actual invoked nodes, results and exact-head receipt.

## Scope and reuse

- R1. Serves B1, I1, S1. Reuse the original admission and session record, worktree-scoped sandbox path helper, existing locks and native/forwarding run records.
- N1. No broader write authority, unrelated session change, live tool launch, host installation or cutover. b153/b183 retain their existing ownership.

Resume a fix round in the same admitted writer session on the same host and prove that it can commit in its owned worktree.

- Scope. Use the existing CLI dispatch and native session path in `rust/pg-spawnd/src/{cli.rs,main.rs,native_writer_commands.rs,native_writer_session.rs}`, the bounded remote-resume adapter at `rust/pg-spawnd/src/native_writer_remote_resume.rs`, existing Python forwarding/receipt paths in `src/plangraph/spawner/{cli.py,remote_ps.py}`, Rust fixtures `rust/pg-spawnd/tests/{native_writer.rs,native_resume_route.rs,native_writer_fixture/codex.rs}`, and the item's changelog. Rebase against the base named above and reduce the write set to observed gaps. Use native writer's existing workspace-write roots for the already-admitted worktree metadata only.
- Inputs and ownership. Original run ID, recorded session ID, originating host and owned worktree are authoritative. A missing or mismatched pin refuses before spawning. Use the spawner's existing native resume and settlement path. Clear stale terminal result fields from a new running receipt after acknowledgement; a failed refresh preserves the acknowledged round as running without representing old results as current. Reuse current locking and idempotency.
- Tests. Fake Codex performs a second commit in the original session/host; a fixture verifies the existing allowed metadata root; wrong pin fails closed; lost refresh cannot surface the previous round's head or exit. Use `cargo test --manifest-path rust/Cargo.toml -p pg-spawnd --test native_writer`, `cargo test --manifest-path rust/Cargo.toml -p pg-spawnd --test native_resume_route`, and existing forwarding receipt tests in Linux and Windows as applicable. No remote resume of an actual writer during development.

## Open points for design approval

None.
