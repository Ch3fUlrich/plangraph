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

Design reviews: seat A r1 (qwen run-050c742b @ecf5bd5d) D20-D25, r2 (qwen run-032bb8bc @0b3a01e2) D49-D50 (both cut to the 4 KB tail; D1-D19 and D30-D48 lost), r3 (mimo-v2.6-pro @0b3a01e2, full text) D60-D74. Every row below names the findings it answers.

### Shared pieces (slice 4a)

| piece | where | behaviour |
|---|---|---|
| limits reader (D49, D65) | `plangraph/settings.py` new fixed accessor `spawn_limits(role)` -> frozen dataclass `SpawnLimits(wall_cap_min, stall_min, stall_cpu_pct, ignore, floor_mb, max_runs, require_principal)`; settings file section `spawn_limits: {writer: {...}, seat: {...}, default: {...}}`; env `PLANGRAPH_SPAWN_LIMITS` (JSON, same shape) overrides for tests | role map: `writer` = `implement`/`fix`/`writer` (gates/admission.py WRITER_ROLES), `seat` = `review`, `default` = everything else; defaults: writer 90/15/2/[]/2000/4/false, seat 45/15/2/[]/2000/4/false; `ignore` holds per-harness path globs (`{opencode: [...], qodercli: [...]}`), so it is data, not a module constant; unknown keys refused like the rest of settings |
| run result (D50, I13) | `service.py` writes `runs/<run>/result.json` `{status, exit_code, exit_class, attempts}` next to the record | `gates/findings.failure_reason(text, status, is_error, exit_class=None)`: any `exit_class` in `LIVENESS_CLASSES = {truncated, stalled, capped, oom, host_full, memguard}` returns a reason before any parsing; `seat_outcome(..., exit_class=)` passes it; `findings_from_files` reads `result.json` when the seat file sits in a run dir (other files: the caller states the class); `_continue_writer` returns at once when the record has an `exit_class` |
| output kept in full (D-2444, D-2795, I17) | `service.py:87-89`, `cli.py:313` | the record keeps a 4 KB display tail; the full redacted stdout/stderr go to `runs/<run>/stdout.log`/`stderr.log` (cap 2 MB, then head 1 MB + tail 1 MB with a marker); `wait --json` returns the paths; seat parsing reads the full file |
| cancel with class (D61, D74) | `runner.cancel(job_id, exit_class='cancelled')` | sets `job.exit_class` BEFORE the kill, so the monitor sees it when the exit arrives (own cancel always wins over -9 classification); hand cancel keeps `cancelled`, which stays in `routing._PARK_EXITS`; `_PARK_EXITS` is not changed; the liveness classes are new names outside it |
| fall-through and terminal (D61, D73, I14) | `execute()` in `service.py` and `runner.py` | the loop continues to the next leg when the attempt has an exit_class in `{truncated, stalled, capped, oom}` (today it only continues on provider stop/unavailable); at most 2 fall-through attempts per run, never a leg already tried; each attempt row records `{leg, exit_class, next_leg}`; when every attempt ended in a liveness class the terminal is `status=failed`, `exit_class=<last>`, never `limited`/exit 4; `_save` merges `exit_class` set-once and the error text includes each attempt's class |
| wall cap (D65, D71) | runner ctor `clock=time.monotonic` (injected); per run, not per attempt | cap = `clamp(spawn_limits(role).wall_cap_min, estimate_tokens / 1000, 240)` minutes, so big cards get more time without a card field; each attempt gets `wait_for(..., remaining)`; over cap -> `cancel(job, 'capped')`; `PLANGRAPH_SPAWN_RUN_TIMEOUT` stays a per-attempt cap and its exit 124 is now `exit_class=capped` (falls through) instead of a bare failure |
| truncation (D21, D62, D20) | `liveness.truncated(harness, exit_code, stderr_tail, patterns)`, patterns per harness in settings (`spawn_limits.truncation: {opencode: [regex]}`) | the provider envelope carries no finish reason today (it accepts only 429/402), so slice 4 matches the harness's own final stderr lines only (never stdout, where the reply and any quoted phrase live) and only on a nonzero exit; default opencode pattern = "still truncated after .* output token limit continuations"; a finish-reason field in the gateway envelope is a later change outside slice 4; the run records `limited_above=<estimate_tokens>` for (leg, account, combo) in a new optional `LegStateStore` dimension, `plan_spawn` skips that leg only for cards at or above that size, expiry 6 h, never a plain `limited` |
| spawning principal (D-2796, D69, I18) | `service.py` record creation (spawn, continuation, `save_refusal`), `cli.py spawn --principal` | record `principal = {name, session, host, pid, cwd, source}` with `source` in `flag`/`env` (`PLANGRAPH_PRINCIPAL`, `CLAUDE_SESSION_ID`)/`fallback` (parent argv[0]); continuation and refusal records copy the parent's principal; it is self-asserted attribution, not authentication (a keeper-token principal is later work); `require_principal` refuses only `source=fallback` for writer roles and ships off, routing turns it on after callers pass `--principal` |

### Monitor and stall (slice 4b)

