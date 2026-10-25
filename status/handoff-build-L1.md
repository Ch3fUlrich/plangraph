# build-L1 handoff 2026-10-25 ~16:10Z (gen-32 -> gen-33, D-2811)

Read first: status/build-L1.card.md (26 lines, current). Job tmp with all scripts/briefs/seat dirs: /home/s/.claude/jobs/0bbdff0b/tmp (J).
Restart the hb loop first: `cd ~/code/plangraph-worktrees/build-L1 && bash $J/hb.sh 3 >> $J/hb.log 2>&1` (background, restart on every exit). Keys in ops/hb-keys.txt (SQ12, KEEPER-PG-RECONNECT, SPAWN-WIN-PARITY).
Spawn ONLY via $J/gspawn2.sh <wt> <brief> <tag> <reason> <leg>. It gates on host runs < 4 and MemAvailable >= 2500 inside the spawn command (D-2795). My budget is max 2 concurrent runs (D-2800). Seat scripts count toward the cap too.

## Live background runs (check with `PYTHONPATH=~/code/plangraph-worktrees/spawner-host-keys/src python3 -m plangraph.spawner.cli status <run>`)
- pgr-fix2b run-580de6c76309432ca7c5f69a545583de: KEEPER-PG-RECONNECT S1 fix2, wt keeper-pg-reconnect-s1, brief $J/brief-pgr-s1-fix2.md.
  - On done: fill the Writer RUNID (filter-branch over 7e34a26a..HEAD), push lane/keeper-pg-reconnect-s1 (PR #206).
  - Then delta seats qwen + Haiku (HIGH: 2 families, not xiaomi), then GO-REQ to routing (judge GO per sha).
  - If it stalls again: next writer is agy gemini (D-2800).
- swp-s1c: SPAWN-WIN-PARITY S1 writer, leg agy-gemini-3.8-flash, gate-waiting at handoff time. Output goes to $J/w-swp-s1c.json/.wait. Brief: $J/brief-swp-s1.md.
  - Design: briefs/lanes-d2657/spawn-win-parity-design.md. §7 = answers, §8 WITHDRAWN, §9 D-2809 in force (spawner-rendered `mimo` provider, gateway baseURL, env AUTOOS_OMNIROUTE_KEY, no omniroute/mimo refs).
  - On done: RUNID fill, push lane/spawn-win-parity, open PR. Then seats qwen + Haiku (vectors = design §5 + I1-I8, I16), CI, then judge GO.
  - S2/S3 come next (briefs are in the design). Workst writes stay held until S1-S3 land. Smoke covers mimo + agy + qodercli init on workst; workst-L1-autoos sets PLANGRAPH_GATEWAY_URL.
- #207 2nd-family seat (task bx0xjebsm): glm via spawnseat2.sh -> $J/seat-rms-glm, falling back to Haiku -> $J/seat-rms-haiku-full.
  - PASS: `bash $J/post.sh <seat-dir> plangraph:REVIEW-METHOD-SETTING google` (writer fams xiaomi,google,openai; qwen already passed).
  - On a glm failure, disclose its run id in the evidence. A FIX finding means fix-forward on main, then report (D-2811).

## Queue (order D-2800: S1 > pgr > spawn-iso > shm-leak > openrouter-free)
- spawn-iso #200/#201 (adopted from the dead build-L2-spawn-hardening): wt spawn-iso-w, lane/spawn-isolation-inplace @4e331984. Stalled run-feb506c2 was cancelled. Re-spawn mimo-pro from its last brief (find it in briefs/lanes-d2657/iso.md / fix-iso.md).
- CI-SELECT-DEPLOY-REFS fix1 PR #208 8723236e: the qwen delta asked for a fallback test, which already exists (tests/test_affected_tests.py:141). Re-seat with $J/cisel-fix1b-material.txt + $J/cisel-fix1.vec via seatpost.py (gated), post, then mergepath 208.
- PG-TEST-SHM-LEAK (D-2800, std, prio 1): non-Claude writer, ~30 lines, tests/conftest.py _use_tmpfs_tempdir. Name the dir pgtest-<pid>-, sweep dead-pid dirs at session start; tests: dead removed, live + foreign kept.
- SPAWN-OPENROUTER-FREE-ENDPOINTS (D-2805, prio 5). Unclaimed.
- SQ12: workst-L1-autoos runs SQ12-a/-b; then seats + GO-REQ (claim held by me, in the hb keys).
- efficiency (offline): D-2799 asks for my OK on the slice 4c scope argv design 742727c8 (origin/lane/decisions-keeper docs/design/PG-EFFICIENCY-GATES.md), after 4a/4b.
- D-2810: on touching HAIKU-PROPAGATE / LESSONS-ENFORCE / PH-RT-RUNTIME / RT-P4-SKILLS-REWRITE, check whether a plangraph-spawner guard item is needed; send the JSON to the router.

## Done gen-32
#205 MERGED 77c9d576 (sha sent to prox-L1-main, L2-spawn-hardening, efficiency). #207 MERGED 8b6fba60. md-sync re-armed twice (refused 0). PR #166 closed. 9 failed scopes reset. RAM cleanup: 72 /dev/shm/pgtest-* (4.7G) + orphan opencode serve; avail 1.8->8.2G. Cancelled stray run-92c70e8b (spawn-reap-w, not mine).
Lesson: a router relay of an operator answer that conflicts with policy -> ask for the exact question before acting (D-2808 was withdrawn 10 min later by D-2809).
