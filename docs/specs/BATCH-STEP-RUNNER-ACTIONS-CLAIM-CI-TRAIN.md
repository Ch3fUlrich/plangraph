# BATCH-STEP-RUNNER-ACTIONS-CLAIM-CI-TRAIN

Outcome. Compose keeper claims, exact-head CI classification evidence and verified train admission with the batch runner.

- plan_source. Keeper item plangraph:BATCH-STEP-RUNNER-ACTIONS-CLAIM-CI-TRAIN (2026-11-25), batch routing D-7038, priority 10.
- base commit. 506c0cf41b72cb3e5aab7a08a11dec45b8b6ed64
- card SHA-256. 653072243ba55b94d8ae1443040f5e3a192df77f1bd16682ed2a0aa5835bf0af
- tree SHA-256. fd922bf18d2a24a8c7206f563bef0516fb5134f676b2e2825acf5b391bbb9a2d
- evidence_after_deploy. Live: after the external providers and admitting-host command are deployed, retain the acknowledged epochs, exact CI run and attempt, and authoritative manifest containing the pinned part for one eligible batch.

## Verified premise

- The step runner and shell worktree, fast-checks and draft PR push exist. `Composite` forwards claim, CI and train join to the caller. The feeder already claims items individually and carries their epochs while it selects and launches. The keeper also has a batch claim verb with per-key results. (`rust/pg-spawnd/src/step_runner_shell.rs:72–195,199–239`, `rust/plangraph-cli/src/feeder/adapters.rs:218–238`, `rust/plangraph-cli/src/feeder/workflow.rs:29–105,248–288`, `src/plangraph/keeper/store_verbs.py:253–303`, `docs/KEEPER.md:385`).
- The named CI-RED-CLASSIFIER-AUTOFIX provider is absent from this tree. `plangraph wait ci` already filters verified non-test runs and waits by exact SHA, but its `Run` drops the attempt and its outcome treats skipped and neutral as green. The watcher routes failures. Draft lane CI alone supplies no test proof. (`rust/plangraph-cli/src/wait_ci.rs:15–24,36–72,139–200,249–296`, `rust/plangraph-cli/src/ci_watch/mod.rs:332–339,491–519`, `docs/ci-gate.md:9–20`).
- Landing train registration and building exist. The registrar computes READY eligibility, build consumes a preselected READY snapshot, and admit checks a review of the built train. The keeper journal prepares a whole train under a router lease. This tree exposes no single-part join returning a train and manifest for an exact lane CI tuple. (`rust/plangraph-cli/src/land/registrar.rs:138–192`, `rust/plangraph-cli/src/land/cli.rs:11–17,68–104,208–265`, `docs/landing-train.md:39–55`, `src/plangraph/keeper/store_merge_locks.py:161–226`).

## Decisions

- D1. Implement after AGENT-STEPS and before CLI-ENTRY. This item owns service requests, response validation and the composition seam. CLI-ENTRY also writes `main.rs` to register its command module. No feeder, keeper, workflow or landing source edits are part of this item.
- D2. Evolve the existing `Composite` to take separate agent and service ports. Define `ServiceActions` with claim, ci_class and train_join and `AgentActions<R>` with write, review and fix. Give the agent port worktree, fast_checks and push methods with defaults delegating to the supplied `ShellSteps<R>`, so integration can retain native admission and exact-head gates. Keep one `StepActions` implementation for composition. Give the service port `verify_claim`; the composite calls it before each action after claim, checking the worktree step’s claim reference as well. The real service renews the exact saved epochs and refuses the dependent action on any failed renewal. Caller ports in fixture-only tests can supply their own claim evidence. Default shell behavior remains callable. CLI-ENTRY supplies the native overrides.
- D3. Build `BatchServices` over bounded argv and JSON process calls. Use the existing `pg claim-batch --json KEY...` client, the same keeper authority as the feeder's individual claim. Require an exact ordered array of one result per input key, a positive epoch and a current lease for each successful row. Set the environment variable `PG_IDEMPOTENCY_KEY` of each `pg claim-batch` subprocess to `<run id>.claim.<attempt>`, which passes check_write_key (src/plangraph/keeper/api_routes.py:89-95, characters `A-Za-z0-9._-`, at most 128), so a retry after an unknown outcome repeats the same request and the keeper returns its first result (src/plangraph/keeper/cli_verbs.py:146-152). Every `pg heartbeat-batch` subprocess gets a fresh key of its own per call (`<run id>.hb.<unix ms>.<seq>`, seq a counter of the runner process that rises on every call), never an inherited or reused one, because heartbeat_batch is a retryable write too.
- D4. When `pg` exits 2 with a valid partial JSON result, treat that as a definitive partial new claim and save acknowledged epochs before compensating with `pg release --json KEY` and explicit `PG_EPOCH` for each acknowledged success. Persist failed releases and the refusal so retries cannot start a second claim. Recheck any outstanding compensation by that same epoch, recording stale or released as settled. For malformed or lost replies, retry the same claim request key and release no unacknowledged epoch. Persist indeterminate status in the run directory and never use a new key to work around it. On partial adoption, fail the step and leave feeder-held claims alone. Preserve successful claims through train handoff. Keeper submit, close and landing settlement keep their current owners. (`src/plangraph/keeper/cli_verbs.py:278–279,550–552`, `src/plangraph/keeper/store_verbs.py:409–430`).
- D5. The CI port sends one JSON object on stdin with `repo`, `head`, `pr`. The trusted installed executable returns the same tuple plus `classification` (green or red), positive integer `run` and positive u32 `attempt`, only for the current, completed, authoritative test attempt of the exact PR and head. The adapter parses the typed response strictly and validates the tuple and bounds before mapping it to `CiClass`. Give the command one 30-minute bound per runner attempt. Missing, pending, non-test, ambiguous and unavailable evidence, timeout and nonzero exit fail the step with a diagnostic. A valid red classification completes the row and the runner stops before train join. The absent authoritative classifier and red autofix are out of scope, so live CI classification requires that provider.
- D6. The train port sends `repo`, `batch_key`, `head`, `pr`, `run`, `attempt` as JSON on stdin. The trusted installed executable must authenticate the green tuple, perform or reconcile idempotent admission keyed by that tuple and batch, honor the existing train admission rules, and verify that the authoritative manifest contains that PR and head, and return the echoed tuple plus nonblank `train` and `manifest` references only after inclusion. Require an exact typed match and nonblank outputs before returning `TrainJoin`. Give the call a two-minute bound per attempt. Pending, left out, stale, malformed, unavailable and mismatch responses fail with an explicit reason. The missing single-part join provider is out of scope, so a deployed batch can reach Joined only when that provider exists.
- D7. Use absolute executable paths for the CI and train ports, no shell, in the admitted repository. Reuse `adapter::run_command` and expose its existing stdin sibling `run_command_input` to this module. Bound each keeper client call to 120 seconds. Report exit or timeout and safe diagnostics without relaying credentials, and require exactly one JSON response on stdout. The process fixtures replace these providers at the boundary. (`rust/pg-spawnd/src/adapter.rs:52–126`, `docs/batch-step-contract.md:9–25,43–45`).

