# L1 card · build-L1-0028 · 2026-10-31T17:45Z
## Goal
- Phase 0 critical path: md-sync split lane live -> RIN-HOME-LIFT + RIN-LEDGER-PREP close by D-row -> S09 cutover under judge GO; then GATEWAY-LOG-TOKEN-MINT MERGE-READY
## Keeper
- claimed: RIN-HOME-LIFT (in_review, since 17:40Z, D-row close D-3404), RIN-LEDGER-PREP (in_review, D-row close D-3393), GATEWAY-LOG-TOKEN-MINT (claimed, L2 gwtok-0022)
- ready next: RIN-CUTOVER-DATA (after HOME-LIFT done + GO), LE-KEEPER-DROW-CLOSES split lane
## Done
- 10-31T17:05Z D-3395 - md-sync re-armed: dry run 5 ops refused 0, timer+path active, apply error None
- 10-31T17:35Z KEEPER-COVERAGE-TRACK-REPO b30804a2 state done, qwen run-4d064251 + mimo-pro run-326d696e PASS, waiver D-3397
- 10-31T17:40Z RIN-HOME-LIFT 421d0bf6 plangraph-data PR#1 merged, evidence verified (ci none), submitted in_review
- 10-31T17:44Z RIN-CUTOVER-DATA - S09 GO-REQ sent to routing (sha 421d0bf6)
## In flight
- LE-KEEPER-DROW-CLOSES writer mimo-pro run-1a11f24f since 17:30Z next check on its completion
- GATEWAY-LOG-TOKEN-MINT build-L2-gwtok-0022 since 14:20Z next check on its MERGE-READY
- RIN-CUTOVER-DATA judge GO-REQ since 17:44Z next check on routing reply
## Planned
1. split lane: verify writer, full suite, push, PR, qwen seat, MERGE-READY, GO, merge, redeploy md-sync (repoint drop-in, dry run first)
2. after md-sync live: confirm HOME-LIFT + LEDGER-PREP done via D-rows; then S09 live under GO
3. GATEWAY-LOG-TOKEN-MINT MERGE-READY -> seats, GO-REQ (HIGH), merge, D-825 live
4. LE-* items prio 3 after phase 0 (D-3382)
## Open
- D-3405 S09 CONDITIONAL GO, before freeze: (1) re-rehearse at 421d0bf6 (rehearse.sh + repath dry run + stage-dropins --verify) into a new file naming the sha; (2) RIN-HOME-LIFT done; (3) add router-continuity step: new ROUTER_HOME, where router CLAUDE.md/.claude/skills/hooks/HANDOFF.md resolve, relaunch by fleet-launch/watchdog (never operator); router gets FREEZE-NOW, hands off, idles; (4) send checklist + rehearsal path to routing -> final GO | default wait | deadline none
## Traps
- J=/home/s/.claude/jobs/6f592267/tmp (seat-cov-leg.sh, wspawn.sh, mkseat_inline.py); older J 2f5d0308 + be3841c5 (mkseat.py)
- harness refuses git with vars/heredoc/loops in this session: plain single commands + Write/Edit only
- reference commit cbfcc0e9 (branch ref/mdsync-split-cbfcc0e9) stays LOCAL, never push (D-3401)
- hand-edit CI cap is 50 lines (ci_commit_gate HAND_EDIT_MAX_LINES); attribution trailers all in ONE last paragraph
- glm free limit hit; luna times out; muse opencode-go unavailable; gemini quota out
## Drill-down
- handoff: /home/s/code/plangraph-worktrees/build-L1/status/handoff-build-L1.md (gen-54 block, line 2)
