CARD: /home/s/code/plangraph-worktrees/build-L1/status/build-L1.card.md (current state; read first)
# build-L1 handoff #12 (gen-11 end -> gen-12, 2026-10-17 ~07:00Z)
Startup: skill unattended-orchestration. NO crons (D-1377); wait via ONE background command that exits. Effort medium. Msgs only BLOCKED/DONE/
MERGE-READY/GO/L2-LAUNCH/incident. Never AskUserQuestion; questions to routing with options+default. Card <= 40 lines.
Rules: D-1504 model paths. D-1442 judge = server-L1-routing (ref changes on recycle: ListAgents, newest bg one). D-1533 any family.
D-437/D-1554 coding.vm only TOOL-LESS writers/seats; D-1569 docker ok for throwaway test DB only. Dismissed seat fail verdicts are NOT countable (judge).

## Leases (FIRST after clear)
Run: `cd /home/s/code/plangraph-worktrees/build-L1; for k in $(cat ops/hb-keys.txt); do ops/pg.sh heartbeat $k; done`, then ONE bg loop:
`for i in $(seq 1 16); do sleep 1800; for k in $(cat ops/hb-keys.txt); do ops/pg.sh heartbeat $k; done; done` (8 h; restart when it exits).
Keys now: server:RT-P1-TEMPORAL-CHECK, plangraph:MODEL-LOOP, MODEL-NAMES, PG-CI-FORGEJO. Remove a key only AFTER the item is DONE.

## Gen-11 landed (main 2d61e281 + packet ce3c2dee)
- Keeper verbs D-1542 all DONE (#39-#44). LIVE-SYNC-6 DONE (GO D-1569): keeper LIVE = 102e4885, drop-in z6, record seats-archive/live-sync-6-102e4885/LIVE-RECORD.md.
- UI batch DONE: TABICON #50, SMALLFIX-RULE #51 (docs-only PR -> evidence via `gh workflow run tests.yml --ref <lane>`), UI-TABLE #52+#54 (router review
  delegated by operator; D-1644 evidence_kinds=commit+ci), NEEDS-YOU #53. Guard: PATH-EXEMPT #55 (D-1613), SEP-SPLIT #56 (judge ACCEPT).
## IN FLIGHT
- LIVE-SYNC-7 GO REQUESTED from routing (07:0xZ): packet main docs/plan/rethink/live-sync-2d61e281.md §2 Step A (keeper drop-in z7) then Step B (render
  drop-in plangraph-render.service.d/live-sync-z7-2d61e281.conf). Pre-GO dump sha 77f1a56d (backups/pre-go-live-sync-2d61e281-20261017T065658Z).
  On GO: run steps exactly, fresh step-3 dump sha, record seats-archive/live-sync-7-2d61e281/LIVE-RECORD.md (copy LS6 format), DONE to routing with
  gates, MainPID, max seq before/after. Rollback = rm drop-ins (always valid). zsh: use bash -c or quote multiword CLI args (my LS6 verify rc2 slip).
  FINDING: render + md-sync run from .claude/worktrees/proposal -> NEVER retire worktree-proposal at ROLLBACK-HOLD (tell routing again then).
- build-L2-keeper-model (workstation, session_01CbRb89Zc9xrbezss6j8BCi, via workst-L1-autoos): MODEL-LOOP, MODEL-NAMES, PG-CI-FORGEJO (HIGH -> judge).
  Seats there: qwen qodercli + gemini via agy. MODEL-LOOP may need a schema/kind migration -> live-sync impact.
- build-L2-rtp1-predeploy-2 (workstation session_019HCi2CNLnEFtGZQS8XcaDy): W-F7 fix round on PRs #46-#49, then one delta seat round, MERGE-READY
  "N countable + M dismissed". #46/#47 HIGH judge; #48/#49 self-approve (1 earlier pass + 1 fresh delta pass). Merge order 46,47,48,49.
- TEMPORAL-CHECK T3 blocked on operator certs OS-208. MDSYNC 24h check due 2026-10-17 18:45Z -> DONE to routing.
- ROLLBACK-HOLD after 06:42Z: ask routing to retire drop-ins live-sync-1b923e5/8165c22/cd85d13 ONLY (keep proposal tree, see finding).
- Not taken (wait): PG-DEC-ATTRIBUTION (schema), PG-PROJECT-PRIORITY (needs PG-ANSWER-UI), QA-VERBS/PG-ANSWER-UI (auth), MODEL-HOST (live data),
  PUBLIC-READY/SPLIT-PUBLIC (outward), KG-ADAPTER/MODEL-STATS.
## Merge recipe
`gh pr ready N; gh pr merge N --merge --match-head-commit <sha>` -> `ops/pg.sh evidence K --kind commit+ci --commit <head>` -> submit -> verdicts:
`python3 $sv --key K --seat-dir D --writer-family <xiaomi|anthropic> --token-dir ~/.config/plangraph` ($sv = `git show origin/main:tools/seat_verdict.py`
to a tmp file; qwen dirs w/o evidence.json: `python3 ops/qseat.py D/qwen D/qw/out.json` first). Item define: JSON list with "id"+"project",
"under" a plangraph gate (routing gates refused); the live guard scans text, so never put credential-like samples in item text.
Peers: routing [5a7958], judge server-L1-routing (newest), workst-L1-autoos [b76c36], prox-L1-main [8fa900].
