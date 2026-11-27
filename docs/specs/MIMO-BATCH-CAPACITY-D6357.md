# MIMO-BATCH-CAPACITY-D6357

Restore a usable MiMo Pro review seat on the normal spawner route, with enough read and test access to complete one witnessed batch review.

## Binding

- D1. Protected order D-7020 rule 3.104(1) and D-7019 rule 3.103(2,4). Data commit `dfc9cc344799e37c5c4bccf15b9f6f3d7f600b22`. Source base `c3cdc1749e95eb7bd824fc799f8b82fa1e15efed`.
- D2. One gpt-6.2-sol-high implementation lane. SPAWNER-REVIEW-TOOLS supplies policy and command acceptance. RL28-LIVE-OPENCODE-PROOF supplies the live check. Its existing open RL28-RUST-SEAT-TOOLS-IN-LAUNCH dependency needs a routing disposition before the live item can close.
- D3. Design candidate for canonical `docs/specs/MIMO-BATCH-CAPACITY-D6357.md`. Card SHA-256 `0b25a9183e03487e294158875f1ca5ce2a025f357bbe2809baa8f724d2d0f6d9`. Tree SHA-256 `2c084e13fda7cce1c35ece17f25bdba7aadecb77c404c7083067f760f4a75e08`. Protected binding, design_ref and independent design acceptance are pending.
- D4. The existing policy parks MiMo in `review_tools_denied_legs`. Its declared mode is inline-only. `opencode_repository_review` currently selects the existing DeepSeek and GLM repository seats. Its renderer grants wildcard Cargo and pytest commands. That renderer alone does not prove tool confinement. `pg-jail` currently provides filesystem confinement under Landlock ABI V3.

## Entry points and ownership

- E1. Reuse `review_route_pool`, `RoutePolicy.skip_reason`, `plan_spawn` and `routed_review_legs`. Project MiMo as repository-tools only for the review role. Keep one leg identifier and use the existing registry/provider and quota records. In GPT-head fallback order, select available Hydrogen-high, then available MiMo Pro, then the eligible Sonnet route. Preserve family coverage and mandated specialist seats. Report unavailable isolation as a capability refusal before provider dispatch and continue normal fallback.
- E2. Reuse the detached exact-head checkout, `SpawnerRunner.spawn`, the standalone OpenCode adapter and existing isolated job overlay. Use this same final projection on CLI, API, routed, direct, forwarded and retry paths. Default-deny tools and grant checkout-scoped Read, Grep and Glob. One fixed test shim takes zero caller arguments. Its admitted command comes from a spawner-owned, enumerated test descriptor. Keep project config, plugins, sharing, external directories and web tools disabled for this profile. Validate final merged tool permissions and the environment just before spawn, including unexpected grants, managed or caller config, path escapes and an unproven host. The job owns its disposable checkout, scratch, process identity and cleanup.
- E3. Separate tool execution from model transport. Keep model traffic on the existing provider route. Reuse `pg-jail` for the fixed test shim with the source tree read-only, a job-owned writable build root and scratch, and a scrubbed environment with no model credentials or inherited connections. Add a review-only Linux offline profile that fails closed unless the kernel installs a no-socket filter before command exec, inherited by descendants. The shim resolves a repository-approved executable and fixed argv without a shell. Apply the validated readonly policy and exact shim allow before enabling the route. Tests execute in that profile and return status and a bounded summary through the existing review output path.
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
- R2. Keep routing additions inside existing line limits and use the mapped docs in place. New code belongs in Rust. Reuse the existing fixture modules and preserve each tool-policy negative case. The current source scope is proposed for exact review, including any gate grant routing requires.
- N1. Keep unrelated legs, credential storage, service configuration and rollout ownership on their existing contracts. Keep inline-only as the protected fallback decision if the default cannot fit one safe lane. That fallback needs separate disposition of the required live Read and Test proof.
- Live. The executor runs one witnessed exact-head MiMo review on the approved host after protected deployment authorization. Publish sanitized run evidence and keep the parent open until S1 and the applicable part evidence pass.
