# build-L1 card — rethink P1 (D-1285, batch D-1303) · gen-8 (D-1473, 2026-10-16 09:1xZ, effort medium; beat cron 7cf1cc93 13,43)
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md · design (binding, 14a): .../docs/reports/2026-10-15-rethink-setup-design.md
Branch build-L1 = control branch (never merged). Older log: `git log -p status/build-L1.card.md` (gen-1..4). Handoff: docs/build-L1-handoff.card.md.
Rules: D-1442 judge only HIGH (keeper engine/live, schema, auth), standard = self-approve on seats. Cron beat ALLOWED by operator 2026-10-16 (overrides D-1377 for build-L1; 10-min beat 7-59/10); D-1364 msgs only BLOCKED/DONE/MERGE-READY/GO/incident/launch; D-935 defaults, logged below.

## Main = cd85d13 (#32 snapshot-pg 10:5xZ; #29 7f5a35a; #31 de11496; #30 e790e34 09:53Z; prev 8165c22) · keeper live 8165c22 (GO D-1463 08:21Z, drop-in live-sync-8165c22.conf, backup backups/live-sync-8165c22-20261016T082054Z sha f82b3563; prev 1b923e5; worktree-proposal f3bac63 = 24 h rollback) (CI on workst runner)
DONE in keeper (commit+ci evidence + seat verdicts): KEEPER-SNAPSHOT-PG (#32 cd85d13 head c12e5b1, judge ACCEPT cond. Qwen pass; seats-archive/snapshot-pg-c12e5b1; GO req live sync -> cd85d13), CI-PYTEST-SPEED (#30 e790e34), RT-P1-KEEPER-PG-PREP (#29 7f5a35a, judge ACCEPT), RT-P1-CONTRACT, CI-PARALLEL (openai), RT-P1-KEEPER-PG-CODE (openai+alibaba),
RT-P1-REVIEW-CODE (alibaba), RT-P1-PLANNER-KIT-CODE (alibaba), RT-P1-PROJECTOR-CODE (openai+alibaba; PR #16 merged 7ec2bb5 03:3xZ),
RT-P1-INBOX-CODE (PR #18 b5f45c9; router evidence commit 68a9180, kinds->[commit]; openai seat inbox-seats/r2-1cb7acc),
RT-P1-WORKER-CODE (PR #13 merged main 66d87fe, head dd954e7 CI attempt-1 green; early-cancel hole fixed; openai = gpt-oss delta seat
worker-code-seats/openai-delta-dd954e7, alibaba = aggregate worker-code-seats/verdict-alibaba + adjudication-alibaba.md),
PROJECTOR-ALARMS-READER (PR #28 8165c22, head 77dadcb, judge ACCEPT HIGH, seats alarms-reader-seats; DONE sent), KEEPER-LIVE-SYNC (live 1b923e5 GO D-1437; PR #26/#27 828e68e; judge ACCEPT adjudication; risk: triggers/PG/Temporal unexercised), PROJECTOR-LIVE-NAMESPACE-FLAKE (PR #25 ae5b749, head bfa0fd9, seat ns-flake-seats openai; DONE sent), KEEPER-CI-CONCLUSIONS (PR #24 1b923e5, judge server-L1-routing GO, keeper restarted 06:05Z, a1b1da8 ok), TEMPORAL-SPIKE-TLS (PR #22 bae9b82; tls-seats; gRPC round trip = TEMPORAL-CHECK manual), WORKER-PROJ-V1 (PR #20 69a47cd; worker-proj-seats), PROJECTOR-RECONCILE-SCHED (PR #21 cf9fc7b, head 8a3995e; seats reconcile-seats; DONE sent 04:4xZ w/ inbox-alarm-reader follow-up).

## Open
| Item | State | Next |
|---|---|---|
| ROLLBACK-HOLD | worktree-proposal f3bac63 + backups/live-sync-1b923e5-20261016T064214Z | after 2026-10-17 06:42Z: ask routing to retire drop-in naming / worktree-proposal |
| KEEPER-CI-NEWEST-WINS (p2 HIGH, D-1479) | claimed epoch 1 (lease 12:25Z); brief e82499e; routed to workst-L1-autoos (workst launch); no session id by 12:00Z -> tell routing, coding.vm | MERGE-READY -> judge -> merge -> D-915 restart |
| CI-PG-CONTRACT-TIMEOUT (p2) | instr #31 de11496; root fix #32 landed | proposed: build-L2-snapshot-pg runs 10 green pg runs under load -> evidence -> verdicts |
| CI-RUNS-ON-WORKST | open; autoos:CI-WORKST-RUNNER evidence verified 06:1xZ, in_review | code merged (#17, trailer 813951d, label workst). D-1380: label switch to [self-hosted, fleet-ci] is autoos:CI-RUNS-ON-POOL (workst-L1-autoos since D-1455) after runners carry fleet-ci; switching now would strand CI. Claim+evidence once CI-WORKST-RUNNER closes |
| AO-DEEPINFRA-REMOVE | waits priv-writers merge (now workst-L1-autoos, D-1455) | brief docs/plan/rethink/briefs/l2-deepinfra-remove.md |
| server:RT-P1-PG-DEPLOY | open (server-L1-main) | gates KEEPER-PG/TEMPORAL-CHECK/RT-P1 |
Leases: CI-PG-CONTRACT-TIMEOUT + SNAPSHOT-PG (renewed 10:15Z). Trap: commit+ci verifier = per check suite; each dispatch own suite -> never cancel a dispatch on head sha. L2 live: pytest-speed (workst).

## Tools
- Verdicts: `python3 <(git show origin/main:tools/seat_verdict.py) --key K --seat-dir D --writer-family F --token-dir ~/.config/plangraph`.
- `ops/qseat.py OUT [--adjudication ADJ] part.json...` builds a seat dir (review.txt + evidence.json) from qodercli out.json parts;
  multi-part seats with refuted fix findings pass only with the adjudication file. Evidence kind = `commit+ci` (Plan-Item trailer sha on main).
## FOR ROUTER (active)
- Done/applied by router 03:4x-04:0xZ: L2-DONE x6 (stopped), INBOX evidence, PG-DEPLOY criteria (D-1375), b05 (ATTEMPT-EVENT refused, dropped).
D-1380 local-CI fallback (l2-rules-common). D-1390: L2-LAUNCH "sonnet medium"; plangraph CI label -> [self-hosted, fleet-ci] by fixes-L2-cipool (covers CI-RUNS-ON-WORKST label part).

## FOR OPERATOR
- OPERATOR 2026-10-16: no GitHub billing; CI runs only on workstation or ci.vm (self-hosted). Billing question closed.
- D-935 defaults (gen-5): KEEPER-PG verdicts recorded as per-family aggregates of 6-7 part seats; fix findings (gpt-oss g6, qwen q1/q4)
  judge-REFUTED in keeper-pg-seats/final-221e9b2/adjudication.md, so the aggregate says pass and references that file. Qwen served model is
  request-side only (qodercli reports 'qfmodel'). REVIEW-CODE writer family unknown ("free-writer" co-author) -> passed as unknown-free-writer.
- Spawner admission floor 4608 MB refuses L3 on coding.vm -> Sonnet subagent fallback (projector: 8 closed tasks).
