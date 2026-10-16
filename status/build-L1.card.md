# build-L1 card — rethink P1 (D-1285, batch D-1303) · gen-9 (relaunch routing-00, 2026-10-16 12:5xZ, effort medium; beat cron eab5bcdf 17,47 (D-1436)) · 16:43Z KEEPER ON PG
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md · design (binding, 14a): .../docs/reports/2026-10-15-rethink-setup-design.md
Branch build-L1 = control branch (never merged). Older log: `git log -p status/build-L1.card.md`. Handoff: docs/build-L1-handoff.md (#8 e28ba63d).
Rules: D-1504 model = its path only (qwen3.8-flash qodercli; gemini-3.8-flash agy/AI Studio; muse-spark-1.3 opencode; mimo-v2.6 mimo), path in every brief/seat request. D-1442 judge only HIGH (keeper engine/live, schema, auth) -> LOCAL server-L1-routing; standard = self-approve on seats. D-1364 msgs only BLOCKED/DONE/MERGE-READY/GO/incident/launch. D-1390 medium. D-825 live steps need GO. No GitHub billing.

## Main = b3d71096 (#34 newest-wins; #33 27c95c3; #32 cd85d13; #31 de11496; #30 e790e34; #29 7f5a35a)
Keeper LIVE = b3d71096 on POSTGRES (cloud.vm .159 schema keeper, role keeper_rt, venv-pg) since 16:43Z D-1506b; sqlite era: GO D-1490 12:57Z. Drop-in live-sync-z4-b3d71096.conf (z4: must sort after live-sync-cd85d13.conf).
Backup backups/live-sync-b3d71096-20261016T125712Z (keeper.db 290dd59f). Counts equal except audit_log +2 (snapshot rows). verify + verify-snapshot ok; snapshot GET ~0.74 s.
Rollback = rm z4 drop-in, daemon-reload, stop, rm wal/shm, restore that backup, start (= cd85d13). Prior: cd85d13 (D-1482, backup ...cd85d13-20261016T105157Z), 8165c22, 1b923e5.
DONE in keeper (gen-1..8): see handoff #8 + git log of this card. Gen-9: none new.

## Open
| Item | State | Next |
|---|---|---|
| ROLLBACK-HOLD | worktree-proposal f3bac63 + backups/live-sync-1b923e5-20261016T064214Z; drop-ins 1b923e5/8165c22/cd85d13 | after 2026-10-17 06:42Z: ask routing to retire old drop-ins + worktree-proposal |
| plangraph:RT-P1-KEEPER-PG | LIVE ON PG 16:43:16Z (GO D-1506 step1+2, D-1506b step3). Loss boundary seq 6329 hash ac87778f. Frozen sqlite 0400 sha256 6e6e9609 (check: sha256sum -c backups/pg-cutover-b3d71096-20261016T164230Z/keeper.db.frozen.sha256). Drop-in zz-pg-backend.conf, unit backup .pre-pg-20261016T164313Z. Evidence decision D-1506b attached. DONE sent routing/server-L1-main/orch-setup | rollback = runbook §7 (after-writes path: list seq>6329, reapply); GO step4 delete-or-archive >= 2026-10-18 16:43Z; then submit (seats: keeper-pg-seats final-221e9b2) |
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
