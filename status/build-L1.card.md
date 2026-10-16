# build-L1 card — rethink P1 (D-1285, batch D-1303) · gen-10 · NO cron (D-1377) · resumed post-clear 18:29Z (hb loop bd0zvmjcc)
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md · design (binding, 14a): .../docs/reports/2026-10-15-rethink-setup-design.md
Branch build-L1 = control branch (never merged). Older log: `git log -p status/build-L1.card.md`. Handoff #10: docs/build-L1-handoff.md. After clear: heartbeat TEMPORAL-CHECK lease FIRST.
Rules: D-1504 model = its path only (qwen3.8-flash qodercli; gemini-3.8-flash agy/AI Studio; muse-spark-1.3 opencode; mimo-v2.6 mimo), path in every brief/seat request. D-1442 judge only HIGH (keeper engine/live, schema, auth) -> LOCAL server-L1-routing; standard = self-approve on seats. D-1364 msgs only BLOCKED/DONE/MERGE-READY/GO/incident/launch. D-1390 medium. D-825 live steps need GO. No GitHub billing.

## Main = e70eb566 (#36 planner-kit; #35 cutover record; #34 newest-wins; #33 27c95c3; #32 cd85d13; #31 de11496; #30 e790e34; #29 7f5a35a)
Keeper LIVE = b3d71096 on POSTGRES (cloud.vm .159 schema keeper, role keeper_rt, venv-pg) since 16:43Z D-1506b; sqlite era: GO D-1490 12:57Z. Drop-in live-sync-z4-b3d71096.conf (z4: must sort after live-sync-cd85d13.conf).
Backup backups/live-sync-b3d71096-20261016T125712Z (keeper.db 290dd59f). Counts equal except audit_log +2 (snapshot rows). verify + verify-snapshot ok; snapshot GET ~0.74 s.
Rollback = rm z4 drop-in, daemon-reload, stop, rm wal/shm, restore that backup, start (= cd85d13). Prior: cd85d13 (D-1482, backup ...cd85d13-20261016T105157Z), 8165c22, 1b923e5.
DONE in keeper (gen-1..8): see handoff #8 + git log of this card. Gen-9: none new. Gen-10: keeper-pg-record worktree removed.

