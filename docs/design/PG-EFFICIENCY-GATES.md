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
| 4 | slice 4a run liveness plumbing: limits reader, exit vocabulary + liveness field, run result + I13 refusal, output on disk, cancel class (D-2814, D103; est. 150 code + 200 tests) | 400 |
| 5 | slice 4b polling monitor + stall rule (D-2814) | 300 |
| 6 | slice 4c scope launcher, OOM/memguard class, host admission (D-2814, build-L1 conditions 1-5) | 400 |
| 7 | slice 4a2 fall-through/terminal, wall cap with shared clock and continuation deadline (D-2814, D103; est. 140 code + 160 tests) | 400 |
| 8 | slice 4a3 detectors: truncation, provider_protocol, detector order, LegStateStore size limit, spawning principal (D-2814, D103 split again; est. 150 code + 180 tests) | 400 |

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

Design reviews: seat A r1 (qwen run-050c742b @ecf5bd5d) D20-D25, r2 (qwen run-032bb8bc @0b3a01e2) D49-D50 (both cut to the 4 KB tail; D1-D19 and D30-D48 lost), r3 (mimo-v2.6-pro @0b3a01e2, full text) D60-D74, r4 (mimo-v2.6-pro @742727c8) D64 open + D80-D91. Every row names the findings it answers and its slice. From D-2814 on, each slice gets its own delta seat over only its rows; Plan-Slice `#4` = 4a, `#7` = 4a2, `#8` = 4a3, `#5` = 4b, `#6` = 4c (budgets in "## Slices"); build order 4a -> 4a2 -> 4a3 -> 4b -> 4c; a slice whose diff exceeds its budget fails the size gate and is split again by the writer, never merged over budget.

4a seat r1 (mimo-v2.6-pro run-0d06650a @aec0a2df) D82 open + D100-D109, 4a seat r2 (run-6488c77e @e63a4886) D103 open + D120-D125, folded below. Finding -> slice: 4a = D86, D87, D90, D100, D101, D102, D103, D107, D108; 4a2 = D81, D82, D85, D91, D104, D105, D106, D109; 4b = D64, D88; 4c = D80, D83, D84, D89 (and build-L1 conditions 1-5).

### Slice 4a (Plan-Slice #4, budget 400)

