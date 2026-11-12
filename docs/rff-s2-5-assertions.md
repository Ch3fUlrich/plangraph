# RFF S2-S5 assertion reconciliation

D-5092 removes single-writer and subscription-before-free pins; D-5113 adds
registered utility inputs; D-5114 requires explicit old -> new expectations.
The existing test names remain, including historical names. No test was removed
or newly skipped. Main's safety exclusions, review-family fences, scoped provider
evidence, explicit effort variants, and generic utility/epsilon algorithm remain.

## Merge round replan-0438 (authoritative)

The four planner-service failures are a lane admission regression, not S1 tier
propagation: main's `b619bf1e` passes the whole file unchanged. S5 removed the
D-3828 transformation of `DEFAULT_LEGS`, admitting the implement-only OpenRouter
and paid DeepSeek rows to role-neutral keeper routes as well as worker routes.
`READINESS_LEGS` retains that existing transformation for role-neutral calls
using the default table; role-bearing calls and custom tables retain the slices.
The common `route_card` boundary covers gate, route-for-card and shadow callers,
without adding a preference list or changing the cost-tier algorithm.

- `test_ready_card_moves_to_ready`: unchanged `("agy", "free")`; readiness must
  preserve main's admission, not silently enable implementation-only fallbacks.
- `test_route_for_card_prefers_qodercli`: unchanged `("agy", "free")`; the
  historical name still verifies disabled Qoder yields to free Antigravity.
- `test_exact_tier_checks_inject_leg_table[gate_card]`: unchanged injected-table
  and no-ambient-catalog assertions, including the readiness expectation above.
- `test_exact_tier_checks_inject_leg_table[route_for_card]`: unchanged input and
  catalog assertions, including the route-for-card expectation above.
- Added `test_keeper_readiness_does_not_enable_implementation_fallbacks` for both
  entry points: exact Gemini first, only Sol remaining implement-only, explicit
  worker routing still OpenRouter first and paid DeepSeek last. Both cases failed
  first on `openrouter-free` versus `agy-gemini-3.8-flash` before the code fix.

Verification: main's unchanged planner-service file: `20 passed in 44.19s`;
lane before fix: `4 failed, 16 passed in 56.15s`; new regressions before fix:
`2 failed in 3.88s`. After fix, the whole file: `22 passed in 45.52s` (SQLite),
`22 passed in 30.91s` (isolated Postgres). The affected selector conservatively
chooses the full suite for the existing lane fragments/shared fixture; the run
was explicitly bounded to all lane test files plus routing/shadow/liveness
consumers, never the full suite: `771 passed in 89.32s (0:01:29)`. All test names
from main and the four slice heads remain; no existing planner-service test or
helper changed.

Main's reviewed S1 tier propagation, free-first ordering and placement are retained:
provider-qualified catalog identities beat unrelated bare names; partial review
measurements stay ahead of missing rows inside their own tier; Haiku cannot cross
a cheaper tier; writer preferences break only unmeasured same-tier ties. No new
preference table or benchmark row is introduced. The `placement` JSON field and
dell-first placement path remain unchanged.

Main's free Gemini Flash tier, GLM family for Big Pickle, automatic Muse admission,
`agy-claude-opus` registration and explicit effort aliases remain intact. S2-S5
adds the high/medium Opus and personal Argon registrations, finite operator-stop
expiry, qualified benchmark lookup, measured implementation utility and exhausted
provider fallthrough. Permanent Zen disables retain their exact reasons.

All tests from both parents remain by name. Exact lists now include main's free
Gemini and admitted Muse; GLM policy also excludes Big Pickle. Review-seat checks
retain main's exact Gemini/MiMo with diff and Gemini/Sonnet without diff at every
tested estimate. Historical Haiku-across-tier and bare-catalog assertions now pin
main's stricter cost and provider-identity boundaries. Legacy resolver availability
keeps main's disabled-reader behavior; live-authority readers still admit only ok.
Failure evidence retains main's six-hour expiry, including unknown failure sources.
A probe predecessor must still be fresh when a new failure books down, but its
later expiry does not erase a valid down streak before the newest failure expires.

The line-set files match main byte-for-byte: this lane has no independent added
lines. Deleted Cargo mappings and retired Python waivers remain deleted. Repeated
test imports and duplicate helper assertions were consolidated without losing any
assertion coverage or test name to satisfy existing file-size limits.

## Fix round s763split11b664 (historical; superseded above)

