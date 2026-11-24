# Implement route defaults (D-5206)

Gateway snapshot HTTP 401/403 exclusions appear as `auth_refused` in candidate
and skipped-leg details, carrying gateway URL, principal, and HTTP status as
evidence. They do not claim gateway unreachability.

`planner.legs.WRITER_ORDER` starts with `cx-gpt-6.2-sol-medium`, `cx-gpt-6.2-astra`,
then `agy-gemini-4-hydrogen-high` (agy harness, D-5903). Hydrogen is enabled,
with an unwitnessed seat because AGY emits no served-model telemetry. Quota, cooldown, or a
confirmed availability refusal advances without an override; safety refusals,
cancellation, and guards still stop dispatch. Eligibility and harness policy apply first.
These three writers precede all other cost tiers even when catalog tiers
change. Other eligible legs retain cost-tier order, with subscriptions before
pay-as-you-go.
Writer and review payment ordering uses one classification function for the
payment class of raw and catalog-projected legs, treating muse-amber as paid:
after the implement writer prefix, no pay-as-you-go leg
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
the registered Sol medium id.

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

Route JSON and saved `chosen_leg` include `effort` (medium by default for GPT writers).
Operator leg JSON preserves its validated `effort` through the loaded leg, route
plan, saved `chosen_leg`, and attempt rows; omitted or explicit `null` effort
stays `null`. All other validated leg fields are retained too. The per-run
renderer declares cx effort models; see [SPAWNER.md](SPAWNER.md#card-driven-routing-core).

## Opt-in order core (D-5696/D-5706)

`payment_order(..., order_class="standard"|"high"|"complex")` selects catalog
positions after registered role filtering; it never grants roles or capabilities.
Hydrogen high is enabled by D-5903.
The D-5696/D-5706 opt-in table originally listed Sol xhigh, Hydrogen high and
non-Claude-Code Sonnet high. See [D-6310 and D-6667](#route-order-and-family-admission-d-6310--rule-353)
for current registrations and implementation priority.
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

## Route shadow comparison

Run `plangraph route-shadow --since YYYY-MM-DD` for a tab-separated report, or
add `--json` for the same rows and coverage counts in JSON. The date starts at
midnight UTC. Set `PG_URL` and provide an unscoped Keeper reader token via
`PG_TELEMETRY_TOKEN_FILE`. If that variable is unset, the command uses
`PG_TOKEN_FILE`. The telemetry scope includes attempts from every project,
ad hoc runs, and older rows without a route. The report header counts rows
read and exclusions for the UTC cutoff, missing routes, pins, and missing
task classes.

The command opens `PLANGRAPH_CATALOG_DB` read-only. The catalog defaults
to `~/.cache/plangraph/spawner_catalog.db` and can also be set with `--catalog`.
To run against a read-only export, pass `--attempts FILE` with a JSON array in
Keeper's `attempts --json` row format. Add `verified` to each exported row when
verification was checked. `true` means verified `run-record` evidence on the
same card and run ID, `false` means the evidence was checked and no match was
found, and null or omitted means verification is unknown.

Each row groups classified, unpinned attempts by task class and role. The
fixed-order vote comes from each run's recorded `route.baselines.static_v0`.
The utility vote maximizes benchmark score minus ten score points per USD of
benchmark task cost among the eligible candidates recorded in `route.top5`.
It uses preferred model, effort, and harness cells. If only the legacy catalog
is available, it uses model and effort priors. Candidate model and harness
come from matching observed leg and effort or an unambiguous exact catalog ID.
An unscored candidate casts no vote. Fully scored runs count attempts with
benchmark coverage for every eligible leg. Each arm reports its most frequent
pick, with lexical leg IDs breaking ties.

Both arms use the observed executions of their picked leg in the same task
class and role. First-pass rate is the share of first recorded runs per card
and role within the window that succeeded on the picked leg, among first
recorded runs whose terminal leg was that pick.
The attempt table records the terminal outcome of a run so this rate is a
window-based proxy for first-pass performance. Cost per verified success
divides total observed cost (including failures) by the number of successful
runs with verified run-record evidence. The cost is unavailable if recorded
cost or verification is incomplete or no run was verified. Observed metrics
are descriptive estimates. Paired replay will compare both policies on the
same work. The sample flag marks rows below ten attempts by default.
Use `--sample-floor N` to show a different threshold.

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

[Train delta review](landing-train.md) routes its replay brief as a witnessed document. The gateway witness gate filters the existing route, and only merge-delta writer families are excluded. Ordinary checkout reviews retain their writer-family and diff requirements.

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

Benchmark ranking resolves the catalog's preferred observation against the
model, reasoning effort and harness of each leg. Legacy benchmark databases
remain readable. The catalog query exposes source, imputation and staleness for
each observation. See [benchmark observations](spawner-benchmarks.md#benchmark-observations-by-effort-and-harness).
When a leg leaves effort unspecified, a legacy catalog row can supply the
catalog's default effort for the same model and a compatible harness.
Route candidates label utility backed by a catalog row as `p_source: measured`.
The catalog observation carries its own `imputed` field and provenance.

D-6310 retires `gpt-6.1-sol` (and `gemini-4-argon` for implement; D-6366 serves it as the first review leg), lists `opencode` as a provider,
removes the `deepseek` and `glm` family bans, and leaves no `opencode-go` leg disabled.
Implementation routes prioritize `cx-gpt-6.2-sol-medium` (with `cx-gpt-6.2-sol-{low,medium,high,xhigh,max}`
explicit variants), `cx-gpt-6.2-astra`, and `agy-gemini-4-hydrogen-high`, with
`opencode/deepseek-v4.1-flash-free` (200k) > `opencode-go/deepseek-v4.1-flash` (1M) >
`opencode-go/glm-5.3-flash` (1M) on `opencode` as implementation fallback.
D-6550 (rule 3.66) uses native Codex for GPT legs. GPT falls back to the OpenCode
gateway model when the Codex client is missing on the selected host. D-6652 below
defines when native Sonnet can serve as a writer or reviewer.
D-6667 retires the GPT-6.1 family from leg selection and explicit model launches.
Use `gpt-6.2-sol` for direct Codex launches. The default writer in `WRITER_ORDER`
is `cx-gpt-6.2-sol-medium`.
For native Codex, routing and launch admission check quota and availability independently
of the OpenCode model listing.
Missing models and inactive or unreachable providers keep their own skip reasons
and cannot satisfy the quota fallback.
AgY quota evidence applies to the model that made the call. When that evidence is
account-scoped, the limit applies to that account's use of that model. Route quota
states show the last probe time, including a successful recovery probe before reset.
Credential, authentication and secrets changes prefer the Claude Sonnet high review
seat, subject to availability and cross-family review.
D-6652 (rule 3.66 (f), corrected 2026-11-23) changes the review order above and the Claude writer
rule. Implement keeps the Codex lead first; `claude-sonnet-5-5-high` (`claude-sonnet-5-5-xhigh`
for a card with `hard: true`) is routed only while the Codex lead is limited (429 or quota),
with reason `codex_limited` in the plan and `route_reason` in the run record, and the route
returns to Codex when it serves again. Review serves the two
required OpenCode seats (`opencode-go-deepseek-v4-flash`, `opencode-go-glm-5.3-flash`, tools
mode with checkout reads) ahead of every other leg, each outside the diff's writer families,
and plans two seats on every head. The Claude review seat serves only for credential,
authentication and secrets diffs or when both required seats are out of the route.
`plangraph router go-row` accepts a part only with two seats that each ACCEPT its head with a
`FILES READ` line naming files, and the keeper reads a required-seat record without
`FILES READ` as no seat.
GPT implement and review legs default to medium effort (D-6397); elevated
effort requires matching `card.effort` and an operator `card.effort_decision`
of the form `D-<number>`. Native Codex package sessions pass
`model_reasoning_effort=medium` by default. Other families keep their own effort.
A 1M spawn route uses the declared request bound of a leg that supports that class.
Sol and Astra declare 1,000,000 tokens, including effort variants and native sessions.
Smaller spawn routes use 55% of declared leg context. Legs without a bound and keeper budgets use the 55% class share.
After the required OpenCode seats, the standard review order prefers
`agy-gemini-4-hydrogen-high` and then `gpt-6.2-sol-medium` in Codex. The Mimo, OpenCode and Neon legs remain
available in their configured order, and the Argon review legs stay last. Repository-required reviews admit OpenCode Go DeepSeek and GLM tools legs, which get checkout reads and scoped test commands.
A missing verdict or a diff-only acceptance cannot complete a repository review. The reviewer's family differs from the writer's (a gpt writer skips the gpt leg, a gemini writer skips argon).
`opencode-flint-40b-free`, `opencode-big-pickle`, and `opencode-go-beam-free` are restricted
to low-risk job routes (`low_risk_only` otherwise, never HIGH reviews). `opencode/glm-5.3-flash-free`
stays disabled because it is not served by `opencode models`.
The five-head benchmark for Flint, Big Pickle and Beam was skipped in this lane.
The existing HIGH-review exclusion is covered by `test_d6310_route_orders_low_risk_and_families`.
The Rust route reports legs with more than three missing verdicts in their last
ten completed reviews under `review_health_alerts` and puts them after healthier candidates.

## Research routing

Use `--role research` on the CLI or `role: "research"` in MCP to route or
spawn a research card. Automatic routing uses research-tagged legs, checks
estimates against declared request limits, and orders eligible Claude legs last.
Set `web_required: true` on the card to require web tools on the resolved
harness. This requirement also applies to explicit leg and model picks.
If no route remains, the JSON refusal includes each skipped leg and its reason.

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


A review route also reads the `Writer` trailer of the `diff_ref` head commit
(`planner.seats.head_writer_families`): its family is excluded in addition to the
`--exclude-family` values, an unknown writer refuses, and an explicit `--leg` of that
family is refused.
For a registered-repository ref (`repo_id`, `base_sha`, `head_sha`) the Rust server reads the
same trailers (`launch::with_head_writers`) and adds the families to `writer_families` before the
launch adapter runs; an unreadable head refuses.

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

Merge paths and main's state: the keeper's phase finish asks `plangraph main-state --admit` after CI is green on the phase head and refuses to merge with `main_red`, `main_pending`, `state_unknown` or `ref_not_forward_only` (docs/ci-parallel.md, "Red main and runner switches").

The D-5174 effort profile is computed from the measured S0–S4 bucket, risk,
test coverage, fanout, write set, and context fit. The effective AA floor is
the larger of the risk and level floors. A planner can raise a level. A lower
assignment records its requested value and the rule reason. Task classes group
S0–S1, S2, and S3–S4 by change or regression check. Version 1 uses the plan's
starting floors. Cite a replay delta for future changes to the profile table.

Keeper attempts retain the routed profile, plan hash and selected leg alongside
the captured eligibility, rejected legs, top five, pin and benchmark baselines.
The watchdog delivers one terminal record per spawner run, including runs that
fell through to a different leg. Missing benchmark evidence stays null.
## Worker claim bases

A worker card claimed against an accepted landing-train part receives the train head in `base_sha`. A retry of the same live lease uses the pinned SHA. Ordinary claims use the phase branch tip when one is available, otherwise the configured base branch tip.

Review seats per leg are bounded by `route_policy.json` `review_seat_admission`; the review route carries it as `seat_admission` and the native spawner queues seats beyond the bound: [seat admission](checkpoints/LE-SEAT-BURST-DRAINS-SMART-REVIEW-LEG.md).
