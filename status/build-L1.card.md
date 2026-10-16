# build-L1 card — rethink P1 (D-1285, batch D-1303) · gen-9 (relaunch routing-00, 2026-10-16 12:5xZ, effort medium; beat cron eab5bcdf 17,47 (D-1436)) · beat 14:01Z ok (keeper active; KEEPER-PG lease renewed; waiting DSN D-1500)
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md · design (binding, 14a): .../docs/reports/2026-10-15-rethink-setup-design.md
Branch build-L1 = control branch (never merged). Older log: `git log -p status/build-L1.card.md`. Handoff: docs/build-L1-handoff.md (#8 e28ba63d).
Rules: D-1442 judge only HIGH (keeper engine/live, schema, auth) -> LOCAL server-L1-routing; standard = self-approve on seats. D-1364 msgs only BLOCKED/DONE/MERGE-READY/GO/incident/launch. D-1390 medium. D-825 live steps need GO. No GitHub billing.

## Main = b3d71096 (#34 newest-wins; #33 27c95c3; #32 cd85d13; #31 de11496; #30 e790e34; #29 7f5a35a)
Keeper LIVE = b3d71096 (GO D-1490, 12:57Z; DONE sent routing). Drop-in live-sync-z4-b3d71096.conf (z4: must sort after live-sync-cd85d13.conf).
Backup backups/live-sync-b3d71096-20261016T125712Z (keeper.db 290dd59f). Counts equal except audit_log +2 (snapshot rows). verify + verify-snapshot ok; snapshot GET ~0.74 s.
Rollback = rm z4 drop-in, daemon-reload, stop, rm wal/shm, restore that backup, start (= cd85d13). Prior: cd85d13 (D-1482, backup ...cd85d13-20261016T105157Z), 8165c22, 1b923e5.
DONE in keeper (gen-1..8): see handoff #8 + git log of this card. Gen-9: none new.

## Open
| Item | State | Next |
|---|---|---|
| ROLLBACK-HOLD | worktree-proposal f3bac63 + backups/live-sync-1b923e5-20261016T064214Z; drop-ins 1b923e5/8165c22/cd85d13 | after 2026-10-17 06:42Z: ask routing to retire old drop-ins + worktree-proposal |
| plangraph:RT-P1-KEEPER-PG | CLAIMED ep1. D-1500: roles keeper(copy)/keeper_rt(runtime), DSNs landed 14:10Z (~/.config/plangraph/pg-keeper{-migrate,}.dsn), loss window = list+reapply. Rehearsal 14:11Z: copy/verify/rowhash PASS (64109 rows, seq 6325); runtime FAIL pg_hba rejects keeper_rt -> BLOCKED sent. Script $JOB/tmp/rehearsal.sh; venv-pg | hba line -> rerun rehearsal -> pause notice -> GO step1+2 -> GO step3 (+ExecStart venv-pg, EnvironmentFile pg-keeper.dsn) -> 48h -> GO step4 -> PG-DRILL-POSTCUT |
| CI-RUNS-ON-WORKST | in_review; label switch = autoos:CI-RUNS-ON-POOL (workst-L1-autoos, D-1455) | HOLD: router re-scoping to private repos (operator S18: public repo = GitHub-hosted) |
| ORCH-SETUP coord | plangraph-L1-orch-setup draft lane/orch-setup-plan 2ca03a4e (await routing): no RT-P1 freeze; SU-LEASE-CFG + SU-ENV start AFTER KEEPER-PG cutover (rebased); new RT-P3-ASK-PUSH/PLAN-MODE/HISTORY owned by me | send orch-setup DONE w/ cutover sha |
| AO-DEEPINFRA-REMOVE | waits priv-writers merge (workst-L1-autoos) | brief docs/plan/rethink/briefs/l2-deepinfra-remove.md |
Leases: plangraph:RT-P1-KEEPER-PG (heartbeat: PG_URL=http://127.0.0.1:8787 PG_TOKEN_FILE=~/.config/plangraph/build-L1.token pg heartbeat <key>). L2 live: none (ci-newest-wins stopped; worktree removed 13:0xZ, seat archived l2-cards/ci-newest-wins). pytest-speed L2 idle (done #30).

## Traps
- systemd drop-ins apply in lexical order: name new live-sync drop-ins to sort LAST (check MainPID environ PYTHONPATH after start).
- commit+ci verifier now newest-wins per (workflow, job) live; dispatch suites still: never cancel a dispatch on a head sha.
- zsh: `git push origin $T:refs/...` breaks (:r modifier) -> brace. Bash guard denies unquoted heredoc with $(...).
- Seat dirs need review.txt + evidence.json (ops/qseat.py for qodercli out.json).

## Tools
- Verdicts: `python3 <(git show origin/main:tools/seat_verdict.py) --key K --seat-dir D --writer-family F --token-dir ~/.config/plangraph`.
- Live-sync recipe + counts script: handoff #8 (counts.py copied to ops/ if missing: /home/s/.claude/jobs/2714142d/tmp/counts.py).

## FOR OPERATOR
- D-935 defaults (gen-5) unchanged; spawner floor 4608 MB on coding.vm -> Sonnet subagent fallback.