## Entry points

- E1. Claim through `BatchServices` and the keeper CLI, with the feeder's already admitted epochs supported explicitly.
- E2. Runner through `Composite` with real service action methods and fake process transport in a temporary repository.
- E3. The CI and train process boundaries, plus red termination at the existing runner transition.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 E2 E3 | A fake keeper and CI provider run a batch through the real actions to a fake authoritative train manifest for the same part. Every forwarded tuple and claim receipt matches the list. | X1 |
| I1 | E1 | Partial, malformed, lost and replayed replies never publish a partial receipt or release an unowned epoch. Adopted epochs are validated and the claim is not repeated. | X2 |
| I2 | E3 | Exact red ends at CI; pending, unverified or wrong-identity CI and mismatched or unavailable join cannot manufacture a completed result. | X3 |
| S1 | E2 E3 | Replaying an uncertain join uses the same idempotency identity and only one inclusion can settle. | X4 H1 |

## Verification

- X1. `rust/pg-spawnd/src/batch_step_services_tests.rs::runner_claims_and_joins_the_exact_green_attempt`. Use the actual service adapters and existing runner/composite with fake command execution and deterministic Git fixtures. Assert the keeper request, ordered receipt, all stage rows and verified final tuple.
- X2. `rust/pg-spawnd/src/batch_step_services_tests.rs::claims_replay_or_compensate_only_acknowledged_epochs`. Cover replay after lost reply, partial response and failed compensation, attempted adoption with a stale epoch, successful adoption, and mismatched item order.
- X3. `rust/pg-spawnd/src/batch_step_services_tests.rs::only_bound_completed_ci_and_join_can_advance`. Exercise I2, plus malformed and zero numeric fields and timeout refusals. Assert the attempted step and exact absence of downstream effects.
- X4. `rust/pg-spawnd/src/batch_step_services_tests.rs::join_replay_keeps_one_admission_for_the_same_tuple`. Lose the first join response after the fake provider admits the part. Assert the exact same request on retry and the single authoritative inclusion.
- H1. Card heldout_check runs the existing step runner and worker-core wire contract targets. Write X1–X4 first and record their initial red result. Report the full `pg-spawnd` suite with `--no-fail-fast` and the held-out result verbatim.

## Scope and reuse

- R1. Only the production adapters and fixture side of missing interfaces are implemented here. Reuse the existing keeper CLI client's authentication, claim idempotency, lease checks and release fencing. Keep the runner's stored rows and stages as the evidence consumer.
- R2. Record the provider and claim contract in `docs/operations/batch-step-runner-actions.md` and add the item changelog. Keep provider values and credentials out of checked-in examples. `docs/SPAWNER.md` stays at its cap.
- N1. Outside scope are the provider implementations, CI autofix, draft promotion, workflow changes, settlement, keeper API changes and live feeder dispatch.

## Line budgets

Each write row caps the final physical line count and gives the count at the base commit. A shared row also gives the preceding item’s cap. Keep every writable file at 780 lines or fewer, and apply the documentation headroom check with `python3 tools/check_file_size.py --doc-headroom`.

## Open points for design approval

None.
