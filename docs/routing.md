# Implement route defaults (D-5206)

An implement card uses `cx-gpt-6.1-sol-xhigh` (model `omniroute/cx-gpt-6.1-sol-xhigh`,
effort `xhigh`), then `agy-gemini-3.8-flash` (agy harness). Quota, cooldown, or a
confirmed availability refusal advances without an override; safety refusals,
cancellation, and guards still stop dispatch. Eligibility and harness policy apply first.
Both designated writers precede all other cost tiers, even when catalog tiers
change: Sol xhigh, then Gemini Flash (rule 3.23a / D-5212). Other eligible legs
retain cost-tier order, with subscriptions before pay-as-you-go.
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

## Review cost tiers and retained registrations (D-5648)

`planner/review_order.py::review_key` already orders by the registered payment
tier, projected from an explicitly supplied catalog by `catalog_tiers`: free,
subscription, then pay-as-you-go. Measurements only reorder within one tier;
review seats consume that route. No second cost-order table was added.

DeepSeek's free OpenCode registration supersedes its D-4196 stop under D-5648;
the GLM D-4196 stop and all other existing disables remain. LongCat's free Go
route and Console OpenRouter's free route have independent families. Console
DeepSeek Flash is a paid direct-API route, not a free subscription benefit.
Big Pickle retains review. Every `gemini-4-argon` row remains registered for its
other roles; the review-only Go row is retained disabled with its reason.

The existing `routing.py::_NO_TRAIN_TAGS` / `route_card` privacy filter excludes
training legs for `risk_tags: ["secrets"]` (also auth, money and live-step).
OpenCode's [model privacy catalog](https://opencode.ai/docs/zen/#privacy) states
no prompt training for free DeepSeek and LongCat; Big Pickle may train. The free
OpenRouter route retains the conservative training flag. No privacy bypass was
added. `opencode/mimo-v2.6-flash-free` cannot be enabled in this registry-only
slice: `mimo_policy.py:7-23` permits only the two direct Mimo paths, and clients,
runner, catalog admission and leg loading share that guard. Changing it requires
a policy slice; free Mimo may train on prompts when that slice admits it.
The strict benchmark contract also remains fail-closed:
`tests/test_benchmark_data_contract.py:218` requires sourced rows for every active
leg, but has no rows for the re-enabled DeepSeek or these three new routes. Its
frozen debt ceiling cannot grow. This registry-only slice does not fabricate
measurements, extend that ceiling, or weaken the contract; benchmark validation
therefore remains blocked pending separately sourced benchmark evidence.

Deferred live proof commands (public prompts only; not executed in this slice):

```sh
opencode run --standalone --format json --model opencode/deepseek-v4.1-flash-free 'Reply OK.'
opencode run --standalone --format json --model opencode/mimo-v2.6-flash-free 'Reply OK.'
opencode run --standalone --format json --model opencode/big-pickle 'Reply OK.'
opencode run --standalone --format json --model opencode-go/longcat-2.5-free 'Reply OK.'
opencode run --standalone --format json --model console-openrouter/openrouter/free 'Reply OK.'
opencode run --standalone --format json --model console-deepseek/deepseek-flash 'Reply OK.'
```
