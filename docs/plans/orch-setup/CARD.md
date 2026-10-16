# Card: plangraph-L1-orch-setup (planning L1, Opus; parent workst-L1-autoos), 2026-10-16

State: judge target = the branch head that adds section 12 (fleet capacity, D-1514; supersedes 6087fe20), sent to routing [37547f]. Only the applier ruling is left to the router. fixes-L1 is retired (D-1455); the applier is the router or workst-L1-autoos, per the verdict. No apply or hand-off before the verdict. Do not push to the branch until the verdict (keeps the judge target fixed).
build-L1 DONE: KEEPER-PG cutover live 2026-10-16 16:43:16Z (D-1506b), main b3d71096. Lane F and the Python half of lane A are unblocked; base on main b3d71096 or later. Keeper: backend postgres, schema keeper, runtime role keeper_rt (no DDL).
Branch: lane/orch-setup-plan, worktree H:\worktrees\plangraph-orch-setup-plan, base origin/main 27c95c3.

Files:
- docs/plans/2026-10-15-rethink-setup-design.md and docs/plans/operator-rounds.md: verbatim base from routing
  rethink-setup @ 6ff782a. Body sha256 1958911c.. / ff41ee5c..; check with `sed '1,/^<!-- verbatim below -->$/d' F | sha256sum`.
- docs/plans/2026-10-16-orchestration-setup.md: the design (decisions S1-S23, system, contract, items, lanes; section 12 = fleet capacity D-1514).
- docs/plans/orch-setup/2026-10-16-orch-setup.yaml: batch (42 create, 13 edit, 8 cancel). `apply_batch --dry-run` OK; no cycles
  and no missing deps against the 2026-10-16 snapshot.
- docs/plans/orch-setup/briefs/: COMMON.md + lanes A-K (J fleet signals, K placement).

Next:
1. Wait for the routing verdict on the head. Apply its findings, rerun gen_batch.py + dry-run, commit, push.
2. Ask the operator for approval (AskUserQuestion).
3. On approval, SendMessage to workst-L1-autoos [096459] (old ref 9f94c1 is gone): YAML + brief paths + sha. It loads the batch and launches the lanes.
4. Then persist the decisions (structured-memory), only after FREEZE OFF (Omnigraph write freeze D-1398 L7, 2026-10-16).

Open:
- Seat conflict resolved (S17: gemini via agy writes; seats qwen + muse-spark; fallback mimo, then deepseek).
- The SPLIT-PUBLISH repo-name freeing step.
- Item rulings decided by the operator (S19): Q-API-PUSH merge, T2-SKILL cancel, AO-CTX-RECYCLE fold, CI-RUNS-ON-WORKST re-scope.

Facts:
- Claude Code 2.1.301 reads AGENTS.md natively only when there is no CLAUDE.md; `@AGENTS.md` works.
- WSL Claude login expired (operator).
- The local plangraph main checkout is 503 commits behind origin.
