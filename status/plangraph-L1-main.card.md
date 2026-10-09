# Status card — plangraph-L1-main (coding.vm, the only plangraph L1; D-639)
Updated: 2026-10-09 02:30 UTC | Brief: routing/docs/plan/briefs/plangraph-L1-2026-10-08b.md | Constraints: CONSTRAINTS.md

## Phase
Live tree (worktree-proposal) = main a01c8ed + sync fixes + review fix waves 1-2 + all 2026-10-08 batches, pushed.
plan.ohje serves the NEW UI (render --cdn; nginx :8089 file mount). Real-browser check (workst) PASS at
1440x900 / 1280x800 / 390x844 on fix1. origin/main is behind the live tip: fast-forward waits for routing.
Review record: docs/reviews/2026-10-08-since-1006-seats.md (seats gpt-oss + muse-spark; mediums fixed by W1-W3).

## Running
Cron d83d9251 (:17/:47, session-only). No writers. Keeper = plangraph-keeper.service on the supersede code; plangraph ref origin/main.

## Keeper (127.0.0.1:8787, verify ok)
2026-10-08 batches: Chaptarr (24), missing keys 6 (8), ROUTESETUP (24 + RS-GEMINI-PARAMS), server books/sablier (13),
D-649/650 (5), D-653 routing (9), plangraph reality (10 fix lanes). Backups keeper.db.pre-* (newest by ls -t).

## Next (in order)
See docs/handoff/PLANGRAPH-L1-HANDOFF-2026-10-09.md (open closes, parked proxy go-live, T0-WSCLONE, next items).

## Catch-up (D-653, 2026-10-08)
ZCode stand-in (10-05/06) work is on origin (ded3a03, 2d9d0b5); no keeper writes. Parked WIP 8dfa483 (G2b step 6) and
672eac3 (TB1) only survive as bundles in /home/s/fleet/taildrop/plangraph/ (ui-build clone gone); superseded by 6677ce5.

## Rules that bite most
Plan data never goes to free legs: writers and seats get a code-only git repo (src/tests/tools only). Writers and seats
come from different families (the spawner reports the writer family). Never delete this worktree (the units run it).
Files ≤ 400 lines (page_ui.js 417, page_side.js 518 are older debt → ROUTE-400LOC). D-649: terse messages.
