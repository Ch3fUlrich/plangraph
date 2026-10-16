# Card: plangraph-L1-orch-setup (planning L1, Opus; parent workst-L1-autoos), 2026-10-16

State: verdict CHANGE D-1522 on 026540cd. Its 6 fixes plus the D-081/D-1485 floors are in the branch head, sent to routing [37547f] for re-judging.
- Ruling 5: the ROUTER applies the batch via apply_ops (principal router), after operator approval.
- The operator is away until ~02:30Z (D-1519). The router queues the approval question for their return.
- No lane starts before approval (tooling freeze D-1297, exception recorded in PH-ORCH-SETUP / PH-FLEET-CAPACITY).
- This session also owns routing:RT-P0-TRIAGE (D-1520, prio 1, evidence decision); see triage/ when started.
build-L1 DONE: KEEPER-PG cutover live 2026-10-16 16:43:16Z (D-1506b), main b3d71096. Keeper: postgres, schema keeper, role keeper_rt (no DDL).
Branch: lane/orch-setup-plan, worktree H:\worktrees\plangraph-orch-setup-plan, base origin/main 27c95c3.

Files:
- docs/plans/2026-10-15-rethink-setup-design.md and docs/plans/operator-rounds.md: verbatim base from routing
  rethink-setup @ 6ff782a. Body sha256 1958911c.. / ff41ee5c..; check with `sed '1,/^<!-- verbatim below -->$/d' F | sha256sum`.
- docs/plans/2026-10-16-orchestration-setup.md: the design (S1-S23; section 12 = fleet capacity D-1514).
- docs/plans/orch-setup/2026-10-16-orch-setup.yaml: batch (42 create, 13 edit, 8 cancel). Dry-run OK; no cycles, no missing deps,
  nothing left on a cancelled item (snapshot 20:34Z).
- docs/plans/orch-setup/briefs/: COMMON.md (start gate + lane order) + lanes A-K.
- Generator: scratchpad gen_batch.py (not committed); graph check: scratchpad check_graph.py (run in WSL).

Next:
1. Router re-judge; fix any findings (gen_batch + dry-run + check_graph), commit, push, send the sha.
2. Operator approval at return (router-queued; offer a dedicated workstation setup L1 as an option, D-1522 point 6).
3. Router applies. Then hand-off to workst-L1-autoos [096459]: YAML + brief paths + sha.
4. Persist decisions S1-S23 (structured-memory; Omnigraph open again, D-1521).

Open:
- The SPLIT-PUBLISH repo-name freeing step (operator).
- WSL Claude login expired (operator).

Facts:
- Claude Code 2.1.301 reads AGENTS.md natively only when there is no CLAUDE.md; `@AGENTS.md` works.
- Priorities: lower number = earlier. RT-P1 items are at 1; this track at 2, gates/SU-DOCS at 1, build-L1 P3 items at 3.
