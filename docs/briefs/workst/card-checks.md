# L2 brief: build-L2-card-checks   (lane C)
## Lane: plangraph:RT-P1-CARD-CHECKS (standard: 2 non-Claude seats, family != writer)
No branch yet: create lane/rt-p1-card-checks off origin/main. Read docs/p1-audit is PRIVATE (not in this repo): work from the item text below + code anchors
(readiness.py:222-240, tools/seat_verdict.py:137-141, rust/worker*, gate activity). Note: #94 RT-GATE-WIRING (gate as named checks) is being finished in parallel by build-L2-spine;
the diff-scan check goes into the gate check list it introduces: fetch lane/rt-gate-wiring-r and build on its check API (merge it in once #94 is on main); if it is not merged when you
need it, keep your gate change in a separate commit so it rebases cleanly by merge. Split writers per file group (worker pre-attempt, forbid paths, gate diff scan, held-out/smoke/exports).
