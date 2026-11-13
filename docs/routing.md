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

DeepSeek's free OpenCode review registration supersedes its D-4196 stop under D-5648;
the GLM D-4196 stop and all other existing disables remain. LongCat's free Go
route and Console OpenRouter's free route have independent families. Console
DeepSeek Flash is a paid direct-API route, not a free subscription benefit.
The retained `omniroute-deepseek-flash` now uses the harness-served
`omniroute/deepseek-direct-flash` id (model discovery, 2026-11-13) and the paid tier.
Console and OmniRoute DeepSeek remain the **same model family**, intentionally:
separate provider registrations must never masquerade as independent reviewers.
Big Pickle retains review. Every `gemini-4-argon` row remains registered for its
other roles; the review-only Go row is retained disabled with its reason.

The existing `routing.py::_NO_TRAIN_TAGS` / `route_card` privacy filter excludes
training legs for `risk_tags: ["secrets"]` (also auth, money and live-step).
OpenCode's [model privacy catalog](https://opencode.ai/v2/docs/zen#privacy) states
no prompt training for free DeepSeek and LongCat; Big Pickle may train. The free
OpenRouter route retains the conservative training flag. No privacy bypass was
added. D-2739 admits exactly `opencode/mimo-v2.6-flash-free` through the shared
Mimo policy; other unapproved provider/release paths still refuse. Both free
DeepSeek and free Mimo are review-only, not newly enabled writers. Mimo retains
`trains_on_data=True`. Its native free path must not inherit the direct Mimo
gateway renderer, reasoning controls or keyless-listing probe exemption.

The unchanged strict benchmark contract passes with exact, sourced identity
rows in `config/review_benchmarks.json` for all five activated review
routes and the corrected direct DeepSeek id. Publisher text, source URL and
collection date are retained. These are **identity-only admission records**:
capability, speed and task-cost measurements are unknown (`None`), not copied
from another provider or invented. They never enter the numeric SQLite benchmark
table or count as measured utility. Two resolved legacy waivers are removed;
the frozen debt ceiling and its independent fingerprint are unchanged.

### Witnessed public verdicts (D-2739, 2026-11-13)

Executed from a scratch directory, with no repository content attached:
`opencode run --standalone --format json --model MODEL --title TITLE PROMPT`.
The public prompt was: “Do not use tools. Review this public Python change:
def add(a, b): return a + b. It implements addition without other changes.
Respond with exactly VERDICT: PASS if correct, otherwise VERDICT: FAIL.”
OpenCode run identities (session IDs), final assistant text and finish reasons:
The retained public-only event excerpts are `tests/fixtures/review_free_verdicts.json`.
Session IDs below are displayed as two joined parts (remove the single space),
preserving the public run identity without misclassifying it as a credential.

| Model | Run/session ID | Final text | Finish |
| --- | --- | --- | --- |
| `opencode/deepseek-v4.1-flash-free` | `ses_e2c2431f1ffe ePDkBfAuwMQL4U` | `VERDICT: PASS` | `stop` |
| `opencode/mimo-v2.6-flash-free` | `ses_e2c2431c1ffe 7nJIHJjF01tvW5` | `VERDICT: PASS` | `stop` |
| `console-openrouter/openrouter/free` | `ses_e2c243102ffe uxUBbErvJsyaKg` | `VERDICT: PASS` | `stop` |

These are availability/verdict-format smokes, not reviews of this PR or capability
benchmarks. OpenRouter attempted a rejected `/dev/null` write before its final
assistant verdict; that tool argument is not counted as the verdict.

The subsequent **isolated spawner** verification failed closed: DeepSeek
`run-4100ebdb90934f1081e1f90f36a9bd1e` and Mimo
`run-ff163a6806c2442eac2b39e090f261e7` received HTTP 429 free-usage limits;
OpenRouter `run-11451f3d508a46829c2e5a5570b3c205` was
`not_served_on_host` under the child configuration. None is claimed as a passing
spawner run, and no quota, config isolation or host admission guard was bypassed.
`plan_spawn` already consumes stored failed-probe evidence; the shared real-launch
`resolver.plan_for_launch` performs served-model probing before child launch.
New fixed-input tests exercise both boundaries for every activated review route.

Other optional public smokes (not part of the three witnessed runs above):

