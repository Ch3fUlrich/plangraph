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
