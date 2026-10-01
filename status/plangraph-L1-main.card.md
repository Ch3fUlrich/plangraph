# Status card — plangraph-L1-main (Sonnet 5.5, L1 orchestrator)
Updated: 2026-10-01 | Brief: routing/docs/plan/briefs/plangraph-L1-main.md | Constraints: CONSTRAINTS.md (re-read after compaction)

## Phase
M1 CLOSED. M2a accepted by routing-00 (Opus spot review). HANDOFF WRITTEN docs/HANDOFF.md (Opus successor continues: X6b collect, X7 tiers, re-review, pilot keeper, load, router batch, render, then D-231 M2c research+proposal).
M2b (run-bound tokens, signed snapshot, outbox, anchor, off-host keeper) / M3 (tests+scorecard) / M4 (adoption) not started.

## State (branch worktree-proposal, worktree .claude/worktrees/proposal, last commit 1ef63ee)
- 497 pytest green (~85 s); src 6,167 lines, tests 6,201; all files <= 400. Code-only working repo for writers: $CLAUDE_JOB_DIR/tmp/keeper-code (never move its master while children run, R-coord-01).
- Throwaway round trip of data/plan-v3 (93 items): 0 validator problems, 19 ready, export==import, verify_chain/integrity clean. NOTHING loaded into a real store.
- Reviews (D-219 two seats): seat1 Sonnet subagent PASS (r5), seat2 muse-spark PASS (r5, only Lows); earlier FAILs fixed. Families unresolved in run records for deepseek/spark (spawner gap).
- Open Lows: invisible-unicode family names (admin-only; fix = [a-z0-9._-]+), cancelled waiver retained, waiver truthiness, waiver not bound to item (one D-id can waive many), verdict/evidence/decision not re-verified inside the write txn (id-set+epoch+state recheck only), merge-commit rejection skipped, lease clock/outbox/anchor = M2b.
## Waiting on
- routing-00: Opus spot review (triggers/authorizer/evidence); decision where the pilot keeper runs + when to load plan v3.
- Page screenshots need workstation (no browser here); coding.vm disk ~99%.
## Spend: pending ledger (gateway usage unreadable). Paid deepseek/spark run ids are in the writers' titles (pin-probe, a1,a2,b0,b1,b2,b3,h1,h1b-d,h1e,h2,x1-x5, seat2 rounds) — back-fill from gateway.
## Notes: D-212/D-217/D-219 pinned in CONSTRAINTS.md. Do not read opencode session DBs.
