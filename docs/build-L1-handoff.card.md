# build-L1 card — 2026-10-16 ~03:2xZ (gen-4 5fadc0, ctx 264k, recycle pending)
Goal: rethink P1 bootstrap (plangraph RT-P1-*). Brief /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md; detail docs/build-L1-handoff.md + status/build-L1.card.md (log, FOR ROUTER, FOR OPERATOR); operator rounds docs/plan/rethink/build-operator-rounds.md (binding). Branch build-L1 (never merged).
Rules: D-1377 NO crons (wait = one background cmd that exits); D-1364 msgs only BLOCKED/DONE/MERGE-READY/GO/incident/launch; D-935 defaults; output caveman.
Start: skill unattended-orchestration + main-orchestrator.md -> SKILL-READ; heartbeat 7 leases (`ops/pg.sh heartbeat plangraph:<KEY>`) FIRST.

## Main = 27f5604 (merged: #8 #14 KEEPER-PG W1-3, #11 CI-PARALLEL, #12 REVIEW-CODE, #15 PLANNER-KIT)
## Next
1. CI on workstation (operator order; also: triggers pull_request->main only + cancel-in-progress; 4 runners workst-plangraph-1..4 READY, 8g each; job killed ~03:05Z on runner -1 during scale-up = infra, rerun): runner `runs-on: [self-hosted, workst]` READY (workst-L1-autoos; ephemeral, 1 slot, docker socket, services ok, not reboot-persistent). Sonnet subagent was switching all workflows on lane/ci-runs-on-workst (draft PR) — check `gh pr list`; judge green-on-workst, merge (plangraph:CI-RUNS-ON-WORKST). GitHub-hosted jobs refused (billing).
2. PR #13 WORKER-CODE (HIGH): fix 22922ec (cancel via supervise_with_cancel, janitor on native cancel) awaits CI on workst + qwen delta seat (L2 build-L2-worker-code, workstation bridge session_01HYhk6BmNF9o65Z6McuQxJE). Seats: openai worker-code-seats/openai-37d4a2b (pass, V11 found by judge), qwen worker-code-seats/qwen-37d4a2b. Spot-check fix, merge.
3. Close with evidence+submit+verdicts: KEEPER-PG-CODE (seats keeper-pg-seats/final-221e9b2 both families valid, all refuted), CI-PARALLEL, REVIEW-CODE (seat review-code-seats/qwen1), PLANNER-KIT-CODE (planner-kit-seats/qwen-d01c678). TRAP: `pg evidence --kind commit` = router-only, `--kind ci` unknown -> find correct kinds (docs/KEEPER.md, CLI).
4. build-L2-inbox: PR #18, local 3004 passed, seat gpt-oss pass (nit fix in flight); CI waits for workst switch, then merge main + push. Live L2s: build-L2-projector 19f2b154 (HIGH; Migration 3; copy_to_pg TABLES approved), build-L2-inbox 93ce361d (Migration 4). Silent since ~20:45Z — check alive. build-L2-worker-code (above). Retired: keeper-pg, backup, review-code, planner-kit.
5. AO-DEEPINFRA-REMOVE: brief docs/plan/rethink/briefs/l2-deepinfra-remove.md; launch after fixes-L1 merges priv-writers; fixes-L1 merges.

- Test policy (operator 03:4xZ): small changes = affected tests only, no full suite/CI; CI path filters in the CI-switch PR. Rule in briefs/l2-rules-common.md.

## FOR ROUTER
- L2-LAUNCH build-L2-deepinfra (after priv-writers merge).
- Edit server:RT-P1-PG-DEPLOY criteria: B2 off-host optional (D-1375).
- Incident: GitHub Actions billing refusal (all repos?).
## FOR OPERATOR
- Fix GitHub billing (or confirm workstation-only CI is final). Worker-code writers used paid deepseek via omniroute. keeper-pg leftovers ran as Sonnet subagents (spawner floor 4608 MB refuses; D-1352 pending).

## Traps (measured)
- Spawner admission 4608 MB floor refuses L3 on coding.vm (MemAvail 3.5-4.5 GB) -> Sonnet fallback, record it.
- Writer leg 1 `omniroute/mimo-v2.6-flash` OK since live combo pin (evidence /home/s/seat-evidence/mimo-combos-d1375/); 402/OpenRouter id = incident. Deepinfra removed.
- Seats: gpt-oss `ops/seat.sh ovh/gpt-oss-120b`; qwen via qodercli only; append ops/tail.md; verdict line format rejects bold/U+2011; save prompts durable (job tmp vanishes).
- Workstation files: Taildrop -> `tailscale file get <newdir>`, verify sha256.
- Migration numbers reserved: projector 3, inbox 4 (planner-kit used none).
