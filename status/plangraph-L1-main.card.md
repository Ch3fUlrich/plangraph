# Status card — plangraph-L1-main (Sonnet 5.5, L1 orchestrator)
Updated: 2026-10-01 | Brief: /home/s/code/routing/docs/plan/briefs/plangraph-L1-main.md | Constraints: CONSTRAINTS.md

## Phase (M1 closed: operator approved C; M2a running)
1. Proposal — APPROVED C (M1 verdict, 6 conditions in CONSTRAINTS.md); operator confirmation pending
2. M2a keeper — writers running: wp1b import decisions (private), a1 store core (qwen3.8-27b-free pinned), a2 evidence+--cdn (nemotron-3-super-free pinned); contract docs/KEEPER.md
3. Tests — not started
4. Adoption report — not started

## Now
- Next: merge branches, then B stage (api, cli, mcp, importer) ; reviews = Sonnet subagent + 1 pinned third-family via spawn only; stop+report exact text on any refusal; disk 99% — keep small, nothing heavy
- New req 5a (L1 autonomy) + acceptance criteria are in CONSTRAINTS.md

## Next
- Reviews of WP2/WP3 (D-206 seats) BLOCKED: classifier denied seat A spawn and a log poll; seat B run 20261001-165012-rev-page-seatb-65572c unread. Need browser screenshots on workstation (no local browser)

## Waiting on
- routing-00: judgement of proposal; operator choice. D-206 note: spawner left review families unresolved

## Notes
- Initial commit f610d98 on main; work now in worktree .claude/worktrees/proposal (branch worktree-proposal).
- No remote; ask routing-00 before adding one.
- Spend: pending ledger (gateway usage unreadable, 403). Paid deepseek/spark runs: pin-probe 20261001-170002-pin-probe-deepseek-aaa5ec; a1 170020-a1-store-core-ds-ec7175; a2 170020-a2-evidence-cdn-ds-848e5f; b0 170929-b0-finish-split-ds-1677e2; b1 171430-b1-api-cli-ds-332c04; b2 171430-b2-mcp-importer-ds-61a519; h1 172011-h1-harden-store-ds-858132; h2 172529-h2-harden-evidence-ds-234fd4; b3 172600-b3-e2e-reaperfix-ds-60b595; seat2 171450-rev-evidence-seat2-spark-ab97b0. Do not read opencode session DBs.
- R-coord-01: do NOT move keeper-code master while child runs are live (H1 got a LEAK flag because I ff-merged B2 during it). Merge only after all children finish.
- H1 hit the 128k context limit with 45 failing new tests (WIP cfece84); continue as smaller tasks H1b (schema/db/auth/events) then H1c (store logic).