| piece | where | behaviour |
|---|---|---|
| polling monitor (D70) | `runner._monitor_process` | `proc.communicate()` is replaced by two stream reader tasks plus a loop `await asyncio.wait({proc.wait()}, timeout=tick)` with injected `clock` and `tick` (default 30 s); each tick calls `liveness.check`; output bytes stream to the run's log files as they arrive |
| stall rule (D22, D23, D63, D64) | pure `liveness.check(run, now, probe, limits)` | stalled only when ALL hold for `stall_min` (15): no new stdout/stderr bytes (a streaming model is alive), no new commit on the run branch, no newer mtime on any file under the cwd outside `.git/` and the harness `ignore` globs (tracked or not, so build dirs count), and process-group CPU < `stall_cpu_pct`; a recorded provider wait is `waiting` until its reset + 5 min, or 10 min (the leg_state reprobe default) when the 429/402 had no reset; a harness that waits silently with no output, writes or CPU for 15 min is stalled by design and falls through; a loop that keeps rewriting the same file is bounded only by the wall cap (stated limit) |

### Memory (slice 4c, after build-L1 agrees)

| piece | where | behaviour |
|---|---|---|
| scope for Python launches (D25, D60, D66) | `runner.spawn` before `process_factory`, new `spawner/scope.py` with the same argv layout as `rust/worker-core/src/scope.rs` (a golden test compares both layouts) | when the user manager is reachable (`XDG_RUNTIME_DIR` + bus socket present and `systemctl --user is-system-running` not `offline`): argv = `systemd-run --user --scope --quiet --unit=autoos-worker-pg-<12 hex of run id>.scope -p MemoryMax=<limits> -p MemorySwapMax=0 -p TasksMax=256 -- <cmd>`; run id must match `run-[0-9a-f]{32}`; cancel = `systemctl --user stop <unit>`, then `killpg` as fallback (catches members that left the group); no user manager (Windows, containers, offline manager) -> unscoped, record `scope: none`, and the host admission row still applies; the cgroup ceiling makes the run's own cgroup hit OOM before the global OOM picks a session |
| OOM and memguard class (D67, D66) | `liveness.exit_class_of(job, unit_show)` | at exit, read `systemctl --user show <unit> -p Result -p MemoryPeak` (user-readable, no kernel journal); `Result=oom-kill` -> `oom`; the unit stopped by memguard (its stop log names the unit; memguard writes `stopped <unit>`) -> `memguard`; an own cancel (job.exit_class set) wins; then `reset-failed` the unit; `oom` falls through to the next leg on this host only if admission passes, otherwise `host_full`; moving to another host is the caller's job (D-2472), and a seat lost this way returns no verdict, so `first_review` asks for a re-run |
| host admission (D-2785, D68, I15) | `spawner/host_load.py` `admit_host(probe, limits, harness)`, called by one shared function from both `_spawn` implementations and `_continue_writer` | runs are counted once per dedup key: the cgroup path for scoped runs, else the process group id; a plangraph record counts only when its pid matches pid + `/proc/<pid>/stat` start time (pid reuse); client processes (`opencode run`, `qodercli`, `agy`, `claude` by argv[0] basename, wrappers resolved through their process group) count only when their key is new; the memory check uses MemAvailable, which already includes other users' and containers' usage, so missed wrappers only affect the run count; projected RSS = p90 of `MemoryPeak` from the last 20 scoped runs of that harness on this host, never below the harness default (opencode 600 MB, qodercli 500 MB, agy 500 MB), so cheap runs cannot lower it; refuse `host_full` when MemAvailable - projected < `floor_mb` (2000, D-2706) or counted runs >= `max_runs` (4); the refusal lists the counted keys |

### Tests and size

Test seams (D24, D71, I16): `liveness.*` are pure (injected clock, process/CPU probe, git probe, unit-show reader); the runner takes `clock` and `tick`; `host_load` reads meminfo, `/proc` and unit data through an injected reader; scope argv is asserted on captured argv (golden shared with `scope.rs`), never real systemd. Fixtures from the D-2771 runs: opencode exit 1 with the truncation line on stderr (truncated) and the same line only in stdout (not truncated); a 39 min run with 0 commits, no output, 0.6% CPU (stalled); the same with a 30 min retry-after recorded (waiting); a streaming run with no writes (alive); `Result=oom-kill` (oom), a memguard stop line (memguard), an own cancel racing a -9 (cancelled); `host_full` refusal at each of the three launch sites; a run whose two attempts are truncated then capped (terminal failed/capped, attempts recorded).

Size and order (D72): 4a ~350 lines (limits reader, run result + I13, output in full, cancel class, fall-through/terminal, wall cap, truncation, principal); 4b ~250 (polling monitor, stall); 4c ~350 (scope, OOM/memguard class, host admission; after build-L1 agrees on the shared argv layout). Each slice ships only classes its own code can produce plus the 4a plumbing that consumes them.

Invariants: I13 a run with any liveness exit_class never counts as a writer result or a seat verdict (enforced in `failure_reason` and `_continue_writer`); I14 every attempt records `{leg, exit_class, next_leg}` and liveness-only runs end `failed`, never `limited`; I15 no launch unless MemAvailable minus projected RSS stays >= 2000 MB and fewer than 4 runs are counted (D-2706); I16 every liveness decision is a pure function of injected inputs; I17 seat and writer output is kept in full on disk; I18 every run record (spawn, continuation, refusal) names its principal and the principal's source.

Open follow-up (D-2799 (5), interim, not done): the spawning principal is self-asserted and `require_principal` ships off. Still to do: an authenticated principal (keeper-minted run token checked by the spawner, like the provider-envelope nonce) and turning the refusal on for writer roles. Until then I18 means attribution only.
