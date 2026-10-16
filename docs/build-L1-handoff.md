CARD: /home/s/code/plangraph-worktrees/build-L1/status/build-L1.card.md (current state; read first)
# build-L1 handoff #9 (gen-9 -> gen-10, 2026-10-16 ~17:3xZ, ctx cap 230k)
Startup: skill unattended-orchestration (+ references/main-orchestrator.md). Beat: CronCreate 30 min `17,47 * * * *` (D-1436; gen-9 cron eab5bcdf dies with this session).
Rules: D-1364 msgs only BLOCKED/DONE/MERGE-READY/GO/incident/launch. D-1390 medium. D-1442 HIGH -> LOCAL judge `server-L1-routing [890344]`. D-1504 model = its path only. Live steps need router GO (D-825).

## Gen-9 landed
- Live keeper cd85d13 -> b3d71096 (D-1490, 12:57Z; drop-in live-sync-z4-b3d71096.conf).
- KEEPER-PG CUTOVER LIVE: keeper on Postgres since 16:43:16Z (GO D-1506 step1+2, D-1506b step3). Boundary seq 6329 hash ac87778f. Drop-in zz-pg-backend.conf (EnvironmentFile ~/.config/plangraph/pg-keeper.dsn = keeper_rt; venv ~/.local/share/plangraph/venv-pg). Admin/migrate: pg-keeper-migrate.dsn (role keeper).
  Frozen sqlite 0400 sha256 6e6e9609; backup ~/.local/share/plangraph/backups/pg-cutover-b3d71096-20261016T164230Z. Step 4 (delete-or-archive) needs router GO >= 2026-10-18 16:43Z.
- Record PR #35 merged main a6eb49f6 (head e5898f70 Plan-Item trailer, tests dispatch 38934446074 green). RT-P1-KEEPER-PG = in_review (evidence decision D-1506 ok + commit+ci ok; a stray D-1506b decision row shows ✗, harmless).
- ci-newest-wins worktree removed. Worktree .claude/worktrees/keeper-pg-record (branch merged) can be removed: `git -C /home/s/code/plangraph worktree remove .claude/worktrees/keeper-pg-record`.

## NEXT (in order)
1. JUDGE wants FRESH seats on the cutover (server-L1-routing msg ~17:2xZ): qwen3.8-flash via qodercli (alibaba) + mimo-v2.6-flash via mimo provider (xiaomi). Prompt must carry RAW outputs.
   Evidence bundle DONE: seats-archive/keeper-pg-cutover-a6eb49f6/evidence/ (execution.md, boundary/copy-dryrun/copy-apply/rowhash/verify-pg/verify-switch/verify-snapshot/pg-head/loss-boundary, frozen-sqlite.txt, keeper-rt-privileges.txt (all DDL refused, DML on 20 tables, first post-boundary write 6330 at 16:43:40Z), go-texts-and-hba.txt, unit-effective.txt; secret scan none). Gather script: /home/s/.claude/jobs/68428f2c/tmp/gather.sh.
   TODO: redact the PG_SEMAPHORE_URL line in unit-effective.txt; build prompt.txt = all evidence files raw + questions: (a) every gate has evidence after the boundary? (b) rollback (sqlite 0400 + backup) restorable? (c) any write between freeze and switch? (d) step 4 held? End with `VERDICT: pass|fix` and `TEST-GAMING: yes|no` lines.
   Qwen: `cd <empty dir>; timeout 900 qodercli -p --model Qwen3.8-Flash --tools "" --no-session-persistence -o json "$(cat prompt.txt)" > out.json`, then `python3 ops/qseat.py <seatdir>/verdict-alibaba out.json`.
   Mimo: `ops/seat.sh mimo/mimo-v2.6-flash prompt.txt <seatdir>/verdict-xiaomi keeper-pg-cutover` (gateway review-call writes review.txt + evidence.json).
   Send judge the seat dirs + record sha a6eb49f6 (e5898f70); on ACCEPT: `python3 <(git show origin/main:tools/seat_verdict.py) --key plangraph:RT-P1-KEEPER-PG --seat-dir D --writer-family anthropic --token-dir ~/.config/plangraph` per seat.
   Open judge question for server-L1-main/prox: which pg_hba rule admits keeper_rt live (D-1500 2c8f3d063 ACCEPT vs deployed b0f127b8; hand rule?). build-L1 cannot read pg_hba_file_rules.
2. After KEEPER-PG done: PLANNER-KIT (deps done) -> L2 via routing "L2-LAUNCH <name> <brief> <cwd>" sonnet medium, --parent build-L1. Then WORKER/PROJECTOR after server:RT-P1-TEMPORAL-CHECK; GOVERNOR waits routing:RT-P0-BASELINE.
3. server-L1-main [1c13b7]: PG-DRILL-POSTCUT (rows>0) told it may run. orch-setup: told lanes F/A may start (rebased >= b3d71096).
4. ROLLBACK-HOLD after 2026-10-17 06:42Z: ask routing to retire drop-ins live-sync-1b923e5/8165c22/cd85d13 + worktree-proposal f3bac63 (z4 + zz-pg-backend stay).
5. CI-RUNS-ON-WORKST: HOLD (router re-scope to private repos, S18).
## Traps
Drop-ins lexical order (name overrides to sort last; check /proc/<MainPID>/environ). Worktree-isolated sessions refuse complex git/gh shell lines: use plain commands or exit the worktree. Docs-only PRs trigger no CI: dispatch tests.yml on the branch. Keeper.db is frozen pre-cutover sqlite, NOT live.
