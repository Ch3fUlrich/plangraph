# Direct edit vs delegate: cost model (operator 2026-10-25, ORCH-EFFICIENCY-ENFORCE pt 1-2)

Operator: the 10-line hand-edit cap was an example, not a hard cap. Even a 50-line change is
cheaper done by the orchestrator than delegated, once brief writing and orchestration are counted.
The spawner `route` decides with a calculation, not a line cap.

## Measured inputs (coding.vm run records, 2026-10-25)
- 29 real writer runs (role implement, >= 60 s): 14 failed (p_fail 0.48), median 19 min, p75 33 min.
  This excludes queue wait at the host gate, which often ran 10-60 min today.
- Run records have no token counts. Recording them is a prerequisite: record `tokens_in`,
  `tokens_out` and `usd` per run (pt 10 metrics).

## What both paths pay (left out of the comparison)
- Reading the code to specify the change. An exact-spec brief (D-2739: file:function anchors,
  verbatim findings) needs the same reading as doing the edit.
- Seats, CI, merge, evidence.

## Delegate only
    cost_del = P_out*T_brief + N_del*P_cache*ctx + (W_run + P_out*T_check)/(1-p_fail(leg))
    time_del = queue(host, leg) + dur(leg)/(1-p_fail(leg)) + t_fill_push
- `T_brief`: brief output tokens, about 2-5k.
- `N_del`: extra orchestrator turns, about 6-10. Each turn re-reads the cached context `ctx`
  (100-200k). These are the turns for brief, spawn, stall watch, completion, result read, diff
  check, RUNID fill and push.
- `W_run`: writer run cost (catalog `cost_per_task_usd`).
- `T_check`: tokens to verify the writer's diff, about the diff size plus the report.

## Direct only
    cost_dir = P_out*T_edit + N_dir*P_cache*ctx + carry
    time_dir = lines * s_line + t_tests
- `T_edit`: about 12 tokens per changed line, times about 1.5 for retries of failed edits.
- `N_dir`: about 2-4 turns (edit, test, commit).
- `carry`: tokens the edit adds to the context (`T_edit` plus test output), re-read cached on
  every later turn until compaction.

## Decision (spawner `route`, action `direct_edit`)
    direct  if  cost_dir + L*time_dir  <=  cost_del + L*time_del
- `L`: dollars per hour of pipeline latency. It is set by the operator or router (policy).
  Latency is not free, because every queued item waits on it.
- Hard guards (security and limits, not efficiency):
  - AUTH_DOOR_PATHS is never edited directly (Hand-Edit refused there, as today).
  - The orchestrator's context fill must be <= 70%. Above that, delegate; the boot file
    (pt 5a) keeps relaunches cheap anyway.
  - The Claude quota headroom must stay above the policy floor, so a Claude limit cannot stop
    the run (R-coord-03).
  - The change must stay within one slice (<= 400 lines, D-2739).
- `route` prints both sides with numbers. The commit carries
  `Route-Decision: <run/route id>`, so the choice can be audited.
- With today's numbers, direct wins for any fully specified change up to a few hundred lines.
  Delegation pays off when the orchestrator does not already hold the context, the change
  needs exploration, the context is near its cap, or Claude quota is short.

## ci_commit_gate
- `HAND_EDIT_MAX_LINES` becomes a ceiling equal to the slice cap (400), not an efficiency rule.
- AUTH_DOOR_PATHS stays refused.
- Above 50 lines, the gate requires a `Route-Decision:` trailer whose route record says
  `direct_edit`.
- Interim until the slice lands: raise the cap to 50 (operator's figure), via one Gate-Edit
  with a router D-id.
