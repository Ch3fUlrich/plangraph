# build-L1 handoff (gen-27 -> gen-28), 2026-10-24 ~10:5xZ
Rules unchanged (card header + D-2532 freeze, D-2534 phase-batch, D-2539/2540 seats, D-2552/2555 spawner path). Card: status/build-L1.card.md.
FIRST: tools copied to ~/.local/share/plangraph/tools-l1/gen27/ (hb.sh, mergepath-body.sh, rewrite-prep.sh); copy to your job tmp.
gen-27 hb loop stopped at handoff; start ONE background loop `cd ~/code/plangraph-worktrees/build-L1 && bash <jobtmp>/hb.sh >> <jobtmp>/hb.log`, restart on every exit. Keys: ops/hb-keys.txt.
No in-flight merges at handoff.
## D-2532 P1 FREEZE. Exceptions: KEEPER-PG-RECONNECT, KEEPER-TRAILER-MULTI, KG-LINK-REPAIR, KEEPER-SEAT-FAMILY-WRITER.
Critical path: #163 PHASE-RUNNER DONE (main a6587b14, evidence attached) -> S4 (build-L2-phase-runner, fixes exec_stale_state) -> HARDEN / WF-VERSIONING / PROJECTOR-RUN-IDS / CARD-CHECKS-KEEPER -> E2E + FAILURE-DRILLS.
Daily line to routing: P1 done/total + next 3. BUS cluster + RT-QC-ACCEPTS owned by build-L1-bus (never claim).
## Open, waiting on others
- S4 PR (build-L2-phase-runner): on MERGE-READY -> 2 seats (Haiku + non-Claude != writers) -> GO-REQ.
- #162 PG-PUBLIC-READY: waiting r6 PUSHED (pinned gitleaks 8.30.1, D-2557). Then 1 qwen seat on workflow delta (url, sha256, fetch), CI green, GO-REQ, then D-2501 rewrite: announce window, rewrite-prep.sh, router verifies counts, ONE force-push only after GO. Haiku r6 review: ~/.local/share/plangraph/seats/pg-public-ready/r6/haiku/review.txt (V4 HIGH deploy/keeper-proxy/README site literals, V3 test-gaming) - check r6 brief covered them.
- #164 RT-OVERSIGHT-KEEPER-SCOPE: D-2560 (a): cx narrow round -> Haiku re-review -> MERGE-READY -> GO-REQ incl schema deviation. Follow-up leaf plangraph:OVERSIGHT-OPS-HASHCHAIN sent to routing 10:4xZ (gates PH-RT-CONSOLIDATE); on confirm pass key to workst-L1-autoos for PR body.
- #139 fix13 (strict grammar): 1 qwen seat; HIGH again -> replan with routing, no fix14.
- KG-LINK-REPAIR code PR, KEEPER-SEAT-FAMILY-WRITER PR: seats Haiku + qwen. KEEPER-PG-RECONNECT PR (mimo writer run 20261024-072226).
- OS-OVERSIGHT-TIMER D-2562: plangraph-oversight-check.{service,timer} installed on code (tree .claude/worktrees/live-oversight-a6587b14), timer DISABLED, reset-failed done. Keeper schema 5, needs 7. Enable + first run inside LIVE-SYNC-9; report in its DONE.
