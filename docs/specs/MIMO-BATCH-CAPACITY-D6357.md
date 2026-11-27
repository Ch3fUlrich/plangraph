# MIMO-BATCH-CAPACITY-D6357

Restore a usable MiMo Pro review seat on the normal spawner route, with enough read and test access to complete one witnessed batch review.

## Binding

- D1. Protected order D-7020 rule 3.104(1) and D-7019 rule 3.103(2,4). Data commit `dfc9cc344799e37c5c4bccf15b9f6f3d7f600b22`. Source base `c3cdc1749e95eb7bd824fc799f8b82fa1e15efed`.
- D2. Signed D-7022 binds this one gpt-6.2-sol-high lane to MIMO-BATCH-CAPACITY-D6357 and SPAWNER-REVIEW-TOOLS. Edit the live Python apply_review_permissions in place as the sole renderer. RL28-RUST-SEAT-TOOLS-IN-LAUNCH remains its own port and uses this lane’s tests as parity. REVIEW-LAUNCH-REFUSES-A-CONFIG-WIDER-THAN-READ-ONLY remains separate hardening. Coordinate the existing commit order if hunks overlap. RL28-LIVE-OPENCODE-PROOF remains a separate item. The parent’s S1 supplies its live proof.
- D3. Design candidate. Card SHA-256 `792635ca7ac788ef0646c36cf2caec57667959fcc2ae7ddbc114401a7193c8dd`. Tree SHA-256 `ee8ebbc98fc8161447b14f6468227b66479bc2c0aaf12f201f4312538a589309`. Keeper has the design_ref. D-7022 is signed in fleet row 952. Protected publication of the ruling, the exact lane receipt and independent design acceptance are pending.
- D4. The existing policy parks MiMo in `review_tools_denied_legs`. Its declared mode is inline-only. `opencode_repository_review` currently selects the existing DeepSeek and GLM repository seats. Its renderer grants wildcard Cargo and pytest commands. That renderer alone does not prove tool confinement. `pg-jail` currently provides filesystem confinement under Landlock ABI V3.

## Entry points and ownership

- E1. Reuse `review_route_pool`, `RoutePolicy.skip_reason`, `plan_spawn` and `routed_review_legs`. Project MiMo as repository-tools only for the review role. Keep one leg identifier and use the existing registry/provider and quota records. In GPT-head fallback order, select available Hydrogen-high, then available MiMo Pro, then the eligible Sonnet route. Preserve family coverage and mandated specialist seats. Report unavailable isolation as a capability refusal before provider dispatch and continue normal fallback.
- E2. Reuse the detached exact-head checkout, SpawnerRunner.spawn, the standalone OpenCode adapter and existing isolated job overlay. Keep the sole permission renderer in apply_review_permissions and distinguish the current deny profile, existing repository seats and the bounded MiMo profile. Use the same final projection for CLI, API, direct, routed, forwarded and retry launches. MiMo grants checkout-scoped Read, Grep and Glob with the single mediated test entry. Keep project config, plugins, sharing, external directories and web tools disabled. Validate the actual checkout and effective MiMo profile at launch. Preserve the separate generic launch-hardening owner.
- E3. Keep model transport on the current provider route. The offline test child uses pg-jail with read-only source, job-owned build and scratch directories, a fixed executable and argv from a spawner-owned descriptor, and the installed no-socket filter inherited by descendants. The test child receives a scrubbed environment and closed non-stdio file descriptors. The placed host probes the trusted offline runner and actual checkout locally during launch preparation and refuses MiMo before provider dispatch when either boundary cannot be proven. Normal route fallback handles that refusal. Reuse this check at resumed or forwarded launch on the actual host.
- E4. Reuse the served-model witness and exact review-test receipt. Run the same brief and diff_ref as the first review on one real public head, through the spawner on an admitted host. Record head/base, model witness, tool calls, exact test result, VERDICT, diff bytes, wall time, finding overlap and permission-denial evidence. A missing provider route, timeout or absent witness is a named blocker.

## Acceptance

| ID | Entry | Evidence |
| --- | --- | --- |
| B1 | E1 | GPT-head route shows Hydrogen-high, MiMo Pro, Sonnet in order. Scenarios exercise both availability fallbacks, a failed isolation admission, and the existing family/specialist coverage. |
| I1 | E2 | Standalone launch carries only approved permissions. An injected extra grant, stale config, checkout path escape or unsupported host refuses before process creation. The existing denied profile and existing repository seats retain their behavior. |
| I2 | E3 | The named offline test succeeds on a fixture checkout. Negative checks prove checkout and outside writes, forbidden commands, outside reads and network access are denied. Enforce the same boundary for subprocesses. A failure to install the isolation returns a named refusal. |
| S1 | E4 | The real MiMo run reads the checkout, runs the fixed test and yields a witnessed verdict for the exact head. Record the before/after comparison without changing how a witnessed verdict is accepted. |

- X1 (B1/E1). `rust/pg-spawnd/src/launch_tests.rs::review_route_offers_mimo_pro_after_hydrogen` and `tests/test_route_policy_review_choice.py::test_gpt_review_order_hydrogen_mimo_sonnet`. Preserve the test for refusal on a host without the bounded profile.
- X2 (I1/E2). `tests/test_opencode_isolation_go.py::test_mimo_review_refuses_extra_tool_grants` and `test_mimo_review_isolates_final_permissions`. Drive real launch preparation before the fake process factory.
- X3 (I2/E3). `rust/pg-jail/tests/escape.rs::offline_review_enforces_readonly_and_socket_denial`. Use a synthetic checkout and a local socket sentinel. Include an allowed fixed command and a denied child socket.
- X4 (S1/E4). `tests/test_opencode_isolation_go.py::test_mimo_review_live` uses the real spawner, not a permission matcher. A named skip is reported if this host cannot serve. The item's Live evidence requires a successful run on a serving host.

## Scope and review

- R1. Review-tool admission changes a security boundary. Review the exact command matcher, read boundary, final configuration, scrubbed child and network enforcement before route activation. The configured model's existing provider access stays isolated from test tooling.
- R2. Keep routing.py within 800 lines and runner.py within its 1157-line baseline. Keep docs/SPAWNER.md at 668 lines or fewer with an in-place link and place the MiMo-specific detail in the existing companion operations page. New code belongs in Rust. Reuse the existing fixture modules and preserve each tool-policy negative case. RL28’s later Rust port reuses the positive and negative tool-policy suite as parity. The current file map remains proposed for exact review, including any gate grant routing requires.
- R3. Before DESIGN-OK, resolve the exact standalone OpenCode test invocation, descriptor lookup and tool-pattern semantics with the existing design seat. Prove that the test dispatch cannot traverse a general shell with the model environment or accept caller-supplied paths or arguments. Bind the host-local capability and refusal fixture to the same launch path. Obtain the complete single-lane budget finding from the seat record and confirm scope fit.
- N1. Keep unrelated legs, credential storage, service configuration and rollout ownership on their existing contracts. The parent carries the S1 witnessed live proof. Keep inline-only as the protected fallback decision if the default does not fit one safe lane. That fallback needs separate disposition of the required live Read and Test proof.
- Live. The executor runs one witnessed exact-head MiMo review on the approved host after protected deployment authorization. Publish sanitized run evidence and keep the parent open until S1 and the applicable part evidence pass.
