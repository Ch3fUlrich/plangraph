# BATCH-STEP-RUNNER-AGENT-STEPS

Outcome. Run native write, exact-head review and same-session fix through the batch step runner’s agent transitions.

- plan_source. Keeper item plangraph:BATCH-STEP-RUNNER-AGENT-STEPS (2026-11-25), batch routing D-7038, priority 10.
- base commit. 506c0cf41b72cb3e5aab7a08a11dec45b8b6ed64
- card SHA-256. 8c7d69bdb8469138a85970071eb0bb45c0d282a3f5e05ddf23705c3d13a95c55
- tree SHA-256. 1fedd0fde3891d9acd1ba40d034c57025244d43d6b42ce9a78e07c32d3d14ada
- evidence_after_deploy. Live: after the command and its providers are deployed, retain an admitted batch’s write, check, REQUEST_CHANGES, fix and ACCEPT rows with the same writer session and each reviewed head. Count orchestration turns under the CLI item.

## Verified premise

- The batch runner already schedules and replays write, check, review and fix and has a fake covering the full order. The native writer still owns a separate loop and both native entry points reach it. Keep the existing runner and wire contract. Replace the native loop in this item (`rust/pg-spawnd/src/step_runner.rs:169–218,356–380,474–525`, `rust/pg-spawnd/src/step_runner_tests.rs:150–189`, `rust/pg-spawnd/src/native_writer.rs:84–104,424–620`, `rust/pg-spawnd/src/native_writer_commands.rs:188–223,330–342`).
- The native review seat is already wired through `Driver::review`, including writer-family exclusions, repository tooling and exact-head witnessed verdicts. An empty route can defer to the batch seat, which explains a path out of the native loop, while live frequency and the supplied 207-minute observation are deployment evidence. (`rust/pg-spawnd/src/native_writer_host.rs:434–451`, `rust/pg-spawnd/src/native_writer_review.rs:319–396,483–534`, `rust/pg-spawnd/src/native_writer_review_route.rs:24–44`, `rust/pg-spawnd/src/native_writer.rs:539–545`).

## Decisions

- D1. Implement AGENT-STEPS, then ACTIONS-CLAIM-CI-TRAIN, then CLI-ENTRY. The first owns the agent adapter and transition replacement. The second composes those steps with shell and service actions. The third binds that composition to admitted inputs and durable replay. Only CLI-ENTRY also writes `step_runner.rs`, for the persistence hook, and only CLI-ENTRY also writes `main.rs`, after the actions module registration.
- D2. Put one shared agent phase machine in `step_runner_agent.rs`, exported by `step_runner.rs`. Give it Write, FastChecks, Review, Fix and Accepted phases carrying the head, session, stamp and review reference where applicable. Its transition methods take the typed completed outputs. Write and fix lead to checks, checks lead to review, and review selects Accepted or Fix. Batch `State::after` and the native driver use these same transitions. Put the run loop in this runner module as `drive` over a tick returning `ControlFlow`; both batch running and native `writer_attempt` enter it. Replace the loop at `rust/pg-spawnd/src/native_writer.rs:452–620` with a native phase dispatcher in `native_writer_step_flow.rs`. A dispatcher invocation handles one phase and returns the next phase or the terminal result.
- D3. Keep existing native entry points and records. The native dispatcher starts at Write after native admission, settles Accepted through the existing gates and optional native push, and uses the existing record sink. Its phase handlers retain same-session no-commit and failed-check repair budgets, base preparation and stale-base retry, review deferral, steering, push repair, quota and stop semantics. Keep the old branch inputs, retry counters and return codes in the same phase handlers. The native handlers can restart checks for existing exceptional branches. They use the shared phase transition for every ordinary verdict and review fix. Preserve write relaunch outside the dispatcher, saved/older receipt handling and round-progress stops. Native-only outcomes retain their existing exit and record behavior. (`rust/pg-spawnd/src/native_writer.rs:156–227,255–345,347–423,449–619`).
- D4. Implement `NativeAgentSteps<D: Driver>` over a borrowed driver and native record in `native_writer_step_actions.rs`. Share the existing timed writer call, review validation and finding-based follow-up prompt between the native dispatcher and the adapter methods used by the batch runner. For batch write run `execute(None, ...)` with the rendered role brief and return a committed head beyond the admitted base and the nonempty session. For batch fix require the exact current head and saved session, resolve the named REQUEST_CHANGES receipt, run `execute(Some(session), ...)` with its findings, and require a different committed head beyond base. Check the working tree and effective head before exposing the output. Expose these operations to the later shell composite. Keep writer turns and steering delivery in the record. (`rust/pg-spawnd/src/native_writer.rs:184–203,322–345,439–448,479–493,595–619`, `rust/pg-spawnd/src/native_writer_timing.rs:4–36`, `rust/pg-spawnd/src/native_writer_session.rs:128–140`).
- D5. For the batch adapter, call the existing native exact-head checks and expose the shared stamp path, with the local review stamp also required. Save the native `fast_checks` and `final_head` fields before review. Run one `Driver::review` for each batch review attempt. Map only exact-head ACCEPT or REQUEST_CHANGES with a valid review run ID and, for REQUEST_CHANGES, its findings text. Use the run ID as the contract reference and persist the normalized receipt at `batch-reviews/<run-id>.json` in the private run directory before returning. Record the same head, run ID and outcome in the native record and maintain its review list. Use the last completed REQUEST_CHANGES receipt from the step list for delta review and the exact referenced receipt for fix; validate its head and ID on every read. Deferral, pending, stale base and weak verdict are batch failures with their reason, while native compatibility retains the existing handling and review retries. A batch push precheck reuses final gates and requires an exact-head ACCEPT matching the supplied reference. Let StepLog own batch journal writes and keep native compatibility journaling through the record. Make `stage::record` skip the StepLog-owned stages for the batch-bound native record, retaining writer-turn timings and native-only bookkeeping. (`rust/pg-spawnd/src/native_writer.rs:347–423,497–554`, `rust/pg-spawnd/src/native_writer_stage.rs:7–31`, `rust/pg-spawnd/src/native_writer_host.rs:391–447`, `rust/pg-spawnd/src/step_runner_log.rs:21–29,71–89`).

