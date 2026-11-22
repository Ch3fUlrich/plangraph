# Implement route defaults (D-5206)

Gateway snapshot HTTP 401/403 exclusions appear as `auth_refused` in candidate
and skipped-leg details, carrying gateway URL, principal, and HTTP status as
evidence. They do not claim gateway unreachability.

The declared writer order uses `cx-gpt-6.1-sol-xhigh` (model `omniroute/cx-gpt-6.1-sol-xhigh`,
effort `xhigh`), then `agy-gemini-4-hydrogen-high` (agy harness, D-5903). Hydrogen is enabled,
with an unwitnessed seat because AGY emits no served-model telemetry. Quota, cooldown, or a
confirmed availability refusal advances without an override; safety refusals,
cancellation, and guards still stop dispatch. Eligibility and harness policy apply first.
Both designated writers precede all other cost tiers, even when catalog tiers
change: Sol xhigh, then Hydrogen high (rule 3.23a / D-5212). Other eligible legs
retain cost-tier order, with subscriptions before pay-as-you-go.
Writer and review payment ordering uses one classification function for the
payment class of raw and catalog-projected legs, treating muse-amber as paid:
after the implement writer prefix (Sol, then Hydrogen high), no pay-as-you-go leg
precedes a subscription-class leg, and muse-amber precedes paid DeepSeek.
The designated writers keep that prefix even when catalog tiers reprice them as
pay-as-you-go; review has no writer prefix. A trial or free slot keeps its position
unless it is a writer-prefix leg; a tier-less or id-less leg never raises.
This pure ordering core does not check serving state or change admission.
Main's registered implementation fallbacks retain their roles, permanent disables,
expiry/re-probe behavior and benchmark ranking within the remaining cost tiers.
A leg marked `benchmark_optional` with no row remains eligible in ordinary
routes and is skipped in strict benchmark routes. See
[spawner benchmark routing](SPAWNER.md#card-driven-routing-core).
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

## Opt-in order core (D-5696/D-5706)

`payment_order(..., order_class="standard"|"high"|"complex")` selects catalog
positions after registered role filtering; it never grants roles or capabilities.
The implement table lists Sol xhigh, Hydrogen high and non-Claude-Code Sonnet high.
Hydrogen high is enabled by D-5903; the real registry returns Sol xhigh, Hydrogen high
and Sonnet high (subject to upstream eligibility).
The active review legs the route can offer come from `planner.legs.routed_review_legs` (also used by the scheduled review probe).
Review excludes writer families, requires diff <100k for Haiku, and needs opposing
eligible cheaper verdicts for Ember. Spec amendment: MiMo and both DeepSeek review
legs retain null registered effort, as does Haiku; requested medium/xhigh efforts
are deferred until authoritative benchmark coverage exists. Non-table registrations
are dropped; table identities with legacy models are refused. Four unknown-capability declarations
are explicit-only, permanently disabled and role-less; JSON overrides may not
omit windows, max_request_tokens or trains_on_data even when disabled.
Live default activation and admission migration belong to LIVE-FENCE, not this core.
D-5903 admits the two exact Hydrogen legs without benchmark scores; no benchmark
evidence is invented. Benchmark rows, pending file and frozen ceiling
are unchanged, and the benchmark contract passes with the retained null efforts.
Ember, Sol/Terra review-prefix and planning Fable positive paths are synthetic-only
tests of upstream admission, not real-registry coverage. Real-registry assertions
pin their absence and missing roles (plus permanent disables for declarations).

## DeepSeek review identity (D-5641, D-5643)

The `omniroute-deepseek-flash` subscription leg requests
`omniroute/deepseek-v4.1-flash`, not the obsolete `omniroute/deepseek-flash`.
The Go review leg already requests `opencode-go/deepseek-v4.1-flash`.
Registration is not host availability: the launch boundary checks the rendered
child config via `opencode models --standalone` and skips an absent id with
`not_served_on_host` before starting a harness run. The recorded local probe
does not yet list the corrected OmniRoute id; gateway and credential readiness remain a host-side operator request. Never replace
it with the distinct paid `omniroute/deepseek-direct-flash` route.

## Replan-0438 CI reconciliation (historical validation)

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

## Gemini retirement (operator 2026-11-14)

Every Gemini Flash version is retired. Requests and stored target overrides fail
with `gemini_flash_retired: model retired by operator order 2026-11-14; use
agy-gemini-4-hydrogen (medium: agy-gemini-4-hydrogen-medium)`.
D-5903 enables `agy-gemini-4-hydrogen-high` for implement after Sol and
`agy-gemini-4-hydrogen-medium` for read-only tools review (agy plan mode) in the retired Flash slot.
Both use subscription tier, no training on data, windows 128k/256k/1m and
max_request_tokens 900000 from the existing agy Claude 5.5 legs (same Antigravity
subscription and harness). `clients.build_standalone_fallback_command` uses an
empty `-p` argument; `clients.launch_stdin_payload` sends the prompt as stream JSON
on stdin, so Linux's 128 kB single-argument ceiling does not limit this path.
Reviews use an inline diff without repository tools (agy plan usage). Evidence:
`agy models` lists Hydrogen low/medium/high/xhigh (read 2026-11-14), and agy CLI
reviews returned verdicts for PR #878 (three heads) and #886 that day; see
`handoffs/plan-order/reviews/` (PR 878 head `5ac07d1d`, PR 886 head
`75c5bd2f`, Hydrogen text files). Both seats remain **unwitnessed** (rule 3.26):
AGY emits no served-model telemetry; the runner retains `served_model` and
`served_family` as `unwitnessed`. A declaration or successful verdict is no witness.
Standard-tier ordering still starts at MiMo under D-5706; opt-in high review puts
Hydrogen medium in its fixed table position after any admitted Sol/Terra seats.
Terra, Ember and Fable declarations remain disabled. No Flash fallback is retained.
Earlier reconciliation notes above describe historical behavior, not current admission.

Deployment retirement tombstones are cleanup history: apply prunes the removed
model-named combos and clean routes that have no remaining servable legs.

D-5903 round 3 explicitly retains the Hydrogen tools review seat for cards without `diff_ref`, including when Mimo/OpenCode/Muse seats are unavailable. Like other agy tools seats it reviews the selected checkout (explicit `cwd`, otherwise the runner default); a no-diff card does not request an automatic checkout of another revision. Hydrogen reviews use read-only plan mode.

The shipped provider config and the pending benchmark inventory use the same `omniroute/deepseek-v4.1-flash` identity; the pending row explicitly records missing measurements. Historical fallthrough replay projects the legacy alias onto this registered identity.

Reviews (rule 3.50, D-6028) start on mimo-v2.6-pro, then mimo-v2.6-flash (both inline-only, so
the card needs a diff_ref), then the D-5966 order: agy Hydrogen high, agy Argon medium, DeepSeek
flash and Beam free legs, then native Claude Sonnet high. Each agy model has an independent quota reset; the native Sonnet subscription uses its own quota key.

D-6054: the OmniRoute Muse leg uses `omniroute/spark-1.3-contributor`, declared in the
shipped provider config. The Rust `opencode_catalog` integration tests check enabled
default, explicit and review fallback OpenCode legs against the launch renderer and
`host_skip_reason` on a fixture host. Named exclusions cover external catalogs and
the existing Grok declaration gap; an unknown model fails with its leg name.

D-6082 / D-6143: `measured_down` extends its run-record scan window to cover the
oldest active uncapped provider reset in `leg_state`, so legs with multi-day
provider resets remain suppressed past 24 hours until their `reset_at` elapses.

Review writer aliases (`codex`, `cx-gpt-6.2-sol`, `gpt`) share the registered
writer-family resolver used by spawn admission and review seats. The preferred
Gemini/free review prefix remains first; eligible OpenAI legs outside that order
stay in the fallback list. Other legs outside the operator order
stay excluded. Claude writers may use the
`cx-gpt-6-luna` Codex seat. Unknown names fail with accepted family names
and aliases within native refusal limits; registered leg/model ids remain accepted.
Verdict delivery uses the same resolver to forward canonical writer families.

## Route order and family admission (D-6310 / rule 3.53)

D-6310 retires `gpt-6.1-sol` (and `gemini-4-argon` for implement; D-6366 serves it as the first review leg), lists `opencode` as a provider,
removes the `deepseek` and `glm` family bans, and leaves no `opencode-go` leg disabled.
Implementation routes prioritize `cx-gpt-6.2-sol-medium` (with `cx-gpt-6.2-sol-{low,medium,high,xhigh,max}`
explicit variants), `cx-gpt-6.2-astra`, and `agy-gemini-4-hydrogen-high`, with
`opencode/deepseek-v4.1-flash-free` (200k) > `opencode-go/deepseek-v4.1-flash` (1M) >
`opencode-go/glm-5.3-flash` (1M) on `opencode` as implementation fallback.
While every Codex leg is unavailable (quota_until, down), `agy-claude-sonnet-5-5-high`
is served directly behind the Codex legs; with any Codex leg available it is skipped as
`codex_available` (rule 3.58, D-6366).
GPT implement and review legs default to medium effort (D-6397); elevated
effort requires matching `card.effort` and an operator `card.effort_decision`
of the form `D-<number>`. Native Codex package sessions pass
`model_reasoning_effort=medium` by default. Other families keep their own effort.
For 1M spawn routes, a leg that supports the class and declares
`max_request_tokens` can serve estimates up to that request bound. Sol and
Astra declare 1,000,000 tokens. Legs without a declared bound use 55% of
their supported window class. Smaller spawn routes use the 55% share of a
declared leg context, and keeper window budgets continue to use the class share.
Review routes follow rule 3.61 (D-6481, amending rule 3.58 / D-6366):
`gpt-6.2-sol-medium` first, as a native Codex run (harness `codex`) with repository
access, then `mimo/mimo-v2.6-pro`, `opencode/deepseek-v4.1-flash-free`,
`opencode-go/deepseek-v4.1-flash` and `opencode-go/glm-5.3-flash`, with
`agy-gemini-4-neon-medium`, `claude-sonnet-5-5-high` and `mimo/mimo-v2.6-flash` as fallbacks and
the agy argon legs (`agy-gemini-4-argon-low`, `agy-gemini-4-argon-medium`; plan mode, review only,
never an implement leg) last of the whole route, behind the non-order extras. A review run that ends without a VERDICT line is
recorded `failed` with reason `no_verdict`. The reviewer's family differs from the writer's (a gpt writer skips the gpt leg, a gemini writer skips argon).
`opencode-flint-40b-free`, `opencode-big-pickle`, and `opencode-go-beam-free` are restricted
to low-risk job routes (`low_risk_only` otherwise, never HIGH reviews). `opencode/glm-5.3-flash-free`
stays disabled because it is not served by `opencode models`.

## Automatic work mode (D-5952 / rule 3.41)

`pg-spawnd` route JSON, including the `plangraph_agent_route` MCP response,
reports `work_mode` and `work_mode_reason`. The repository/user setting follows
[work-mode configuration](work-mode.md); absent settings still default to batch.
Manual `batch` and `piecewise` bypass auto and report `manual <mode>`.

`pg_worker_core::work_mode_auto::resolve` selects `batch` in auto when at least
one served implementation leg has catalog `aa_intelligence >= 50.0`, the
strong-writer threshold. Served means admitted by the existing writer planner,
including its quota, availability and policy filters; a registered strong leg
with exhausted quota does not count. Otherwise auto selects `piecewise`.
Missing/non-finite scores cannot qualify a strong writer. Reasons name the
first deciding strong leg, unavailable strong leg, or lower-tier served leg;
an empty snapshot explicitly reports no served writers. Review and orchestrate
routes consult the implementation snapshot, so a strong review-only leg cannot
select batch. This does not change route order or start orchestration.
The config root comes from `card.repo`, `card.cwd`, `card.diff_ref.repo`, then
the daemon working directory, in that order.

## Review `diff_ref.paths` and `plangraph review`

Review cards accept an optional non-empty `diff_ref.paths` list of repo-relative
pathspecs (validated by `planner.seats._check_diff_ref` and `pg_worker_core::review_split::valid_diff_path`
to reject empty paths, NUL bytes, leading `-`/`/`/`\`/`:`, drive prefixes, and `..`
components). When an inline diff exceeds the leg limit, `pg-spawnd` and
`plangraph review` split changed files by top-level directory into path-group seats
over the same `base` and `head`, passing each group in `diff_ref.paths` and
failing the review if any group returns `REQUEST_CHANGES` or a single file exceeds
the leg cap.

## Own harness first (rule 3.53, D-6310)

A model runs in the harness it is trained on: gpt in Codex (`codex`), gemini in agy, claude in
Claude Code or agy (`agy-claude-*` legs count as own). `planner/legs.py` (`resolve_harness`, `own_harness_legs`) resolves each routed leg
inside `plan_spawn` (before policy, seats and ordering, so a paused harness applies to the
resolved leg): with the family's client installed on the host (`shutil.which`) the leg's
`harness` is the own client (a gpt OpenCode leg becomes `codex`, keeping its registered model;
the Codex launch maps `omniroute/cx-gpt-6.2-sol-medium` to `--model gpt-6.2-sol` with
`model_reasoning_effort=medium`); without it the OpenCode leg of the same model runs and carries
`harness_fallback=client_missing` (a native leg with no OpenCode counterpart is skipped as
`client_missing`). Families
without an own client (deepseek, glm, mimo, free legs) stay on OpenCode. Pinning a model's
OpenCode leg with `--leg` while its own client is installed needs `--override-reason`.