The merge from `origin/main` completed without conflicts. Both retained Zen free
legs now keep main's exact permanent reason:
`D-4196 opencode zen never worked, use opencode go; never remove (D-3828)`.
Neither acquires an expiry or becomes override-eligible. The enabled Argon and
Big Pickle registrations, GPT-OSS, Claude review/orchestration registrations, and
explicit effort variants remain. Free-first selection is not permission to
lift a permanent disable.

The historical assertion ledger below records the prior consolidation at
`11b6642f`; its enabled-free-DeepSeek expectations are superseded as follows:

- The neutral default implementation route (`WRITERS`) starts OpenRouter,
  Big Pickle, then Sol/Gemini/Argon/MiMo/gateway DeepSeek subscriptions and the
  paid direct DeepSeek fallback. Exact mechanics tables explicitly enable Zen
  rows to retain every slice's free-tier quota/fallthrough regression.
- Default review starts personal free Argon, Big Pickle, then subscription
  Gemini. Both Zen rows have `disabled: D-4196` skips, never a successful
  override; both override-refusal assertions are restored. Historical registry
  policy tests explicitly lift D-4196, so they still pin `banned_family` and
  `off_family` rather than conflating policy with production disables.
- The raw declared leg tiers are compared with an independent expected id/tier
  table, not `route_card`'s sorted output. Review-seat Claude exclusions are
  restored alongside the distinct-family/GPT fence. Main's exceptional 100k
  no-diff fallback remains explicit: Gemini Flash plus a Claude subscription
  seat (Sonnet in the consolidated registry); other seats must be non-Claude.
- `plan_spawn(catalog_rows=...)` pins both admission and tier projection; absent
  an explicit snapshot, one `get_catalog_models()` result is reused throughout
  a plan. That resolver currently defaults to a seeded in-memory catalog, not
  the host file. Exact planner, free-first, spawn-plan, W11 and round-three probes pin
  empty catalog rows to exercise their declared leg tables; shared test setup
  also isolates `PLANGRAPH_CATALOG_DB` and the legacy default path so CLI
  benchmark discovery and child processes never consume host measurements.
- Six-hour expiry is applied after source/status classification. Successful
  evidence stays healthy, known stale limit/probe failures request reprobe,
  and unknown evidence is not relabeled by a generic early return. Resolver
  availability now shares limited/down/expired exclusion with liveness.
- Legacy reset coverage changes the recorded 24h reset to seven days, asserts
  the stored value changed, and checks the six-hour boundary and evidence.
  Fallthrough assertions pin the manual Muse `off_family` skip instead of
  filtering non-dict records. Round-three `replace` and `plan_spawn` already
  had module-level imports; all three role fallthrough cases run unchanged.
- Duplicate fragment text is merged into one uppercase file per slice, with
  historical probes labeled as observations rather than current eligibility
  or benchmark claims. The tracked-tree regression refuses case-only path
  collisions, and slice-fragment checks allow eventual changelog folding.

No benchmark row, preference list, test removal, or new skip is introduced.

## Historical consolidation ledger (superseded where noted above)

The changes below compare the original consolidation against `origin/main`,
including assertions delivered by the four slices. Historical `WRITERS` was
the concrete unmeasured neutral-policy
route in `tests/test_route_free_first.py`: free DeepSeek, OpenRouter, Big Pickle,
Sol, Gemini Flash, personal high-effort Argon, MiMo Flash, MiMo Pro,
gateway DeepSeek Flash, paid direct DeepSeek. Muse stays
registered but manual-only; GLM stays disabled. Benchmarked routes may reorder
within each cost tier, with missing rows last inside that tier.

## Free-first contract: `tests/test_route_free_first.py`

- `test_writer_order_sol_family_excluded`: no writer without GPT -> free
  DeepSeek leads and GPT is absent, with its `excluded_family` skip retained.
- `test_writer_order_privacy_excludes_training_sol`: Sol/empty private route ->
  explicit non-training free DeepSeek leads both public and private probes;
  training Sol remains excluded with `trains_on_data`.
- `test_cli_route_implement_gpt_first_no_claude_writer`: Sol-only, no Claude
  writers -> concrete non-Claude `WRITERS`; Sonnet and high-effort Opus remain
  registered for review, never automatic implementation.
- `test_route_and_refusal`: Sol model -> free DeepSeek model; missing-card
  refusal and complete JSON-shape assertions are unchanged.
- `test_review_mimo_first`, `test_cli_route_review_mimo_first`: MiMo/Luna then
  subscription Gemini with disabled Zen -> personal free Argon, free DeepSeek,
  Big Pickle; DeepSeek is enabled and subscription Gemini follows at index 3.