| piece | where | behaviour |
|---|---|---|
| limits reader (D49, D65, D90, D91, D102, D107) | `plangraph/settings.py` fixed accessor `spawn_limits(role)` -> frozen `SpawnLimits(wall_cap_min, max_cap_min, stall_min, stall_cpu_pct, ignore, floor_mb, max_runs, require_principal, truncation, provider_protocol)`; settings section `spawn_limits: {writer: {...}, seat: {...}, default: {...}}`; env `PLANGRAPH_SPAWN_LIMITS` (JSON, same shape) overrides for tests | role map is explicit: `writer` = `implement`, `fix`, `writer`; `seat` = `review`; `default` = every other role; `wall_cap_min`/`max_cap_min`: writer 90/180, seat 45/60, default 60/120; all roles `stall_min=15, stall_cpu_pct=2, floor_mb=2000, max_runs=4, require_principal=false`; `ignore` default `{}`; `truncation` default `{"opencode": ["Response still truncated after two output token limit continuations"]}` and `provider_protocol` default `{"opencode": ["tool call delta is missing id or name"]}` (one default each, used by 4a2); all three are maps harness -> list of strings and validation refuses any other shape; 4b/4c add their own fields; unknown keys refused like the rest of settings |
| exit vocabulary (D101) | `spawner/liveness.py` constants; no keeper schema change | `exit_class` keeps the keeper's closed vocabulary (`keeper/schema.py` `EXIT_CLASSES`, validated in `store_cards.py`, CHECKed in `schema_pg.py`, used by `routing.next_after_failure`); the liveness detail goes into a separate record field `liveness` with values `truncated, provider_protocol, stalled, capped, attempt_timeout, oom, memguard, killed, host_full` (a cut output is the separate flag `output_cut`, not a liveness value, D121); the mapping to `exit_class` is fixed: `stalled -> stall`, `capped`/`attempt_timeout -> timeout`, `truncated`/`provider_protocol`/`oom` -> `worker_crash` (the planner's `routing.next_after_failure` may retry the same leg under its attempt budget on a later run; the spawner's own fall-through inside one run never re-tries a leg, D124), `memguard`/`killed`/`host_full -> outage`; a hand cancel stays `exit_class=cancelled`, `liveness=None`; `next_leg` lives only in the spawner run record's attempt rows, never in keeper attempts (`routing._ATTEMPT_KEYS` unchanged) |
| run result (D50, D86, D100, I13) | `service.py` writes `runs/<run>/result.json` `{status, exit_code, exit_class, liveness, output_cut, attempts}` next to the record | a run is REFUSED as a seat or writer result when `liveness` is set OR `exit_class == "cancelled"` OR `output_cut` is true OR status is not `completed`; `gates/findings.failure_reason(text, status, is_error, exit_class=None, liveness=None, output_cut=False)` returns that reason before parsing; `seat_outcome(raw, ..., exit_class=None, liveness=None, output_cut=False)` passes them; `first_review(outputs)` accepts `{seat: raw}` as today or `{seat: (raw, result)}` where `result` is the parsed `result.json`; `findings_from_files(paths)` reads the sibling `result.json` when a path sits in a run dir and otherwise the card field `seat_classes: {stem: {status, exit_class, liveness, output_cut}}` (the same four fields as `result.json`, D120); `gates/admission.py` (the `seat_outputs` path) and `tools/lane_gates.py` pass them through; `_continue_writer` returns at once when the record is refused by the same rule |
| output on disk (D-2444, D-2795, D108, I17) | `service.py:87-89`, `cli.py:313` | the record keeps a 4 KB display tail; the full redacted stdout/stderr go to `runs/<run>/stdout.log`/`stderr.log`; up to 2 MB they are complete; beyond 2 MB the file keeps head 1 MB + tail 1 MB with a marker and the record sets `output_cut: true`, which refuses the run as a seat result (no verdict from a reply with a missing middle); `output_cut` never triggers a fall-through and does not change `status` (a writer's result is its commits, which the cut does not touch; a cut seat is re-run by the caller like any refused seat) (D121); `wait --json` returns the paths; seat parsing reads the file |
| cancel with class (D61, D74, D100) | `runner.cancel(job_id, liveness=None)` | sets `job.liveness` (or nothing for a hand cancel) BEFORE the kill, so the monitor sees it when the exit arrives (an own cancel always wins); a hand cancel ends `exit_class=cancelled`, which `routing._PARK_EXITS` keeps parking and the run-result rule refuses as a seat result; `_PARK_EXITS` is not changed |

4a tests: limits reader shapes, defaults per role and refusals; the liveness -> exit_class mapping table (every value lands in `EXIT_CLASSES`); `failure_reason`/`first_review`/`findings_from_files` refusing each liveness value, a hand cancel and `output_cut` (run-dir and `seat_classes` paths); output files under and over 2 MB; own cancel racing a -9.

### Slice 4a2 (Plan-Slice #7, budget 400; right after 4a)

| piece | where | behaviour |
|---|---|---|
| fall-through and terminal (D61, D73, D85, D109, I14) | `execute()` in `service.py` and `runner.py` | falls through to the next leg on liveness `truncated`, `provider_protocol`, `stalled`, `attempt_timeout`, `oom` (a leg or card-size fault), and only when at least 60 s remain before the run deadline; liveness `capped` (the run deadline itself) ends the run without a new attempt; `memguard`, `killed`, `host_full` are host faults and end the run (the caller moves or requeues it); at most 2 fall-through attempts, never a leg already tried; each attempt row records `{leg, liveness, exit_class, next_leg}`; a run whose attempts all ended in liveness values ends `status=failed` with the last attempt's `liveness`/`exit_class`, never `limited`/exit 4; `_save` merges `liveness` set-once and the error text lists each attempt's value |
| wall cap (D65, D71, D81, D91, D104, D105) | `SpawnService(clock=time.monotonic)` and `SpawnerRunner(clock=...)` take the SAME injected clock (the service passes its clock to the runner it builds); the service computes once per run `deadline = clock() + 60 * min(max(wall_cap_min, estimate_tokens / 1000), max_cap_min)` from the card it holds; `runner.spawn(..., deadline=None)` stores it on `JobInfo.deadline`; the run record stores it | each attempt waits `wait_for(..., deadline - clock())`; the cap covers all attempts of a run; `_continue_writer` gives its continuation the parent record's `deadline` (the continuation is another attempt of the same run, so writer + continuation together stay under the cap); over the deadline -> `cancel(job, liveness='capped')`; `PLANGRAPH_SPAWN_RUN_TIMEOUT` stays a per-attempt cap, its exit 124 becomes liveness `attempt_timeout` |

4a2 tests (attempt liveness values injected directly, the detectors come in 4a3): fall-through on truncated then attempt_timeout (terminal failed, attempts recorded); deadline reached -> `capped`, no new attempt; less than 60 s left -> no fall-through; memguard/host_full ending the run; deadline across two attempts and a continuation with one fake clock shared by service and runner.

### Slice 4a3 (Plan-Slice #8, budget 400; right after 4a2)

| piece | where | behaviour |
|---|---|---|
| truncation (D20, D21, D62, D82) | `liveness.truncated(harness, exit_code, stdout_tail, stderr_tail, patterns)` | the source is opencode's own structured error (opencode v2.1.0 binary, session runner): `{type: "output-limit", message: "Response still truncated after two output token limit continuations"}`, raised when the last three messages all finished with `length`; how `opencode run` prints it is not pinned, so on a nonzero exit the matcher accepts, as the LAST non-empty line of stdout or stderr, either (a) the plain message from `spawn_limits.truncation[harness]`, optionally prefixed `Error: `, or (b) a JSON object whose `type`, `error.type` or `error.name` is `output-limit` or whose `message`/`error.message` equals that message (the `--format json` error event); a writer quoting the phrase inside its reply is not the last line of a failed run; the fixture lines are the binary's exact string in both forms and the fixture file names that source and the evidence runs (D-2771, D-2806: 20261025-103823-pfr2-3185c3, logs on the workst); then `limited_above=<estimate_tokens>` for (leg, account, combo) in a new optional `LegStateStore` dimension, `plan_spawn` skips that leg only for cards at or above that size, expiry 6 h, never a plain `limited` |
| provider protocol error (build-L1, D-2820 evidence) | `liveness.provider_protocol(harness, exit_code, stderr_tail, patterns)` | evidence: mimo-pro run-cc3989f7 (bus R2a, coding.vm, mimo via OmniRoute) ended with 0 commits on the opencode stderr line `OpenAI Chat tool call delta is missing id or name`; today that run is a plain `failed` with no fall-through (no `unavailable_reason` marker, no provider stop); the matcher reads stderr only (the reply lives in stdout, so a quote cannot trigger it) and on a nonzero exit matches any stderr line containing a pattern from `spawn_limits.provider_protocol[harness]` -> liveness `provider_protocol`, which falls through like `truncated` (next leg, never the same leg, at most 2, terminal failed); on exit 0 it matches only when that line is the LAST non-empty stderr line (the run ended on the error), so a run that recovered from an earlier transient delta error keeps its reply (D125); a match on exit 0 sets `provider_protocol`, I13 refuses the run as a result, and it falls through too |
| spawning principal (D-2796, D69, D106, I18) | `service.py` record creation (spawn, continuation, `save_refusal`), `cli.py spawn --principal`; injected `parent_info=` reader (default reads the parent pid's argv[0]) | record `principal = {name, session, host, pid, cwd, source}` with `source` in `flag`/`env` (`PLANGRAPH_PRINCIPAL`, `CLAUDE_SESSION_ID`)/`fallback` (from `parent_info`); continuation and refusal records copy the parent's principal; self-asserted attribution, not authentication (open follow-up below); `require_principal` refuses only `source=fallback` for writer roles and ships off |
| detector order (D123) | `liveness.classify(job, ...)` | one attempt gets exactly one liveness value; first match wins in this order: own cancel (`job.liveness` set, or a hand cancel) > `capped` (run deadline) > `attempt_timeout` (exit 124) > `truncated` > `provider_protocol` > the 4c memory values (`oom` > `memguard` > `killed`); the attempt row also lists any later matches under `also`, for diagnosis only |

4a3 tests: truncation as the last plain line and as a last JSON error line of a failed run (stdout and stderr), not as a mid-reply quote, not on exit 0; provider_protocol: the exact run-cc3989f7 line on stderr with exit 1 -> `provider_protocol` + next leg, the same line as the last stderr line with exit 0 -> `provider_protocol` and refused, the same line earlier in stderr followed by a normal end with exit 0 -> no value (recovered), the same text only in stdout -> no value; detector order (exit 124 with a truncation last line -> `attempt_timeout`, `also: [truncated]`); LegStateStore size limit; principal on spawn, continuation and refusal records, fallback via a fake `parent_info`.

### Slice 4b (Plan-Slice #5, budget 300)

| piece | where | behaviour |
|---|---|---|
| polling monitor (D70, D88) | `runner._monitor_process` | `proc.communicate()` is replaced by two stream reader tasks (`asyncio.create_task`) that append to the run's log files, plus a loop `while proc.returncode is None: await self.sleep(tick); ...` with injected `sleep` and `clock` (defaults `asyncio.sleep`, `time.monotonic`; `tick` default 30 s); each tick checks the deadline from 4a and calls `liveness.check`; a fake `sleep` that advances the fake clock expires the deadline without real time, so I16 holds |
| stall rule (D22, D23, D63, D64) | pure `liveness.check(run, now, probe, limits)` | stalled only when ALL hold for `stall_min`: no new stdout/stderr bytes, no new commit on the run branch, no file under the cwd (outside `.git/` and `limits.ignore[harness]`) with new content, and process-group CPU < `stall_cpu_pct`; "new content" means a content hash not seen before for that path in the run (the probe keeps the last 8 hashes per changed path), so a loop that rewrites the same file with the same or cycling content gives no work signal and is stalled (D64); a recorded provider wait is `waiting` until its reset + 5 min, or 10 min when the 429/402 had no reset; a harness that waits silently with no output, writes or CPU for 15 min is stalled by design and falls through |

4b tests: fake clock + fake sleep; streaming run with no writes (alive); 39 min run with 0 commits, no output, 0.6% CPU (stalled, the run-51bc2b81 shape); same with a 30 min retry-after (waiting); same-file rewrite loop with cycling hashes (stalled); a real edit loop with new hashes (alive until the deadline).

### Slice 4c (Plan-Slice #6, budget 400; build-L1 GO D-2799 with conditions 1-5)

| piece | where | behaviour |
|---|---|---|
| limits for the scope (D80) | `SpawnLimits` gains `memory_max` (default `"2G"`, the rust golden) and `tasks_max` (256) | the scope argv takes them from `spawn_limits(role)`; admission warns in the record when the harness's projected RSS exceeds 80% of `memory_max` |
| scope for Python launches (D25, D60, D66, D84) | `runner.spawn` before `process_factory`, new `spawner/scope.py` | user manager reachable = `XDG_RUNTIME_DIR` and the bus socket exist and the STDOUT of `systemctl --user is-system-running` is not `offline`/empty (its exit code is ignored: `degraded` exits nonzero but is reachable, condition 3); argv = `systemd-run --user --scope --quiet --unit=autoos-worker-pg-<12 hex of run id>.scope -p MemoryMax=<memory_max> -p MemorySwapMax=0 -p TasksMax=<tasks_max> -- <cmd>`, each property as a separate `-p`, `K=V` pair like `scope.rs`; run id must match `run-[0-9a-f]{32}`; no `--collect`, so the unit's `Result` survives the exit for the OOM read (condition 1); golden test: the shared prefix (`--user --scope --quiet`, the three `-p` pairs, `--`) equals `scope.rs` `wrap()` output, with exactly two allowed differences: `--collect` (rust only) and `--unit=` (python only); Python launches stay unjailed (no `pg-jail`, no `env -i`), so 4c gives a memory ceiling, not isolation (condition 5); cancel = `systemctl --user stop <unit>`, then `killpg` as fallback; no user manager -> unscoped, record `scope: none`, admission still applies |
| OOM and memguard class (D67, D66, D83, D89) | pure `liveness.liveness_of(job, unit_show, memguard_lines)` (returns the liveness value; the 4a mapping gives the exit_class) | after the process exits and before anything else: `systemctl --user show <unit> -p Result -p MemoryPeak` (the unit persists because there is no `--collect`; the OOM policy is systemd's default `stop`, which marks the scope failed with `Result=oom-kill`), then `systemctl --user reset-failed <unit>`; `Result=oom-kill` -> `oom`; memguard is pinned from its source and real output: AutoOS `lib/linux/memguard.sh:363` writes `<utc> MemAvailable=<kib> stopped <unit>` to `$AUTOOS_MEMGUARD_LOG` (default `~/.local/state/autoos/memguard.log`; real lines on coding.vm, e.g. `2026-10-23T05:23:54Z MemAvailable=760040 stopped autoos-worker-...scope`; AutoOS test `tests/linux/44-memguard.sh`), so a line naming this run's unit at or after its start -> `memguard`; a -9 with neither, or an unreadable log -> `killed` (oom-or-killed, never a guess, condition 4); an own cancel (job.liveness set, or a hand cancel) wins over all of these |
| host admission (D-2785, D68, I15) | `spawner/host_load.py` `admit_host(probe, limits, harness)`, called from one shared function by both `_spawn` implementations and `_continue_writer` | runs are counted once per dedup key (cgroup path for scoped runs, else process group id); a plangraph record counts only when its pid matches pid + `/proc/<pid>/stat` start time; client processes (`opencode` with `run`, `qodercli`, `agy` with `-p`, `claude` with `-p`, by argv[0] basename whatever its directory, e.g. `/home/s/.local/bin/agy ... -p`; wrappers resolved through their process group; D-2823: the old fleet pattern missed agy) count only when their key is new; the memory check uses MemAvailable, which already includes other users' and containers' usage; projected RSS = p90 of `MemoryPeak` from the last 20 scoped runs of that harness on this host, never below the harness default (opencode 600 MB, qodercli 500 MB, agy 500 MB); refuse `host_full` when MemAvailable - projected < `floor_mb` (2000, D-2706) or counted runs >= `max_runs` (4); the refusal lists the counted keys |

4c tests: captured argv only (no real systemd), golden prefix against `scope.rs`; `is-system-running` stdout `degraded` with exit 1 (reachable) and `offline` (unscoped); unit-show `Result=oom-kill` (oom), memguard line for this unit (memguard), -9 with nothing (killed), own cancel racing all three; admission dedup (one run seen as record + scope + client counts once), an `agy --dangerously-skip-permissions --model ... -p` process from `~/.local/bin` counted (D-2823, bus R2a pid 2646480 shape) while an `agy` without `-p` is not, pid reuse, `host_full` at each of the three launch sites.

### Shared test seams and invariants

Test seams (D24, D71, I16): every `liveness.*` function is pure (injected clock, process/CPU probe, git probe, unit-show reader, memguard lines); the service and the runner share one injected `clock`, the runner also takes `sleep` and `tick`; the principal fallback reads through `parent_info`; `host_load` reads meminfo, `/proc` and unit data through an injected reader.

Invariants: I13 a run with a liveness value, a hand cancel or a cut output never counts as a writer result or a seat verdict (enforced in `failure_reason`, `first_review`, `findings_from_files` and `_continue_writer`), and `exit_class` always stays inside the keeper's `EXIT_CLASSES`; I14 every attempt records `{leg, liveness, exit_class, next_leg}`, liveness-only runs end `failed` (never `limited`), host faults and the run deadline never fall through; I15 no launch unless MemAvailable minus projected RSS stays >= 2000 MB and fewer than 4 runs are counted (D-2706); 4c adds a per-run memory ceiling, not isolation (Python launches are unjailed); I16 every liveness decision is a pure function of injected inputs; I17 seat and writer output up to 2 MB is kept in full on disk, and a run whose output was cut is never a seat result; I18 every run record (spawn, continuation, refusal) names its principal and the principal's source.

Open follow-up (D-2799 (5), interim, not done): the spawning principal is self-asserted and `require_principal` ships off. Still to do: an authenticated principal (keeper-minted run token checked by the spawner, like the provider-envelope nonce) and turning the refusal on for writer roles. Until then I18 means attribution only.