## Open
| Item | State | Next |
|---|---|---|
| ROLLBACK-HOLD | worktree-proposal f3bac63 + backups/live-sync-1b923e5-20261016T064214Z; drop-ins 1b923e5/8165c22/cd85d13 | after 2026-10-17 06:42Z: ask routing to retire old drop-ins + worktree-proposal |
| plangraph:RT-P1-KEEPER-PG | DONE (judge ACCEPT, verdicts alibaba+xiaomi-judged, note seats-archive/keeper-pg-cutover-a6eb49f6/DONE-NOTE.md). LIVE ON PG 16:43:16Z (GO D-1506 step1+2, D-1506b step3). Loss boundary seq 6329 hash ac87778f. Frozen sqlite 0400 sha256 6e6e9609 (check: sha256sum -c backups/pg-cutover-b3d71096-20261016T164230Z/keeper.db.frozen.sha256). Drop-in zz-pg-backend.conf, unit backup .pre-pg-20261016T164313Z. IN_REVIEW: evidence decision D-1506 ok + commit+ci e5898f70 (PR #35 merged a6eb49f6, tests dispatch 38934446074 green). FRESH SEATS sent judge server-L1-routing [890344] (build-L1 8af498c5 seats-archive/keeper-pg-cutover-a6eb49f6: qwen/qodercli pass TG no; mimo pass TG unsure rc5). hba answered D-1505 b0f127b8d. DONE sent routing/server-L1-main/orch-setup | rollback = runbook §7 (after-writes path: list seq>6329, reapply); GO step4 delete-or-archive >= 2026-10-18 16:43Z; verdicts per judge -> done; then PLANNER-KIT via L2-LAUNCH |
| plangraph:RT-P1-PLANNER-KIT | DONE main e70eb566 (#36), seat alibaba | - |
| server:RT-P1-TEMPORAL-CHECK | claimed epoch 1 (heartbeat lease). Not deployed (7234/8234 closed). Plan Server docs/operations/temporal-check-live-plan.md @314c06d42. GO D-1513 T1-T6+T9; executor prox-L2-temporal-check 1149b99b (home 13.3G, cloud 19.0G free); T1+T2 PASS (log seats-archive/temporal-check/live-log.md); T3 BLOCKED on T0 certs OS-208 | on GO: prox runs T0-T4; then T5 samples, T6 C0, T9 B1-B5 (worker on workstation), T11 decision row >= +24h |
| routing:RT-P0-BASELINE + RT-P0-MDSYNC | D-1520 prio 1; claim forbidden (token scope) -> asked router delegate; L2-LAUNCH build-L2-rtp0 requested (brief l2-rtp0-baseline-mdsync.md, wt routing-worktrees/build-rtp0 lane/build-rtp0) | MERGE-READY -> routing merges; MDSYNC +24h green check |
| BLOCKED-ELSEWHERE | WORKER+PROJECTOR <- TEMPORAL-CHECK (mine); GOVERNOR <- routing:RT-P0-BASELINE; CLASS-COMBOS <- autoos:RT-P0-OR-DRIFT; INBOX<-PROJECTOR; REVIEW<-WORKER; E2E <- all | watch ready |
| CI-RUNS-ON-WORKST | in_review; label switch = autoos:CI-RUNS-ON-POOL (workst-L1-autoos, D-1455) | re-scoped (plan2 6087fe20): workst runner private repos only; public plangraph = GitHub-hosted; await routing verdict |
| ORCH-SETUP coord | lane/orch-setup-plan @026540cd (await routing; D-1514 + UI-FLEET-TAB, WORKER-USES-PLACEMENT mine; lane J host_samples no collision): no RT-P1 freeze; SU-LEASE-CFG + SU-ENV start AFTER KEEPER-PG cutover (rebased); RT-P3-ASK-PUSH -> plangraph:Q-API-PUSH (HIGH, absorbs API-PUSH; PLAN-MODE deps it); orch-setup-plan @6087fe20 awaits routing | send orch-setup DONE w/ cutover sha |
| AO-DEEPINFRA-REMOVE | waits priv-writers merge (workst-L1-autoos) | brief docs/plan/rethink/briefs/l2-deepinfra-remove.md |
L2 live: none (planner-kit 21fa0888 stopped, worktree removed). Leases: server:RT-P1-TEMPORAL-CHECK (heartbeat: PG_URL=http://127.0.0.1:8787 PG_TOKEN_FILE=~/.config/plangraph/build-L1.token pg heartbeat <key>). L2 live: none (ci-newest-wins stopped; worktree removed 13:0xZ, seat archived l2-cards/ci-newest-wins). pytest-speed L2 idle (done #30).

## Traps
- FREEZE ON (D-1398 L7): NO Omnigraph writes (all graphs) until routing FREEZE OFF; keeper unaffected.
- systemd drop-ins apply in lexical order: name new live-sync drop-ins to sort LAST (check MainPID environ PYTHONPATH after start).
- commit+ci verifier now newest-wins per (workflow, job) live; dispatch suites still: never cancel a dispatch on a head sha.
- zsh: `git push origin $T:refs/...` breaks (:r modifier) -> brace. Bash guard denies unquoted heredoc with $(...).
- Seat dirs need review.txt + evidence.json (ops/qseat.py for qodercli out.json).

## Tools
- Verdicts: `python3 <(git show origin/main:tools/seat_verdict.py) --key K --seat-dir D --writer-family F --token-dir ~/.config/plangraph`.
- Live-sync recipe + counts script: handoff #8 (counts.py copied to ops/ if missing: /home/s/.claude/jobs/2714142d/tmp/counts.py).

## FOR OPERATOR
- D-935 defaults (gen-5) unchanged; spawner floor 4608 MB on coding.vm -> Sonnet subagent fallback.
