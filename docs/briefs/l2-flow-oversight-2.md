# L2 brief: build-L2-flow-oversight-2 (recycle of build-L2-flow-oversight ec773009, 2026-10-22)
Read first: docs/briefs/l2-flow-oversight.md (rules + lanes; this file overrides it) and /home/s/code/plangraph-worktrees/flow-oversight.card.md (predecessor card).
Rules: docs/plan/rethink/briefs/l2-rules-common.md (D-2148 writers, D-2205 seats, D-2202 mimo prefixed id, Authorship D-2046/2047, FILES line D-2198).
Corrections to the card (build-L1 state 2026-10-22 ~15:40Z):
- #90 RT-P1-CI-GATE is DONE (merged e3e6c0a4, D-2209 + branch protection D-2211, docs #102 519b9ba1). Main requires ci-gate.
- #87 merged 25fcd889, keeper in_review (HIGH). Seats on eafb7695: qwen pass (does not count: qwen wrote #87), glm pass (advisory, no zhipu principal), deepseek-v4.1-free fix, mimo fix. Shared real gap -> follow-up.
Lanes, in order:
1. lane/flow-unblock-followup (wt /home/s/code/plangraph-worktrees/flow-followup off 519b9ba1), Plan-Item: plangraph:RT-P1-FLOW-UNBLOCK, tests only:
   (a) a NON-router add_dep via apply_ops on a claimed item is refused (assert the refusal code); if it is NOT refused, stop and tell build-L1 (HIGH fix);
   (b) test_add_dep_on_a_claimed_item_is_a_router_edit asserts the item_defined (delegated) event;
   (c) test_claim_succeeds_without_base_when_git_or_head_unavailable: length assert before zip().
   FILES: tests/test_keeper_deps_ops.py, tests/test_keeper_evidence_wiring.py (+ an apply_ops test file only if needed), CHANGELOG.md. Non-Claude writer (mimo/mimo-v2.6-flash prefixed > deepseek-direct-flash), Writer trailer, PR, ci-gate green, MERGE-READY <PR> <sha>. build-L1 runs the seats.
2. RT-OVERSIGHT-CHECKS + RT-OVERSIGHT-KEEPER-SCOPE (HIGH): spec ~/code/plangraph-worktrees/flow-oversight-spec.md, wt flow-oversight; red-tests writer run 20261022-150645-oversight-red-tests-mimo-d7c4e7 (check result via autoos-agent result, apply, red run) -> GO-REQ to build-L1 before the implementation writer.
3. KEEPER-OPTASK-CLOSE (HIGH) -> SEAT-VERDICT-FAMILIES -> SEAT-VERDICT-CLAUDE-SEAT (D-2206; red-first + GO-REQ each). Then CI-COMMIT-GATE-HARDEN (HIGH, D-2209 DC).
Item text: ~/.local/share/plangraph/relay/flow-oversight.md (+ ops/pg.sh show via build-L1 if missing).
Report to build-L1 only: MERGE-READY / BLOCKED / GO-REQ. No acks. RECYCLE-READY <card> before 230k.
