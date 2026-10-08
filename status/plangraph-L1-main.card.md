# Status card — plangraph-L1-main (coding.vm, the only plangraph L1; D-639)
Updated: 2026-10-08 12:30 UTC | Brief: routing/docs/plan/briefs/plangraph-L1-2026-10-08b.md | Constraints: CONSTRAINTS.md

## Phase
Live tree (worktree-proposal) = main a01c8ed + sync fixes + review fix waves 1-2 + all 2026-10-08 batches, pushed.
plan.ohje serves the NEW UI (render --cdn; nginx :8089 file mount). Real-browser check (workst) PASS at
1440x900 / 1280x800 / 390x844 on fix1. origin/main is behind the live tip: fast-forward waits for routing.
Review record: docs/reviews/2026-10-08-since-1006-seats.md (seats gpt-oss + muse-spark; mediums fixed by W1-W3).

## Running
- W4 supersede rule (md_sync_dclose.collect + keeper _closes_token_sha): the later D-row wins. Closes CHAPTARR-ENV +
  9 D-548 keys once merged AND the keeper daemon runs the new code (restart needed).
- Research leg: agent task size → step 5 options to routing.

## Keeper (127.0.0.1:8787, verify ok)
2026-10-08 batches: Chaptarr (24), missing keys 6 (8), ROUTESETUP (24 + RS-GEMINI-PARAMS), server books/sablier (13),
D-649/650 (5), D-653 routing (9), plangraph reality (10 fix lanes). Backups keeper.db.pre-* (newest by ls -t).
Blocker: keeper.json plangraph ref = local `main` (f610d98, the initial commit) → no plangraph commit close can verify
(K1-KEEPER stuck since 10-03). Lane plangraph:KEEPER-REF-PLANGRAPH: set ref origin/main + restart (needs routing OK).

## Next (in order)
1. W4 review (1 seat, not the writer's family) → merge → daemon restart → md-sync → report the closes.
2. Closes D-row for routing once main is fast-forwarded: UI-TABLE-MODEL, UI-OVERVIEW-FIX, UI-REVIEW-W3, GRAPH-UI-BUILD,
   GRAPH-UI-VISCHECK (workst PASS), GRAPH-UI-RESEARCH (design note on main).
3. Open lanes: UI-QA-COUNTS, UI-TESTS-TABLE-QA, UI-CREATED-AT, UI-HASH-STATE, UI-NODE-BUDGET, RENDER-NEWUI-DETECT.
4. After autoos-L1 lands layered-planning (D-646): 3-line pointer in skills/plangraph-planning.

## Catch-up (D-653, 2026-10-08)
ZCode stand-in (10-05/06) work is on origin (ded3a03, 2d9d0b5); no keeper writes. Parked WIP 8dfa483 (G2b step 6) and
672eac3 (TB1) only survive as bundles in /home/s/fleet/taildrop/plangraph/ (ui-build clone gone); superseded by 6677ce5.

## Rules that bite most
Plan data never goes to free legs: writers and seats get a code-only git repo (src/tests/tools only). Writers and seats
come from different families (the spawner reports the writer family). Never delete this worktree (the units run it).
Files ≤ 400 lines (page_ui.js 417, page_side.js 518 are older debt → ROUTE-400LOC). D-649: terse messages.
