# PG-EFFICIENCY-GATES design (operator D-2739, routing DECISIONS §0)

Author: claude-opus-5-5 (D-2739: Opus writes this item). Evidence base: routing
`docs/reports/2026-10-25-fix-rounds-and-progress.md` and `2026-10-25-efficiency-research/r1-r4`.
Goal: the §0 rules that broke in practice become code, so no L1 has to remember them.

## Gates

| § | Gate | Where | Refuses / reports |
|---|---|---|---|
| 0.1 | follow-up item | keeper `create_items`/`apply_ops create` -> `gates.followup.check_new_items` | id suffix `-LOWS/-FIX/-FU/-FOLLOWUP/-SQn/-Rn`, or `fixes`/`corrects` naming a not-finished item; router (operator channel) and `system` (planner) exempt |
| 0.1 | fix log | keeper verb `log_item` (kind `fix`), event `item_logged` | appends to the item's fix log (shown under the description in `show`); `in_review` -> `claimed` under the same key |
| 0.2e | round counter + cap | `gates.rounds.count_rounds` over `changes_requested` + `item_logged` events; `show.gates`; keeper `alarms` row `review_round_cap` | `rounds_since_replan >= 3` -> spawner refuses implement runs until `log_item kind=replan` (strong author model) |
| 0.2d | fix brief by code | `gates.findings.parse_seat_output` + `build_fix_brief`; spawner fix path | every finding verbatim with id + invariant table + class-fix + full affected suite; a brief missing a finding id is refused |
| 0.2c | verdict parser | `review.verdict.strict_verdict`, `tools/seat_verdict.py` | HIGH/MED in body -> changes whatever the VERDICT line; empty / billing / fence / failed output -> no verdict (re-run) |
| 0.2b | size gate | `tools/lane_gates.py size` (CI) | > 400 changed lines or > 15 files vs merge-base (lockfiles/generated/vendor excluded) fails, warns at 200/8, unless a `Plan-Slice:` trailer names a design slice whose budget covers it |
| 0.2f | base pin | `tools/lane_gates.py base` (CI) + spawner | > 1 merge-from-main in the lane fails; writer/fixer briefs that ask to merge main are refused (the one merge is the L1's final step) |
| 0.2a | plan gate + fixer strength | spawner `gates.admission.admit` | HIGH (or `est_lines > 400`) implement run without a reviewed `design` log entry is refused; fixer legs weaker than the writer or flash-class are dropped |
| 0.2c | first review = two seats with text | `gates.findings.first_review`, `tools/lane_gates.py seats <files>` | fewer than 2 seats, or any seat without a verdict -> no round verdict (exit 3, re-run that seat); any seat changes -> changes |
| D-2744 | retired family | `gates.models.retired` in `SpawnService._gate_plan` (every role, overrides too) | deepseek legs (family or model id) dropped with skip reason `retired_family`; no leg left -> refused; an all-limited plan stays exit 4 |

## Invariants

| id | invariant |
|---|---|
| I1 | A gate never fails open on malformed input: unparsable seat output = no verdict; unknown model tier = standard (never strong). |
| I2 | Round count is a pure function of the event log (replay = same number); a `replan` resets only the since-replan count, never the total. |
| I3 | One failing review round counts once: a `fix` log entry that answers a `changes_requested` with no fix entry in between is the same round. |
| I4 | A fix brief contains every finding id of its card verbatim; the L1 note may only be appended after them. |
| I5 | Spawner admission without a configured keeper skips item-state gates (round cap, plan gate) and records `gates_skipped`; card-only gates (fix brief, merge-main, fixer strength) always run. |
| I6 | Lane size counts added+deleted lines from `git diff --numstat <merge-base> HEAD`; binary files count as 1 line; excluded paths never count. |

## State table (item, `log_item`)

| state | fix | design | replan |
|---|---|---|---|
| open | log only | log | log |
| claimed | log | log | log |
| in_review | log + -> claimed (same epoch, fresh lease) | log | log |
| done / cancelled | refused (bad_state) | refused | refused |

## Input classes

| input | class | result |
|---|---|---|
| seat text with `VERDICT: APPROVE` and a `HIGH` finding line | contradiction | changes |
| seat text `HIGH: none`, `0 HIGH`, `no MED findings` | negated severity | not a finding |
| empty / whitespace / only `billing`/`quota`/`402`/fence text | failed seat | None (re-run) |
| finding without explicit id | generated id `<seat>-F<n>` | kept verbatim |

## Slices

| slice | scope | max_lines |
|---|---|---|
| 1 | all gates above, one lane (D-2739: single task, single go) | 3000 |
| 2 | decisions in the keeper (D-2746 (10)): rule items, rule tree, generated DECISIONS.md | 600 |
| 3 | legs from properties x live OmniRoute availability (D-2758 (5)) | 450 |

## Slice 2: decisions in the keeper (operator D-2746 (10))

Goal: the plan graph is the single source of truth for rules; `routing/DECISIONS.md` is a generated view.

| part | where | behaviour |
|---|---|---|
| rule item | project `decisions`, key `decisions:RULE-<n>` (e.g. `RULE-0-2A`), kind `decision`, `operator=0`, `wait=NULL` | description = the rule line verbatim; title = number + bold title; `source` = section; `note` = `tag=<keep|gate:..|gate-todo ..>; d=<D-ids>; order=<n>` |
| in force / retired | state `done` = in force, `cancelled` = retired | done/cancelled rows never show in ready, blocked, critical path, stall or operator-wait views; no lifecycle (claim/review) applies |
| write path | new router-only verb `rule_set` (event `rule_set`, folded by `events.fold`) | creates or replaces one rule item and its depends_on edges in one event; a replace keeps history in the event chain; refuses keys outside project `decisions`, text with credentials, edges to unknown rule keys, cycles |
| rule tree | `edges` depends_on rule -> rule | from rule-number references in the rule text (`per 3.2`, `(7.1)`, `0.2b`) plus the §0.2 pipeline 0.2a -> 0.2b -> 0.2c -> 0.2d -> 0.2e; `tools/decisions_view.py tree` walks it |
| section text | items `decisions:PREAMBLE`, `decisions:SECTION-<n>` (kind `decision`, done) | description = the block's text with one `{{<rule number>}}` placeholder per rule line |
| import | `tools/decisions_view.py import DECISIONS.md` -> `rule_set` ops (dry-run default) | round-trip check before writing: render(parse(file)) == file byte for byte |
| view | `tools/decisions_view.py render` (keeper `snapshot`) -> DECISIONS.md | deterministic: sections by number, rules by `order` |
| CI check | routing repo: `decisions.json` (keeper export, committed with the view) and `DECISIONS.md == render(decisions.json)`; host check `export == keeper` | a hand edit of DECISIONS.md fails CI; CI never needs keeper access |

Invariants: I7 render(import(file)) == file; I8 a rule key exists once, its latest `rule_set` wins on replay; I9 rule edges form a DAG.

Switch-over for routing: (1) slice 2 merged + keeper deployed; (2) `decisions_view.py import routing/DECISIONS.md --apply` (router token); (3) `render` == file, commit `decisions.json` + CI check in routing; (4) from then a new D-row = `rule_set` first (CLI `plangraph-keeper rule set`), then `render`, commit view + log row together; hand edits of DECISIONS.md are refused by CI.

## Slice 3: legs from properties x live availability (operator D-2758 (5), routing OK)

Goal: no hand-kept leg order. Any model may run (Claude included); the pick is computed from model properties and what OmniRoute serves right now. Bans stay.

| part | where | behaviour |
|---|---|---|
| catalog | `routing_support/catalog.py` (new) | live list = OmniRoute `GET /v1/models` (host gateway, key per D-2758 (6)) + `agy`/`qodercli` probes (`resolver.host_skip_reason`); cached 5 min in the spawner state dir; fetch failure -> last cache, marked `stale`, never an empty list |
| properties | `planner/legs.py` `MODEL_PROPS` (model -> family, tier, windows, trains_on_data, harness/path per D-1504) | only facts, no order; a served model without a props row is skipped with reason `no_props` (listed in the plan, so the gap is visible) |
| roles | derived, not listed | a model takes a role when its benchmark row has that role's score (`score_model`); no row -> prior score from tier, `p_source=prior` |
| rank | `plan_spawn` for every role (today: review only) | filters first: bans (`OPERATOR_REMOVED_MODELS`, retired deepseek D-2744), privacy (`gates.privacy.sensitive_leg_ok`), window fit, `limited` (provider stops, `unavailable_until`), excluded families; then `rank_candidates(role, risk, need_tokens)`; ties by tier (free < subscription < paid) |
| removed | `WRITER_ORDER`, `DESIGN_ORDER`, "no Opus leg" | `plan.source` = `computed`; the D-2683 order survives only as a recorded tie-break until benchmark rows exist for all its legs |
| attribution | unchanged | who may WRITE on open work stays the CI commit gate's job (D-2046); the router does not filter Claude |

Invariants: I10 a banned or privacy-refused model is never in a plan, whatever its score; I11 the plan lists every served model with its reason (picked, skipped + why); I12 same catalog + benchmark + card -> same plan (deterministic).

Size: ~450 lines incl. tests (hermetic: catalog from a fixture, never the live gateway in CI). Depends on the D-2758 (6) key path (6100115c, held for operator OK); without it the catalog falls back to the configured legs and records `catalog: offline`.

## Slice 4: run liveness (router D-2771 evidence, build-L1 2026-10-25)

Evidence: mimo-v2.6-pro writers on workst ran ~3 h and ended "Response still truncated after two output token limit continuations" with near-empty WIP (20261025-070241-exec-sq12-70c4dc WIP f308086f 45 lines; 20261025-103823-pfr2-3185c3; 20261025-042244-pfg2-b26b60); plangraph run-51bc2b81 mimo-pro 39 min, 0 commits, 0.6% CPU (stall, cancelled by hand); code host qodercli seats OOM-killed (kernel, pid 2192098 13:04:23) while memguard saw "no agent run to stop".

| gap | where | behaviour |
|---|---|---|
| truncation loop | `gates/findings.py` failure classes + spawner result | "still truncated after ... continuations" / output-limit text = failed run (`exit_class=truncated`), never a writer result; next leg per plan, leg marked `limited` for that card size |
| stall | `spawner/runner.py` watchdog (extends `stall_watchdog.py`) | no new commit / file write in the cwd and CPU < 2% for 15 min -> cancel, `exit_class=stalled`, fall through to the next leg; WIP kept on the run branch |
| wall cap | settings, per role | writer 90 min, seat 45 min default; over cap -> cancel, same fall-through |
| memory | `spawner/runner.py` launch | child starts in a `systemd-run --user --scope` unit named `autoos-worker-pg-<run>` so memguard stops it before the kernel OOM picks a session; exit -9 with a kernel OOM line -> `exit_class=oom`, retried once on the next host per D-2472 (workst) |

Invariants: I13 a truncated, stalled, capped or OOM run never counts as a writer result or a seat verdict; I14 every cancel records its exit_class and the fall-through leg.
