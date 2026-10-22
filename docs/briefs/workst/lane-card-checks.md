# L2 brief: build-L2-card-checks   (lane C)
## Lane: plangraph:RT-P1-CARD-CHECKS (standard: 2 non-Claude seats, family != writer)
No branch yet: create lane/rt-p1-card-checks off origin/main. Read docs/p1-audit is PRIVATE (not in this repo): work from the item text below + code anchors
(readiness.py:222-240, tools/seat_verdict.py:137-141, rust/worker*, gate activity). Note: #94 RT-GATE-WIRING (gate as named checks) is being finished in parallel by build-L2-spine;
the diff-scan check goes into the gate check list it introduces: fetch lane/rt-gate-wiring-r and build on its check API (merge it in once #94 is on main); if it is not merged when you
need it, keep your gate change in a separate commit so it rebases cleanly by merge. Split writers per file group (worker pre-attempt, forbid paths, gate diff scan, held-out/smoke/exports).

## Keeper item text (read-only copy)
plangraph:RT-P1-CARD-CHECKS | Card check quality: red-first check, protected check files, test-gaming diff scan
state claimed | kind lane | track RETHINK | prio 7 | epoch 2
claimed_by build-L1 | delegate - | lease 2026-10-22T16:38:08Z | reviewer -
deps plangraph:RT-P1-PLANNER-KIT=done
unblocks plangraph:PH-RT-RUNTIME, plangraph:RT-P1-E2E
desc Audit G6 (D-1998), research docs/p1-audit/research/c-cards-contracts.md and b-gates-merge.md: readiness only dry-runs check_cmd (readiness.py:222-240), held-out is optional, smoke_checks and exports are never run, test gaming is judged only by the reviewer self-report (seat_verdict.py:137-141). Design D8 asks for a real deterministic check per unattended card.
done Worker pre-attempt runs the check on the base sha in the card worktree: if it already passes, a change card parks unless check_kind=regression; files the check reads are forbid paths for the writer unless listed in the write set; gate activity scans the diff for deleted/skipped tests and removed asserts (finding -> check_fail with reason); held-out check required for risk-tagged cards; smoke_checks run; exports signatures verified after the attempt; tests; 2 seats of non-Claude families different from the writer (D-2148 order).