- `test_policy_banned_free_legs_stay_skipped`: Sol-only implement and MiMo-led
  review -> concrete `WRITERS` minus banned DeepSeek inputs; review starts Argon
  then Pickle, with DeepSeek `banned_family` and GLM's permanent D-3605 stop.
- `test_agy_skipped_while_out_of_quota`: Sol-only -> `WRITERS` minus limited
  Gemini Flash; its skip reason remains `limited`.
- `test_route_sol_unavailable_keeps_tier_writer_order`: no remaining writers ->
  concrete `WRITERS` minus Sol, retaining its `down` evidence.
- `test_fixed_review_order`: MiMo/Gemini/Luna-style pin -> fixed-table free
  Qwen and free inputs before Gemini, MiMo, paid DeepSeek, Haiku last.
- `test_no_anthropic_implement_leg_removes_sonnet`: no Claude implementations ->
  no Claude implementations retained; Sonnet serves review/orchestration and
  high-effort Opus review only. Removed console Sonnet is still absent.
- `test_fixed_implement_sol_across_tiers`: Sol always first -> exact trial,
  free, subscription, paid permutations; unavailable Sol is removed from each.
- `test_implement_lead_independent_of_writer_order`: Sol survives legacy order
  mutation as pinned lead -> free DeepSeek leads independently of that constant.
- `test_writer_order_d2683_implement_first_seven`: eight Sol-first inputs ->
  concrete free-first mechanics route including GLM, new registrations, and the
  paid direct fallback. The forwarding routing test checks the same contract.
- `test_main_policy_gpt_first_and_retained_routes`: Sol-first implementation,
  MiMo-family review pins, old registry set -> OpenRouter leads under the real
  bans; review leads with personal Argon; the exact registry adds both Opus
  efforts, gateway DeepSeek/Muse, personal Argon, Pickle, and main's GPT-OSS.

## Registry, windows, and routing mechanics

`tests/test_planner_routing.py`:

- `D25_IDS` equality and dependent `test_default_legs_exact_d25_order`,
  `test_with_measured_overrides_by_id_and_ignores_unknown`,
  `test_small_estimate_goes_128k_qodercli_first`,
  `test_over_leg_max_request_drops_leg`, and
  `test_no_estimate_or_no_leg_raises`: old registry/order -> exact combined
  registry and exact cost-sorted route, including all new inputs; complete
  unavailability still refuses and request-limit exclusions are unchanged.
- `test_default_legs_exact_d25_order`: raw table tier monotonicity -> routed
  tier monotonicity; append-only registrations need not be tier-sorted in source.
- `test_90000_goes_256k_and_only_legs_with_that_window`: old 256k list -> add
  personal high-effort Argon and high-effort Opus with their declared windows.
- `test_risky_tag_removes_trains_on_data_legs_with_reason`: four training
  exclusions -> six, including Pickle and personal free Argon; no trainer routes.
- `test_writer_order_d2683_unavailable_skipped`: eligible Sol leads -> eligible
  free DeepSeek leads; limited Gemini remains absent.
- `test_writer_order_d2683_review_order_unchanged`: subscription Gemini -> free
  personal Argon, without weakening the inline-diff gate.
- `test_writer_order_d2683_writer_family_exclusion`: Gemini/Sol-first and
  Sol-only implementation -> Argon/free DeepSeek-first and exact `WRITERS`;
  explicit excluded families still do not route.
- `test_implement_benchmark_highest_cx_still_writer_order_and_skips_limited`:
  Sol-only -> exact free-first combined mechanics route minus limited MiMo Pro;
  measured Sol cannot cross the free/subscription boundary.
- `test_no_implement_plan_leg_has_claude_family`: zero Claude implementers and
  two distinct non-writer review families remain required at every tested
  estimate/diff setting, including the consolidated registrations.
- `test_alternate_limit_lookup_restricted_to_agy_harness`: Qwen/Sol lead ->
  personal Argon/free DeepSeek lead. The important assertions are unchanged:
  Qwen does not inherit agy-prefixed evidence, Gemini does, and provider-stop
  fallback skips only the appropriate harness-qualified model.

`tests/test_spawn_plan.py`:

- `test_writer_live_limit_reset_and_scope`: Sol -> free DeepSeek as first
  model; free DeepSeek -> free GLM as mechanics fallback. Evidence lookup now
  names that first free leg; account isolation and reset-boundary checks remain.
