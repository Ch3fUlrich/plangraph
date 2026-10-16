# build-L1 card — rethink P1 (D-1285, batch D-1303) · gen-5 (successor, 2026-10-16 03:2xZ)
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md · design (binding, 14a): .../docs/reports/2026-10-15-rethink-setup-design.md
Branch build-L1 = control branch (never merged). Older log: `git log -p status/build-L1.card.md` (gen-1..4). Handoff: docs/build-L1-handoff.card.md.
Rules: D-1377 no crons; D-1364 msgs only BLOCKED/DONE/MERGE-READY/GO/incident/launch; D-935 defaults, logged below.

## Main = 7ec2bb5 (CI on workst runner since #17 5188310)
DONE in keeper (commit+ci evidence + seat verdicts): RT-P1-CONTRACT, CI-PARALLEL (openai), RT-P1-KEEPER-PG-CODE (openai+alibaba),
RT-P1-REVIEW-CODE (alibaba), RT-P1-PLANNER-KIT-CODE (alibaba), RT-P1-PROJECTOR-CODE (openai+alibaba; PR #16 merged 7ec2bb5 03:3xZ),
RT-P1-INBOX-CODE (PR #18 b5f45c9; router evidence commit 68a9180, kinds->[commit]; openai seat inbox-seats/r2-1cb7acc),
RT-P1-WORKER-CODE (PR #13 merged main 66d87fe, head dd954e7 CI attempt-1 green; early-cancel hole fixed; openai = gpt-oss delta seat
worker-code-seats/openai-delta-dd954e7, alibaba = aggregate worker-code-seats/verdict-alibaba + adjudication-alibaba.md).

## Open
| Item | State | Next |
|---|---|---|
| RT-P1-WORKER-PROJ-V1 (HIGH) | claimed (b05) | brief briefs/l2-worker-proj-v1.md; L2 = reused build-L2-worker-code on workstation (routing 04:1xZ) |
| PROJECTOR-RECONCILE-SCHED | claimed (b05) | brief briefs/l2-projector-reconcile-sched.md; branch pushed @66d87fe; L2 on WORKSTATION via workst-L1-autoos (coding.vm MemAvail 3.1 GB; local worktree reconcile-sched unused) |
| RT-P1-TEMPORAL-SPIKE-TLS (prio 1) | claimed 04:2xZ | gates server:RT-P1-TEMPORAL-CHECK; brief briefs/l2-temporal-tls.md (spike + rust/worker mTLS; router agreed, criteria edit refused while claimed -> record "and rust/worker binary" in DONE note + evidence); L2 routed to workst-L1-autoos |
| CI-RUNS-ON-WORKST | open, blocked on autoos:CI-WORKST-RUNNER | code merged (#17, trailer 813951d, label workst). D-1380: label switch to [self-hosted, fleet-ci] is autoos:CI-RUNS-ON-POOL (fixes-L1, all repos) after runners carry fleet-ci; switching now would strand CI. Claim+evidence once CI-WORKST-RUNNER closes |
| AO-DEEPINFRA-REMOVE | waits priv-writers merge (fixes-L1) | brief docs/plan/rethink/briefs/l2-deepinfra-remove.md |
| server:RT-P1-PG-DEPLOY | open (server-L1-main) | gates KEEPER-PG/TEMPORAL-CHECK/RT-P1 |
Leases: heartbeat claimed items each beat (`ops/pg.sh heartbeat plangraph:<KEY>`); last 03:2xZ.
L2s: projector told to stand down (DONE). Retired: keeper-pg, backup, review-code, planner-kit (sessions still listed idle).

## Tools
- Verdicts: `python3 <(git show origin/main:tools/seat_verdict.py) --key K --seat-dir D --writer-family F --token-dir ~/.config/plangraph`.
- `ops/qseat.py OUT [--adjudication ADJ] part.json...` builds a seat dir (review.txt + evidence.json) from qodercli out.json parts;
  multi-part seats with refuted fix findings pass only with the adjudication file. Evidence kind = `commit+ci` (Plan-Item trailer sha on main).

D-1380 03:34Z: interim local-CI fallback rule added to briefs/l2-rules-common.md (record by sha; no lane waits for GitHub).

## FOR ROUTER (active)
- Done/applied by router 03:4x-04:0xZ: L2-DONE x6 (stopped), INBOX evidence, PG-DEPLOY criteria (D-1375), b05 (ATTEMPT-EVENT refused, dropped).
- Pending: L2-LAUNCH build-L2-temporal-tls (asked); reconcile + worker-proj routed to workst-L1-autoos; L2-LAUNCH build-L2-deepinfra after priv-writers merges.
- Incident: GitHub Actions billing refusal (GH-hosted runners); plangraph on workst runner; D-1380 local fallback.

D-1390: L2-LAUNCH = "sonnet medium"; build-L1 effort medium at next recycle; plangraph CI label -> [self-hosted, fleet-ci] by fixes-L2-cipool (covers CI-RUNS-ON-WORKST label part).
Infra: PG + Temporal move to cloud.vm (D-1386); cloud.vm starting (D-1388).

## FOR OPERATOR
- Fix GitHub billing, or confirm workstation-only CI is final. Worker-code writers used paid deepseek via omniroute.
- D-935 defaults (gen-5): KEEPER-PG verdicts recorded as per-family aggregates of 6-7 part seats; fix findings (gpt-oss g6, qwen q1/q4)
  judge-REFUTED in keeper-pg-seats/final-221e9b2/adjudication.md, so the aggregate says pass and references that file. Qwen served model is
  request-side only (qodercli reports 'qfmodel'). REVIEW-CODE writer family unknown ("free-writer" co-author) -> passed as unknown-free-writer.
- Spawner admission floor 4608 MB refuses L3 on coding.vm -> Sonnet subagent fallback (projector: 8 closed tasks).