## Entry points

- E1. Existing `native_writer::run` and `run_resume`, entering the runner’s phase machine through `writer_attempt`.
- E2. Batch `Runner::run` with the native adapter and fixture actions for the non-agent steps. CLI assembly is the next two items.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 E2 | The native path and full Runner share the same phase trace. The fake Driver sees write, check, review, same-session fix, check and review with the correct heads and previous findings. Acceptance reaches the appropriate existing push boundary once. | X1 X2 |
| I1 | E2 | Wrong-head review, absent evidence, deferral, changed session and unchanged fix head cannot complete a step or authorize push. | X3 |
| S1 | E1 | Native repairs, bounded retries, steering, base movement, saved resume, deferral, stop and quota retain their existing outcomes. The native entry points both reach the runner and the old scheduling loop is gone. | X2 H1 |

## Verification

- X1. `rust/pg-spawnd/src/native_writer_step_actions_tests.rs::batch_runner_uses_one_driver_session_across_review_and_fix`. Run the real Runner with the adapter, deterministic non-agent actions and a fake Driver with distinct committed heads. Assert row order, receipt IDs, exact checks, previous review contents and driver call arguments.
- X2. `rust/pg-spawnd/src/native_writer_step_actions_tests.rs::native_entries_use_runner_phases_for_requested_changes`. Drive both native entry points with the same fake, instrument the shared phase dispatcher in test builds, and assert the phase trace and settlement. Inspect the implementation diff for deletion of the loop at `rust/pg-spawnd/src/native_writer.rs:452–620` and a single mapping from verdict to phase.
- X3. `rust/pg-spawnd/src/native_writer_step_actions_tests.rs::batch_adapter_refuses_unbound_agent_evidence`. Exercise the production adapter with each I1 case and assert reason, side-effect count and absence of an accepting row.
- H1. Card heldout_check runs the existing `native_resume_route`, `native_writer_mcp` and `native_writer` integration targets. Keep the existing native unit fixtures and runner tests green.
- Write X1–X3 before implementation and record their initial red result. Report the full `pg-spawnd` suite with `--no-fail-fast` and the held-out result verbatim.

## Scope and reuse

- R1. Keep `BatchSteps` v1 and the remote `steps` CLI contract. Use the existing timed turn, role prompt, review receipt, native gate and run record helpers. Batch rehydration supplies the completed-step evidence to this adapter in CLI-ENTRY. The batch path may fail on stale base and leave its prefix intact. Native compatibility preserves its existing rebase and base-retry policy.
- R2. Move only native phase code and tests needed for the replacement. Add the item's changelog and an operations note explaining the two entry points and shared transitions. `docs/SPAWNER.md` is already at the effective 668-line cap (`tools/check_file_size.py:47–51,254–260`).
- N1. Outside scope are service transport, feeder placement, review routing changes, remote commands, wire schema and gate policy.

## Line budgets

Each write row caps the final physical line count and gives the count at the base commit. A shared row also gives the preceding item’s cap. Keep every writable file at 780 lines or fewer, and apply the documentation headroom check with `python3 tools/check_file_size.py --doc-headroom`.

## Open points for design approval

None.