- `test_seats_and_impossible_fence`: Qwen then Gemini -> Gemini then Qwen;
  Haiku-only Claude registry -> Sonnet, both Opus efforts, Haiku. The impossible
  fence also excludes new Pickle, preserving its original one/no-seat purpose.
- `test_operator_order_and_roles_roundtrip`: Sol-only implement list and old
  review list -> exact combined role lists; JSON serialization still roundtrips.
- `test_benchmark_within_tier_only`: Sol-first eight-leg `operator-order` ->
  exact utility-ranked combined list, Pickle first and paid DeepSeek last,
  `p_source=benchmark`; absent data still reports `no benchmark p`.
- `test_orchestrate_role_routes_opencode_first`: MiMo Pro then Sonnet -> free
  Argon, MiMo Pro, gateway DeepSeek, Sonnet, medium Opus; invalid roles refuse.
- `test_agy_leg_models_use_agy_model_ids`: old exact native-id mapping -> add
  personal Argon and both Opus efforts, preserving main's GPT-OSS spelling.

## Availability and safety policy

- `tests/test_leg_expiry.py::test_d3605_sol_routes_implement_never_review`:
  Sol first -> free DeepSeek first; Sol remains implementation-only.
- `test_load_legs_ignores_legacy_expires`: discard all legacy expiry values ->
  preserve `None`/finite timestamps and reject strings, booleans, NaN/infinities.
- `test_no_local_state_in_leg_definitions`: ban every expiry mention -> allow
  only the intentional operator-expiry field; provider availability fields stay
  prohibited. The unchanged guard red cases still detect injected local state.
- `test_config_row_schema_has_no_local_state`: no expiry schema field -> expiry
  is optional, never required; provider-state fields remain rejected.
- `tests/test_leg_claude_haiku.py::test_leg_present_review_only_and_last`:
  no expiry attribute -> `expires is None`; role, harness, family, placement stay.
- `tests/test_reconcile_leg_key_fixture.py::test_reconcile_leg_key_fixture`:
  exact legacy fixture shape -> exact shape plus default `expires=None`; all
  harness-qualified key expectations remain unchanged.
- `tests/test_opencode_isolation_go.py::test_disabled_and_fallback_routes`:
  both free Zen legs have D-4196 stops -> free DeepSeek is active in both roles,
  GLM has its D-3605 stop; unverified gateway Gemini remains unroutable.
- `tests/test_leg_adapter.py::test_catalog_policy_is_not_mutated`: exact old
  catalog order -> same relative order plus the seven combined registrations;
  equality before/after availability reads and no-provider-read checks remain.
- `tests/test_registry_route.py::test_route_skips_banned_and_off_families`:
  empty implementation skips, Sol-only, both Zen overrides disabled -> explicit
  DeepSeek bans, GLM stop, OpenRouter lead, disabled GLM override still refused.
- `test_route_skips_paused_harness_per_role`: Sol lead, every opencode skip is
  `paused_harness`, no remaining writer -> OpenRouter lead, explicit permanent
  GLM/manual Muse reasons take precedence, two eligible Gemini agy writers remain.
- `test_explicit_policy_argument_wins`: policy enables GLM -> policy enables
  free DeepSeek, but cannot bypass GLM's permanent operator stop.
- `test_claude_written_high_gets_luna_and_qwen`: Qwen/MiMo -> Qwen/free Argon.
- `test_default_policy_seats_deepseek_for_review`: MiMo first -> free Gemini
  first; lifted DeepSeek review eligibility remains checked.
- `test_seat_excludes_every_writer_family`: Gemini/MiMo and old coverage ids ->
  Gemini/Pickle and their concrete coverage ids; keeper coverage remains empty.
- `test_uncoverable_parts_are_an_error`: ban all old alternatives -> additionally
  ban new Pickle so the original uncovered-part/error assertion stays meaningful.
- `tests/test_registry_seats.py::test_keeper_writer_names_exclude_their_legs`:
  strict writer-name parsing of every reviewer -> normalized reviewer-family
  comparison, including catalog-only Pickle; unknown writer rejection stays.
- `test_inline_failure_does_not_decide_a_launched_run`: first tools-mode free
  opencode leg -> tools-mode agy leg, which does not inline the deliberately
  invalid diff. The original launched-run/limited outcome assertion stays.
- `tests/test_review_legs_d3552.py::test_implement_route_still_skips_banned_and_off`:
  empty skips/Sol-only -> explicit DeepSeek/GLM reasons and OpenRouter lead;
  banned-family, review-only-role and disabled-override checks remain.
