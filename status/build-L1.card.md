# build-L1 card — rethink P1 (D-1285, batch D-1303) · gen-7 (successor, 2026-10-16 06:2xZ, effort medium; beat cron daab011d)
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md · design (binding, 14a): .../docs/reports/2026-10-15-rethink-setup-design.md
Branch build-L1 = control branch (never merged). Older log: `git log -p status/build-L1.card.md` (gen-1..4). Handoff: docs/build-L1-handoff.card.md.
Rules: D-1442 judge only HIGH (keeper engine/live, schema, auth), standard = self-approve on seats. Cron beat ALLOWED by operator 2026-10-16 (overrides D-1377 for build-L1; 10-min beat 7-59/10); D-1364 msgs only BLOCKED/DONE/MERGE-READY/GO/incident/launch; D-935 defaults, logged below.

## Main = 828e68e · keeper live 1b923e5 (live-1b923e5 worktree via drop-in; worktree-proposal f3bac63 = 24 h rollback) (CI on workst runner)
DONE in keeper (commit+ci evidence + seat verdicts): RT-P1-CONTRACT, CI-PARALLEL (openai), RT-P1-KEEPER-PG-CODE (openai+alibaba),
RT-P1-REVIEW-CODE (alibaba), RT-P1-PLANNER-KIT-CODE (alibaba), RT-P1-PROJECTOR-CODE (openai+alibaba; PR #16 merged 7ec2bb5 03:3xZ),
RT-P1-INBOX-CODE (PR #18 b5f45c9; router evidence commit 68a9180, kinds->[commit]; openai seat inbox-seats/r2-1cb7acc),
RT-P1-WORKER-CODE (PR #13 merged main 66d87fe, head dd954e7 CI attempt-1 green; early-cancel hole fixed; openai = gpt-oss delta seat
worker-code-seats/openai-delta-dd954e7, alibaba = aggregate worker-code-seats/verdict-alibaba + adjudication-alibaba.md),
KEEPER-LIVE-SYNC (live 1b923e5 GO D-1437; PR #26/#27 828e68e; judge ACCEPT adjudication; risk: triggers/PG/Temporal unexercised), PROJECTOR-LIVE-NAMESPACE-FLAKE (PR #25 ae5b749, head bfa0fd9, seat ns-flake-seats openai; DONE sent), KEEPER-CI-CONCLUSIONS (PR #24 1b923e5, judge server-L1-routing GO, keeper restarted 06:05Z, a1b1da8 ok), TEMPORAL-SPIKE-TLS (PR #22 bae9b82; tls-seats; gRPC round trip = TEMPORAL-CHECK manual), WORKER-PROJ-V1 (PR #20 69a47cd; worker-proj-seats), PROJECTOR-RECONCILE-SCHED (PR #21 cf9fc7b, head 8a3995e; seats reconcile-seats; DONE sent 04:4xZ w/ inbox-alarm-reader follow-up).

## Open
| Item | State | Next |
|---|---|---|
| ROLLBACK-HOLD | worktree-proposal f3bac63 + backups/live-sync-1b923e5-20261016T064214Z | after 2026-10-17 06:42Z: ask routing to retire drop-in naming / worktree-proposal |
| PROJECTOR-ALARMS-READER (p3) | claimed 06:5xZ (lease 08:52Z); brief briefs/l2-projector-alarms-reader.md 25bead7 | L2 build-L2-alarms-reader 8d58f6a0 launched (coding.vm) -> MERGE-READY -> judge |
| CI-PYTEST-SPEED (p2, D-1457) | claimed 07:5xZ (lease 09:56Z); brief briefs/l2-ci-pytest-speed.md a402661 | L2-LAUNCH build-L2-pytest-speed asked (workst) -> MERGE-READY w/ 3 timings -> judge self (standard) |
| CI-RUNS-ON-WORKST | open; autoos:CI-WORKST-RUNNER evidence verified 06:1xZ, in_review | code merged (#17, trailer 813951d, label workst). D-1380: label switch to [self-hosted, fleet-ci] is autoos:CI-RUNS-ON-POOL (fixes-L1, all repos) after runners carry fleet-ci; switching now would strand CI. Claim+evidence once CI-WORKST-RUNNER closes |
| AO-DEEPINFRA-REMOVE | waits priv-writers merge (fixes-L1) | brief docs/plan/rethink/briefs/l2-deepinfra-remove.md |
| server:RT-P1-PG-DEPLOY | open (server-L1-main) | gates KEEPER-PG/TEMPORAL-CHECK/RT-P1 |
Leases: ALARMS-READER (08:52Z, renew by 08:4xZ), CI-PYTEST-SPEED (09:56Z). L2 live: alarms-reader 8d58f6a0 (coding.vm, pushed a4bf956 + seats, no DDL).

## Tools
- Verdicts: `python3 <(git show origin/main:tools/seat_verdict.py) --key K --seat-dir D --writer-family F --token-dir ~/.config/plangraph`.
- `ops/qseat.py OUT [--adjudication ADJ] part.json...` builds a seat dir (review.txt + evidence.json) from qodercli out.json parts;
  multi-part seats with refuted fix findings pass only with the adjudication file. Evidence kind = `commit+ci` (Plan-Item trailer sha on main).
## FOR ROUTER (active)
- Done/applied by router 03:4x-04:0xZ: L2-DONE x6 (stopped), INBOX evidence, PG-DEPLOY criteria (D-1375), b05 (ATTEMPT-EVENT refused, dropped).
- Pending: L2-LAUNCH build-L2-temporal-tls (asked); reconcile + worker-proj routed to workst-L1-autoos; L2-LAUNCH build-L2-deepinfra after priv-writers merges.
D-1380 local-CI fallback (l2-rules-common). D-1390: L2-LAUNCH "sonnet medium"; plangraph CI label -> [self-hosted, fleet-ci] by fixes-L2-cipool (covers CI-RUNS-ON-WORKST label part).

## FOR OPERATOR
- OPERATOR 2026-10-16: no GitHub billing; CI runs only on workstation or ci.vm (self-hosted). Billing question closed.
- D-935 defaults (gen-5): KEEPER-PG verdicts recorded as per-family aggregates of 6-7 part seats; fix findings (gpt-oss g6, qwen q1/q4)
  judge-REFUTED in keeper-pg-seats/final-221e9b2/adjudication.md, so the aggregate says pass and references that file. Qwen served model is
  request-side only (qodercli reports 'qfmodel'). REVIEW-CODE writer family unknown ("free-writer" co-author) -> passed as unknown-free-writer.
- Spawner admission floor 4608 MB refuses L3 on coding.vm -> Sonnet subagent fallback (projector: 8 closed tasks).
