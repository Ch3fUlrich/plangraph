# build-L1 card — rethink P1 (D-1285, batch D-1303) · gen-5 (successor, 2026-10-16 03:2xZ)
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md · design (binding, 14a): .../docs/reports/2026-10-15-rethink-setup-design.md
Branch build-L1 = control branch (never merged). Older log: `git log -p status/build-L1.card.md` (gen-1..4). Handoff: docs/build-L1-handoff.card.md.
Rules: D-1377 no crons; D-1364 msgs only BLOCKED/DONE/MERGE-READY/GO/incident/launch; D-935 defaults, logged below.

## Main = 7ec2bb5 (CI on workst runner since #17 5188310)
DONE in keeper (commit+ci evidence + seat verdicts): RT-P1-CONTRACT, CI-PARALLEL (openai), RT-P1-KEEPER-PG-CODE (openai+alibaba),
RT-P1-REVIEW-CODE (alibaba), RT-P1-PLANNER-KIT-CODE (alibaba), RT-P1-PROJECTOR-CODE (openai+alibaba; PR #16 merged 7ec2bb5 03:3xZ).

## Open
| Item | State | Next |
|---|---|---|
| RT-P1-WORKER-CODE (HIGH) | claimed; PR #13 bff8725 HOLD | qwen delta seat pass (worker-code-seats/qwen-delta-22922ec, sha ok). HOLD 03:4xZ: rust-worker attempt 1 failed e2e cancel 25s timeout (job 116791673094) -> suspected early-cancel race (signal before attempt starts = V11 again). L2 to verify workflow.rs, add 0s/pending-cancel e2e, poll instead of 2s settle, attempt-1 green. Then merge + 2 verdicts (openai worker-code-seats/openai-37d4a2b) |
| RT-P1-INBOX-CODE | claimed; PR #18 MERGED main b5f45c9 (CI 38845394565 ✓✓) | evidence REFUSED: head 1cb7acc (merge) has no Plan-Item trailer; trailer commits (68a9180) have no CI runs (pull_request-only CI). Router: add_evidence_router commit 68a9180 (or commit+ci on 1cb7acc). Then submit + verdict openai seat inbox-seats/r2-1cb7acc (writer anthropic). L2 stood down. |
| CI-RUNS-ON-WORKST | open, blocked on autoos:CI-WORKST-RUNNER | code merged (#17, trailer 813951d); claim+evidence once workst-L1-autoos closes CI-WORKST-RUNNER |
| AO-DEEPINFRA-REMOVE | waits priv-writers merge (fixes-L1) | brief docs/plan/rethink/briefs/l2-deepinfra-remove.md |
| server:RT-P1-PG-DEPLOY | open (server-L1-main) | gates KEEPER-PG/TEMPORAL-CHECK/RT-P1 |
Leases: heartbeat claimed items each beat (`ops/pg.sh heartbeat plangraph:<KEY>`); last 03:2xZ.
L2s: projector told to stand down (DONE). Retired: keeper-pg, backup, review-code, planner-kit (sessions still listed idle).

## Tools
- Verdicts: `python3 <(git show origin/main:tools/seat_verdict.py) --key K --seat-dir D --writer-family F --token-dir ~/.config/plangraph`.
- `ops/qseat.py OUT [--adjudication ADJ] part.json...` builds a seat dir (review.txt + evidence.json) from qodercli out.json parts;
  multi-part seats with refuted fix findings pass only with the adjudication file. Evidence kind = `commit+ci` (Plan-Item trailer sha on main).

## FOR ROUTER (paused until ~04:10Z)
- INBOX evidence (see Open). lesson: L2 PR head (merge commit) must carry `Plan-Item: plangraph:<KEY>` since CI runs only on PR heads; add to l2-rules-common.md.
- L2-DONE build-L2-inbox.
- L2-DONE build-L2-projector, build-L2-review-code, build-L2-planner-kit, build-L2-keeper-pg (retire sessions).
- New items (projector follow-ups, from L2 DONE): (1) keeper event type for attempts (schema.py), (2) Rust Card workflow must follow
  projector contract v1 docs/projector.md (fold into RT-P1-WORKER / E2E), (3) scheduler for nightly `python -m plangraph.projector reconcile`.
- L2-LAUNCH build-L2-deepinfra after priv-writers merges. Edit server:RT-P1-PG-DEPLOY criteria: B2 off-host optional (D-1375).
- Incident: GitHub Actions billing refusal (GH-hosted runners); plangraph now on workst runner.

## FOR OPERATOR
- Fix GitHub billing, or confirm workstation-only CI is final. Worker-code writers used paid deepseek via omniroute.
- D-935 defaults (gen-5): KEEPER-PG verdicts recorded as per-family aggregates of 6-7 part seats; fix findings (gpt-oss g6, qwen q1/q4)
  judge-REFUTED in keeper-pg-seats/final-221e9b2/adjudication.md, so the aggregate says pass and references that file. Qwen served model is
  request-side only (qodercli reports 'qfmodel'). REVIEW-CODE writer family unknown ("free-writer" co-author) -> passed as unknown-free-writer.
- Spawner admission floor 4608 MB refuses L3 on coding.vm -> Sonnet subagent fallback (projector: 8 closed tasks).