- `tests/test_seat_findings_last.py::test_disabled_override_matches_runner`:
  select a D-4196 row -> select permanently disabled GLM; identical service and
  runner refusals and absence of override audit artifacts remain checked.
- `tests/test_writer_disabled_d3828.py::test_disabled_writer_refusal`: every
  non-Sol leg -> every registered non-implementation leg; role-ineligible
  overrides still refuse. `test_sol_only_and_no_fallback`: Sol-only -> exact
  `WRITERS`; an all-limited reader still leaves no eligible writer.

## Benchmarks and fallback integration

- `tests/test_bench_required.py::test_absent_explicit_operator_order`: gaps equal
  routed writers -> gaps equal all active registered implementation inputs,
  including policy-filtered Muse. `test_strict_names_missing`: missing Sol ->
  missing free DeepSeek. Strict errors still name the absent first input.
- `tests/test_spawner_cli_catalog.py::test_missing_legs_registered_for_utility`:
  S3's exact roles -> review/orchestration for Sonnet, review only for high-effort
  Opus, and implementation/review/orchestration for gateway DeepSeek/Muse,
  retaining exact model, active, family, harness checks.
- `tests/test_spawner_host_bench.py::test_review_route_uses_benchmark_p`: old
  three-subscription route -> exact free-first combined route including personal
  Argon, Pickle, gateway DeepSeek and main's GPT-OSS.
- `test_implement_order_unchanged_by_benchmark`: benchmark ignored and identical
  order -> benchmark applied, same eligible ids, and monotone cost tiers.
- `tests/test_spawner_paid_last.py::test_only_sol_writes`: Sol-only -> complete
  concrete combined implementation role list, including disabled GLM; legacy
  constant and retained free/paid fallback rows are still checked.
- `tests/test_spawn_cli.py::test_replan_and_all_limited`: eight attempts,
  Sol/DeepSeek/GLM/OpenRouter first -> eleven eligible attempts plus manual Muse
  skip, DeepSeek/GLM/OpenRouter/Pickle first. The success-spare assertion selects
  Pickle rather than a raw index; all-limited replan retains explicit Muse skip,
  provider evidence, exit 4, and no extra launches.
- `test_transport_only_fallthrough`: eight -> twelve recorded inputs; successful
  spare reasons apply to routed leg records, not the separate manual-policy
  skip. Exact one/two actual launches and captured-evidence checks are unchanged.
- `tests/test_w11_selection.py::test_tier_first_and_swap`: pinned Sol-first list
  unaffected by metric swap -> exact utility order inside cost tiers and a
  concrete reordered free tier after swapping two free rows.
- `test_candidate_json_and_limit`: Sol lead -> measured free GLM lead while the
  missing free row sorts last in its tier; metrics, reason and evidence checks stay.
- `test_default_database_cli_mcp`: Sol/`operator-order` -> Pickle/`benchmark` in
  both interfaces; registered disabled inputs may have prior rather than measured
  metadata, while measured candidates still expose floating utility values.
- `tests/test_w30_regressions.py::test_free_zen_legs_are_exactly_the_disabled_set`:
  two disabled Zen rows -> exact four-row map, with DeepSeek/Pickle/Argon enabled
  and GLM permanently stopped. `test_explicit_disabled_zen_leg_refused`: both
  refused -> GLM refused without launch, enabled DeepSeek dry-runs successfully.
- `tests/test_route_claude_effort_legs.py::test_route_lists_them_as_explicit_only`:
  legacy Sol lead -> free DeepSeek lead; the complete explicit-only id set remains
  unchanged. `test_other_writers_still_refused`: former role-ineligible Gemini
  -> still-review-only GPT-OSS, preserving the role refusal contract. Qualified
  effort-model aliases still resolve to main's explicit variant, not shared defaults.
- `tests/test_w9_regressions.py::test_mid_fallthrough_bench_refusal`: every
  skipped attempt is unlaunched -> manual Muse retains its `off_family` reason,
  every routed spare remains `run_terminated_before_leg`; strict benchmark
  refusal type, evidence, exit status, and exactly one launch remain checked.

## CI fix-round safety regressions

`tests/test_route_agy_reset.py::test_no_claude_implementers_and_haiku_last` is
unchanged. The combined branch removes implementation eligibility from default
Sonnet/Opus while retaining every registered leg and explicit effort variant.
New free-first tests reject custom Claude/Anthropic/Claude-flagged implement
inputs and require Haiku last across cost tiers with and without measurements.
These are safety constraints, not a new model preference list or benchmark row.
