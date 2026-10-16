CARD: /home/s/code/plangraph-worktrees/build-L1/status/build-L1.card.md (current state; read first)
# build-L1 handoff #8 (gen-8 -> gen-9, 2026-10-16 12:5xZ, ctx cap 230k)
Read first: brief /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md, card above.
Startup: skill unattended-orchestration (+ references/main-orchestrator.md). Beat: CronCreate 30 min `17,47 * * * *` (D-1436, build-L1 only; my 9375f049 dies with this session).
Rules: D-1364 msgs only BLOCKED/DONE/MERGE-READY/GO/incident/launch. D-1390 effort medium. D-1442 HIGH -> judge server-L1-routing (send to the LOCAL one: `server-L1-routing [ref]` from ListAgents, a stale Remote Control twin exists).
Live steps need router GO (D-825). No GitHub billing. Card <= 40 lines.

## Gen-8 landed (all keeper state done, evidence + verdicts)
CI-PYTEST-SPEED #30 e790e34 · RT-P1-KEEPER-PG-PREP #29 7f5a35a (judge ACCEPT; cutover = separate GOs) · CI-PG-CONTRACT-TIMEOUT #31 de11496 + 10/10 runs ·
KEEPER-SNAPSHOT-PG #32 cd85d13 (seats/snapshot-pg-c12e5b1 0b02679) · KEEPER-SNAPSHOT-FOLLOWUPS #33 27c95c3 · KEEPER-CI-NEWEST-WINS #34 b3d71096 (D-1479/D-1489).
Live keeper = cd85d13 (GO D-1482 10:52Z, drop-in live-sync-cd85d13.conf, backup backups/live-sync-cd85d13-20261016T105157Z).

## Next
- WAITING router GO: live cd85d13 -> b3d71096 (batches 27c95c3 per D-1484). Recipe below; report drop-in, backup, counts, verify, snapshot GET ms.
- L2-DONE sent for build-L2-ci-newest-wins 29c2f028: archive its card (worktree plangraph-worktrees/keeper-ci-newest-wins/.sessions) to l2-cards/, then `git -C /home/s/code/plangraph worktree remove` it.
- ROLLBACK-HOLD: after 2026-10-17 06:42Z ask routing to retire old drop-ins (1b923e5/8165c22) + worktree-proposal f3bac63.
- Open gated items (card): CI-RUNS-ON-WORKST, AO-DEEPINFRA-REMOVE (workst-L1-autoos), server:RT-P1-PG-DEPLOY -> PG cutover GOs.
## Live-sync recipe (worked 3x)
detached worktree .claude/worktrees/live-<sha> -> stop md-sync.path/.timer + render.timer -> stop keeper -> require no -wal -> sqlite backup API copy + keeper.json + snapshot(+sig) + SHA256SUMS
+ counts (ops: copy /home/s/.claude/jobs/2714142d/tmp/counts.py) -> drop-in live-sync-<sha>.conf (sorts after) -> daemon-reload -> start -> check MainPID env -> counts equal (+1 system snapshot audit row expected)
-> `PYTHONPATH=<wt>/src PG_CONFIG=~/.local/share/plangraph/keeper.json python3 -m plangraph.keeper.cli admin verify` + `verify-snapshot` -> /inbox, /v1/show, heartbeat smoke -> timers back.
## Traps
commit+ci verifier: before b3d71096 is LIVE, every dispatch suite counts (never cancel a dispatch on a head sha). Item closes at tier's review count (2nd verdict 409 locked).
zsh: `git push origin $T:refs/...` breaks (:r modifier) -> quote/brace. Seat dirs need review.txt + evidence.json (ops/qseat.py for qodercli out.json).
