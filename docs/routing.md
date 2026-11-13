# Implement route defaults (D-5206)

An implement card uses `cx-gpt-6.1-sol-xhigh` (model `omniroute/cx-gpt-6.1-sol-xhigh`,
effort `xhigh`), then `agy-gemini-3.8-flash` (agy harness). Quota, cooldown, or a
confirmed availability refusal advances without an override; safety refusals,
cancellation, and guards still stop dispatch. Eligibility and harness policy apply first.
Both designated writers precede all other cost tiers, even when catalog tiers
change: Sol xhigh, then Gemini Flash (rule 3.23a / D-5212). Other eligible legs
retain cost-tier order, with subscriptions before pay-as-you-go.
Writer and review payment ordering uses one classification function for the
payment class of raw and catalog-projected legs, treating muse-amber as paid:
after the implement writer prefix (Sol, then Gemini Flash), no pay-as-you-go leg
precedes a subscription-class leg, and muse-amber precedes paid DeepSeek.
The designated writers keep that prefix even when catalog tiers reprice them as
pay-as-you-go; review has no writer prefix. A trial or free slot keeps its position
unless it is a writer-prefix leg; a tier-less or id-less leg never raises.
This pure ordering core does not check serving state or change admission.
Main's registered implementation fallbacks retain their roles, permanent disables,
expiry/re-probe behavior and benchmark ranking within the remaining cost tiers.
Exhausting all eligible legs still yields no route; writer priority never bypasses
availability. Role-neutral keeper readiness retains its narrower admission, using
the current Sol xhigh id so an unavailable default table cannot resurrect plain Sol.

Plain Sol and other Sol effort variants remain active, explicit-only catalog legs
requiring audited overrides. Claude/Fable/Opus/Sonnet/Haiku never enter automatic
implement routes or fallbacks; explicit picks are unchanged. No legs are deleted.
Automatic implement eligibility wraps canonical family detection with implement-only
Anthropic/Fable aliases for the leg id, family, model and harness, including effort-bearing aliases even
when `claude_family` is false or the model is renamed. Unidentified families fail
closed; a false legacy flag never overrides Claude identity. All catalog rows are
regression-tested. Shared scoring, review and writer-family fences retain their
existing resolver behavior. Route JSON exposes `excluded_family` in skipped and
candidate rows; CLI stderr names each skipped leg, including when no route remains.
An opaque alias for a registered model may use that registration's family proof
(for example, Big Pickle is GLM); a Claude identity still always vetoes admission.

