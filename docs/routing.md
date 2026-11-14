# Implement route defaults (D-5840; supersedes D-5206)

An implement card uses `cx-gpt-6.1-sol-medium` (model `omniroute/cx-gpt-6.1-sol-medium`,
effort `medium`), then `agy-gemini-3.8-flash` (agy harness). Quota, cooldown, or a
confirmed availability refusal advances without an override; safety refusals,
cancellation, and guards still stop dispatch. Eligibility and harness policy apply first.
Both designated writers precede all other cost tiers, even when catalog tiers
change: Sol medium, then Gemini Flash (rule 3.23a / D-5212). Other eligible legs
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
the current Sol medium id so an unavailable default table cannot resurrect plain Sol.

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

Route JSON and saved `chosen_leg` include `effort` (`medium` by default).
Operator leg JSON preserves its validated `effort` through the loaded leg, route
plan, saved `chosen_leg`, and attempt rows; omitted or explicit `null` effort
stays `null`. All other validated leg fields are retained too. The per-run
renderer declares cx effort models; see [SPAWNER.md](SPAWNER.md#card-driven-routing-core).

Effort admission reads the declared leg field, never a model-name suffix. Ember Max has null registered effort; its capability-based disable remains unchanged. A disallowed-effort leg is recorded as skipped and later legs are tried; an exhausted route refuses. Explicit higher effort still requires a matching card effort and decision ID.

## Opt-in order core (D-5696/D-5706)

`payment_order(..., order_class="standard"|"high"|"complex")` selects catalog
positions after registered role filtering; it never grants roles or capabilities.
The implement table lists Sol xhigh, Hydrogen high and non-Claude-Code Sonnet high.
Hydrogen high is table-only until its registration is admitted; the real registry
returns only Sol xhigh and Sonnet high (subject to upstream eligibility).
Review excludes writer families, requires diff <100k for Haiku, and needs opposing
eligible cheaper verdicts for Ember. Spec amendment: MiMo and both DeepSeek review
legs retain null registered effort, as does Haiku; requested medium/xhigh efforts
are deferred until authoritative benchmark coverage exists. Non-table registrations
are dropped; table identities with legacy models are refused. Six unknown-capability declarations
are explicit-only, permanently disabled and role-less; JSON overrides may not
omit windows, max_request_tokens or trains_on_data even when disabled.
Live default activation and admission migration belong to LIVE-FENCE, not this core.
No benchmark evidence is invented; benchmark rows, pending file and frozen ceiling
are unchanged, and the benchmark contract passes with the retained null efforts.
Ember, high/complex review-prefix and planning Fable positive paths are synthetic-only
tests of upstream admission, not real-registry coverage. Real-registry assertions
pin their absence and missing roles (plus permanent disables for declarations).
## DeepSeek review identity (D-5641, D-5643)

The `omniroute-deepseek-flash` subscription leg requests
`omniroute/deepseek-v4.1-flash`, not the obsolete `omniroute/deepseek-flash`.
The Go review leg already requests `opencode-go/deepseek-v4.1-flash`.
Registration is not host availability: the launch boundary checks the rendered
child config via `opencode models --standalone` and skips an absent id with
`not_served_on_host` before starting a harness run. The recorded local probe
does not yet list the corrected OmniRoute id; its provider declaration and
gateway and credential readiness remain a host-side operator request. Never replace
it with the distinct paid `omniroute/deepseek-direct-flash` route.

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

The shipped provider config and the pending benchmark inventory use the same `omniroute/deepseek-v4.1-flash` identity; the pending row explicitly records missing measurements. Historical fallthrough replay projects the legacy alias onto this registered identity.
