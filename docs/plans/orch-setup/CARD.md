# Card: plangraph-L1-orch-setup (planning L1, Opus; parent workst-L1-autoos), 2026-10-16

State: judge target lane/orch-setup-plan @ 1ef8026c (supersedes 2ca03a4e), queued at routing-00 behind the KEEPER-PG cutover write pause (D-1506, ~15-20 min from 2026-10-16). Verdict + 5 rulings follow the cutover. fixes-L1 is retired (D-1455); the applier is the router or workst-L1-autoos, per the verdict. No apply or hand-off before the verdict. Do not push to the branch until the verdict (keeps the judge target fixed).
build-L1 DONE: KEEPER-PG cutover live 2026-10-16 16:43:16Z (D-1506b), main b3d71096. Lane F and the Python half of lane A are unblocked; base on main b3d71096 or later. Keeper: backend postgres, schema keeper, runtime role keeper_rt (no DDL).
Branch: lane/orch-setup-plan, worktree H:\worktrees\plangraph-orch-setup-plan, base origin/main 27c95c3.

Files:
- docs/plans/2026-10-15-rethink-setup-design.md and docs/plans/operator-rounds.md: verbatim base from routing
  rethink-setup @ 6ff782a. Body sha256 1958911c.. / ff41ee5c..; check with `sed '1,/^<!-- verbatim below -->$/d' F | sha256sum`.
- docs/plans/2026-10-16-orchestration-setup.md: the design (decisions S1-S19, system, contract, items, lanes).
- docs/plans/orch-setup/2026-10-16-orch-setup.yaml: batch (32 create, 8 edit, 4 cancel). `apply_batch --dry-run` OK; no cycles
  and no missing deps against the 2026-10-16 snapshot.
- docs/plans/orch-setup/briefs/: COMMON.md + lanes A-I.

Next:
1. Wait for the routing-00 verdict (judge request sent: 1ef8026c). Apply rulings, rerun gen_batch.py + dry-run, commit, push.
2. Ask the operator for approval (AskUserQuestion).
3. On approval, SendMessage to workst-L1-autoos: YAML + brief paths + sha. It loads the batch and launches the lanes.
4. Then persist the decisions (structured-memory).

Open:
- Seat conflict resolved (S17: gemini via agy writes; seats qwen + muse-spark; fallback mimo, then deepseek).
- The SPLIT-PUBLISH repo-name freeing step.
- Item rulings decided by the operator (S19): Q-API-PUSH merge, T2-SKILL cancel, AO-CTX-RECYCLE fold, CI-RUNS-ON-WORKST re-scope.

Facts:
- Claude Code 2.1.301 reads AGENTS.md natively only when there is no CLAUDE.md; `@AGENTS.md` works.
- WSL Claude login expired (operator).
- The local plangraph main checkout is 503 commits behind origin.