```sh
opencode run --standalone --format json --model opencode/big-pickle 'Reply OK.'
opencode run --standalone --format json --model opencode-go/longcat-2.5-free 'Reply OK.'
opencode run --standalone --format json --model console-deepseek/deepseek-flash 'Reply OK.'
```

### D-2739 finding dispositions (seat order, duplicates retained)

| Finding | Disposition and guard |
| --- | --- |
| F1 HIGH live proof | Three native runs above retain session IDs, final assistant VERDICT lines and stop frames; isolated-spawner failures are disclosed separately. |
| F2 HIGH benchmark contract | Exact sourced identity rows cover all activated routes; unchanged contract and frozen fingerprint pass without invented numeric scores. |
| F3 HIGH OmniRoute model | Retained leg now names served `omniroute/deepseek-direct-flash`, paid; metadata test pins both. |
| F4 HIGH missing Mimo/policy slice | Free review registration and narrowly scoped shared policy implemented here under D-2739/D-5648. |
| F5 HIGH missing Mimo/live proof | Review-only free Mimo registration, conservative training flag and native verdict session retained. |
| F6 HIGH exact free Mimo path | Exact `opencode/mimo-v2.6-flash-free` admitted; other free Mimo releases still refuse. |
| F7 HIGH OmniRoute model duplicate | Served direct id corrected and tested, registration retained. |
| F8 HIGH benchmark rows | Re-enabled and new routes have sourced exact keys; independent ceiling unchanged. |
| F9 LOW mechanics fixture | Complete production table retained; mechanics lift only disables, never training flags or rows. |
| F10 LOW DEFAULT_LEGS override | Same fix, with exact all-registration order and production privacy assertions. |
| F11 LOW ambient route_card | Not applicable: `routing.py:104-106` has no policy or reader arguments and performs no ambient reads; explicit table and clock now pinned anyway. |
| F12 LOW Claude fence | Exception requires both missing diff and a large estimate; all small-estimate seats are fenced, including boundary tests. |
| F13 LOW dead active branch | Active state depends on explicit Argon parameter case, making both branches reachable. |
| F14 LOW retained-disabled Argon | Added Go Argon case asserts exact disable reason and empty roles. |
| F15 LOW W30 imported table | Patch precedes service construction; call-time spawnable table and exact refusal reason asserted. |
| F16 MED live proof | Native public proof retained; no failed isolated-spawner run is relabelled successful. |
| F17 MED OmniRoute base verification | Model corrected in production and pinned to discovered served id. |
| F18 MED Console own family | Invalid as an independence requirement: direct and free DeepSeek are the same family; `seats.py:65-67` must deduplicate them. Regression proves they cannot fill two seats. |
| F19 MED privacy expands writers | Free DeepSeek and Mimo are review-only; new-writer regression verifies absence even under neutral policy. |
| F20 MED pre-run probes | Already implemented at `resolver.py:500-520` real-launch boundary; planner consumes stored probe evidence at `routing.py:327-359`. New per-route tests cover both without network I/O in planning. |
| F21 MED Argon fixture substitution | Restored original Argon case for implement; retained Gemini Flash review case as additional parametrization. |
| F22 MED fabricated planner table | Complete table/roles/privacy retained and explicit exact expected order extracted into a test-only helper. |
| F23 MED note-only policy branch | Enabled review-only DeepSeek now exercises actual writer refusal and asserts no override audit; production-disabled GLM refusal retained. |
| F24 MED tier indices | `legs.py:31` includes trial, so old indices were correct; now additionally assert named free/subscription/paid tiers and two actual seats. |
| F25 MED W30 construction binding | Patch moved before construction, exact patched registration and refusal checked; GLM case still tests a production disable. |

Validation: the affected selector's changelog/shared-helper fallback was bounded
with `--within` to 45 lane, routing, policy, benchmark, isolation, renderer and
completion-evidence test files; every changed test file is included. With two
workers: `1364 passed in 28.09s`. A broad reverse-import trial was interrupted
after `668 passed, 4 skipped in 751.52s`; it is not claimed as a green gate.
The whole suite was not run. Argon transport/witness probes retain their exact
historical inputs through an explicitly named historical fixture, while separate
production tests prove the retained Go Argon id and model cannot launch.
