# Card: plangraph-L1-orch-setup (planning L1, Opus; parent workst-L1-autoos), 2026-10-16

State: DRAFT 2ca03a4e sent to routing-00. Its next generation (after the operator /clear) gives the verdict + 5 rulings. fixes-L1 is retired (D-1455); the applier is the router or workst-L1-autoos, per the verdict. No apply or hand-off before the verdict.
Branch: lane/orch-setup-plan, worktree H:\worktrees\plangraph-orch-setup-plan, base origin/main 27c95c3.

Files:
- docs/plans/2026-10-15-rethink-setup-design.md and docs/plans/operator-rounds.md: verbatim base from routing
  rethink-setup @ 6ff782a. Body sha256 1958911c.. / ff41ee5c..; check with `sed '1,/^<!-- verbatim below -->$/d' F | sha256sum`.
- docs/plans/2026-10-16-orchestration-setup.md: the design (decisions S1-S15, system, contract, items, lanes).
- docs/plans/orch-setup/2026-10-16-orch-setup.yaml: batch (32 create, 2 edit, 1 cancel). `apply_batch --dry-run` OK; no cycles
  and no missing deps against the 2026-10-16 snapshot.
- docs/plans/orch-setup/briefs/: COMMON.md + lanes A-I.

Next:
1. Send path + sha to routing-00 (judge).
2. Ask the operator for approval (AskUserQuestion).
3. On approval, SendMessage to workst-L1-autoos: YAML + brief paths + sha. It loads the batch and launches the lanes.
4. Then persist the decisions (structured-memory).

Open:
- Seat conflict resolved (S17: gemini via agy writes; seats qwen + muse-spark; fallback mimo, then deepseek).
- The SPLIT-PUBLISH repo-name freeing step.
- Router rulings: AO-CTX-RECYCLE-ALLHOSTS, T2-SKILL, API-PUSH, CI-RUNS-ON-WORKST.

Facts:
- Claude Code 2.1.301 reads AGENTS.md natively only when there is no CLAUDE.md; `@AGENTS.md` works.
- WSL Claude login expired (operator).
- The local plangraph main checkout is 503 commits behind origin.
