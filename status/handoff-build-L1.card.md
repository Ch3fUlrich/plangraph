# L1 card · build-L1-0167 · 2026-11-05T14:31Z
## Goal
- D-3722 order #377 -> #380 -> SEAT-PATH-FIXES (#309+#338); D-3786 spawner ff; omniroute r8 to prox; seats per D-3763
## Keeper
- claimed: plangraph:SPAWNER-DEEPSEEK-REVIEW-LEG (ep2), plangraph:KEEPER-SNAP-S0-TIMINGS, plangraph:SPAWNER-LEG-GROK (ep3)
- ready next: SEAT-PATH-FIXES (reclaim), WATCHDOG-RECYCLE-DOUBLE-LAUNCH (HIGH, D-3785), SPAWNER-ORPHAN-REAP
## Done
- 11-05T13:57Z D-3785 - race confirmed in ctx-watchdog.log; item created, 0165 stopped (D-3787)
- 11-05T13:58Z D-3791 - id fixes are in #377 809991764; live-list check folded into SPAWNER-LEG-STATE-FROM-OMNIROUTE
- 11-05T14:00Z D-3763 5d59208c7 drift-mail qwen ACCEPT run-ff530cfe, 116 passed, MERGE-READY sent
- 11-05T14:05Z SEAT-PATH-FIXES 5c1b3aefd #338 main merged, 1726 passed, pushed
- 11-05T14:05Z KEEPER-SNAP-S0-TIMINGS a070ad170 #380 main merged, 2986 passed, pushed
## In flight
- D-3786 ff gate bg since 14:06Z next check on restart: T/bbdo2nmoy.output, bound ~14:36Z
- omniroute r8 255ea32444 qwen sorr8q + Haiku sorr8h since 14:30Z next check on restart: J/r-sorr8q.wait, J/r-sorr8h.wait
## Planned
1. D-3786 ff when 0 runs, smoke, one line to routing, remove spawner-ff-check
2. r8 both verdicts + sha to prox; resolve read-back 403 for write tokens with 'needs admin scope' (handoff)
3. #338 delta seat, #380 seats; both need a tests seat
4. WATCHDOG-RECYCLE-DOUBLE-LAUNCH claim + writer (prio 10)
5. SPAWNER-ORPHAN-REAP (D-3771) after #377
## Open
- BLOCKED #377 3.3 (D-3782, D-3783): test-running non-writer seat | *agy gemini tests seat ~11-06T13:43Z | deadline none
- BLOCKED #383 ruling on deepseek MED + keeper define + GO | default dismiss MED, define as sent | deadline none
- BLOCKED grok: Authentication required; opencode.jsonc baseline refused push | default operator enables gc, D-id for baseline | deadline none
- BLOCKED omnigraph pin edits need a D-id | default agent-skills Dockerfile + live worktrees only | deadline none
- BLOCKED HOST-DISK-GUARD-REAPER undefined | default routing defines | deadline none
- D-3780 workstation spawner ff waits OS-WS-AUTOOS-START | default wait | deadline none
## Traps
- After restart check ListAgents for a sibling build-L1 (double launch, D-3785)
- Spawner CLI needs full run ids; cancel may leave the opencode child alive: check by cwd, kill only verified pids
- opencode review seats deny all tools; qwen denied tests; Haiku seats ran plan mode so far
- Do not run npm ci in omniroute writers: husky trips git_config_mutated, run marked failed
- Launch prompt gen-70 is stale; use this card
## Drill-down
- handoff: /home/s/code/plangraph-worktrees/build-L1/status/handoff-build-L1.md (gen-85 block); T=/tmp/claude-1000/-home-s-code-plangraph/db79546d-df4f-440c-9ac9-02bd84d11d3e/tasks