Route JSON and saved `chosen_leg` include `effort` (`xhigh` by default).
Operator leg JSON preserves its validated `effort` through the loaded leg, route
plan, saved `chosen_leg`, and attempt rows; omitted or explicit `null` effort
stays `null`. All other validated leg fields are retained too. The per-run
renderer declares cx effort models; see [SPAWNER.md](SPAWNER.md#card-driven-routing-core).

## Free review legs (D-5743, slice 1; scope D-5784)

OPERATOR D-5648 (confirmed by D-5768) lifts the GLM review fence for
`opencode/big-pickle`. The existing `review_allowed_families` exception in
`route_policy.json` now includes `glm`; there is no model-specific policy code.
Other GLM registrations retain their permanent disables. Implement/orchestrate
remain off, and paused-harness, no-verdict and writer-family fences still apply.
Eligible free reviews precede subscription and paid legs with or without benchmarks.

Deferred to slice 2 by D-5784: `opencode-mimo-v2.6-flash-free`,
`opencode-go-longcat-2.5-free`, and `console-openrouter-free`. No sourced benchmark
rows or independent harness/window/request-limit/tool-mode/note evidence exist
in the repository for these paths. Their registrations and positive routing
expectations are removed rather than copying Big Pickle metadata or inventing
provenance. Free MiMo remains refused by generic path and catalog filters; any
future exception must bind the exact review leg, not arbitrary roles or rows.
No paid leg, Argon role or keeper-readiness admission changes here.

D-5768 explicitly permits unknown privacy (`null`) for ordinary review diffs
until sourced classification in slice 3. It is not a no-training claim: risk
tags and secret/auth/gateway/site-data paths reject anything other than explicit
`false`, with `trains_on_data` skips. Both `paths` and `write_set` are checked.
The only runtime reader of `Leg.trains_on_data` in `src` is `route_card`;
`plan_spawn` uses that same fail-closed check. The strict benchmark contract,
seed, frozen ceiling and pending list are unchanged. Offline tests do not prove
serving availability or live verdicts (slice 4). Other historical test names are kept.

`test_gpt_review_lists_big_pickle_free_leg_before_every_costlier_leg` checks
Big Pickle's free-tier ordering with and without measurements and explicitly
excludes all three deferred legs from the route.
`test_cli_gpt_review_lists_big_pickle_and_omits_three_deferred_free_legs` checks
the same ordering and exclusions in CLI route JSON.
The registry test's `without_d4196()` fixture deliberately reactivates the old
Zen GLM leg; its historical expectation is correct only for that fixture.
`test_route_skips_banned_and_off_families` also checks unmodified `DEFAULT_LEGS`:
only Big Pickle is offered for GLM, while Zen/Go report D-4196/D-3605 disables,
matching the live-policy siblings. Before this clarification, all four files
(`test_review_free_legs`, `test_registry_route`, `test_review_legs_d3552`, and
`test_route_policy_review_choice`) passed together: `70 passed in 19.01s`.

## Replan-0438 CI reconciliation

All existing test names and #763 checks remain. The lane diff was searched for
the same role-stripping and two-writer-only assumptions; its registry, policy,
expiry, benchmark, CLI, fallthrough and explicit-leg tests now retain all #763
inputs instead of deleting their implementation roles to enforce priority.

- `test_strict_names_missing[None,data1]`: D-5206/D-5212 replace the missing
  first writer's name with Sol xhigh, not OpenRouter. Assert every missing active
  implementation registration, including filtered inputs, is still reported.
- `test_all_legs_unavailable_gives_no_route`: unchanged #763 expectation; fix
  readiness's stale plain-Sol id so the unavailable default table exhausts.
- `test_keeper_readiness_does_not_enable_implementation_fallbacks[gate_card,route_for_card]`:
  retain Gemini-first keeper admission and paid fallback isolation. D-5206 replaces
  only the admitted Sol id; D-5212 replaces OpenRouter-first worker priority with
  Sol xhigh then Gemini. Paid DeepSeek still ends the worker route.
- `test_each_role_falls_through_failed_and_limited`: unchanged #763 expectation;
  recover registered Big Pickle's GLM proof without accepting unknown families.
- `test_missing_legs_registered_for_utility`: all seven parameter cases unchanged;
  restore the missing implementation roles, model ids and permanent-disable rules.
- `test_implement_order_unchanged_by_benchmark`: D-5212 replaces global tier
  monotonicity only for the Sol/Gemini prefix; all remaining tiers stay monotone,
  measured utility still applies, and the eligible set stays identical.
- `test_running_records_release_every_attempt_job`: unchanged assertions; the
  second writer is now agy, so its fake child must emit a valid success frame.
- `tests/test_spawner_prompt_argv_limit.py::test_real_review_child_reaches_leg`
  (both sizes): left untouched as the separately owned, known main-red failure.

Validation: affected selection bounded to all lane test files and routing,
liveness, placement and utility consumers: `1312 passed in 137.05s (0:02:17)`.
Postgres planner-service, round-three, catalog, watchdog, leg-state and expiry
files: `139 passed in 63.38s (0:01:03)`. The untouched argv-limit file reproduces
`2 failed, 44 passed in 5.38s`, both at its digest-versus-redaction assertion
on line 57. The full suite was not run, following the brief's final prohibition
on whole-suite runs; no test was skipped or weakened to hide those failures.
