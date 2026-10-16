CARD: /home/s/code/plangraph-worktrees/build-L1/status/build-L1.card.md (current state; read first)
# build-L1 handoff #11 (gen-10 end -> gen-11, 2026-10-16 ~21:5xZ)
Startup: skill unattended-orchestration. NO crons (D-1377); wait via ONE background command that exits. Effort medium. Msgs only BLOCKED/DONE/
MERGE-READY/GO/L2-LAUNCH/incident. Never AskUserQuestion; questions to routing with options+default. Card <= 40 lines.
Rules: D-1504 model paths. D-1442 judge = server-L1-routing (ref changes on recycle: ListAgents, pick newest/active; uds sock from its last msg).
D-1533 any model family; Claude one leg. D-437/D-1317: coding.vm only TOOL-LESS writers/seats (rules-common updated). D-1554 commit 'Writer:' trailer.
FREEZE OFF (D-1521): Omnigraph writes only via OMNIGRAPH_BASE_URL=https://omnigraph.ohje.ooguy.com.

## Leases (FIRST after clear)
Background loop btuvjdrrb dies with /clear. Run at once: `for k in $(cat ops/hb-keys.txt); do ops/pg.sh heartbeat $k; done`, then restart one bg loop:
`for i in $(seq 1 16); do sleep 1800; for k in $(cat ops/hb-keys.txt); do ops/pg.sh heartbeat $k; done; done`. Remove keys from ops/hb-keys.txt when done.
NEVER `pkill -f "sleep ..."` (kills own shell) — use TaskStop.

## Gen-10 landed
- RT-P0-BASELINE closed (routing main 42ffcc0, D-1528). RT-P0-MDSYNC: merged; 24h green check due 2026-10-17 18:45Z: status refused=0, md-sync timer+path
  active, `python3 ~/code/routing/bin/routerctl` shows no md-sync line -> send routing DONE (router closes via D-row). Items in routing project: build-L1
  token can't claim (unclaimed by order).
- RT-P1-GOVERNOR DONE main 3fd91bd6 (#37). LIVE-SYNC-5 done (GO D-1539): keeper LIVE = 3fd91bd6, drop-in live-sync-z5-3fd91bd6.conf,
  record seats-archive/live-sync-gov-7c1ef8be/LIVE-RECORD.md.
- D-1542: 46 ex-plangraph-L1-main items now mine (list: snapshot owner=build-L1). Keeper verbs batch L2 build-L2-keeper-verbs (a38d9b22, coding.vm,
  brief briefs/l2-keeper-verbs.md, wt plangraph-worktrees/keeper-verbs, seats plangraph-worktrees/keeper-verbs-seats/<KEY>-<sha>/{qwen,openai}):
  DONE: PG-ROUTER-REOPEN (#39 de1242fc), PG-L1-CREATE-WITH-EDGE (#40 4c460c3e), PG-FOLD-UNKNOWN-TYPE (#41 30e776e2).
  IN FIX: PG-ESTIMATE-SRC-EDIT PR #42 7fa80529 — judge CHANGE: estimate_h=None edit (auto-fill src on cleared h; explicit src with h=None accepted).
  L2 told: fix + 2 tests + 2 delta seats + CI -> MERGE-READY -> re-judge delta. Then PG-LE-DEPS-OPS, PG-LE-GUARD-KEY (tool-less writers).
  Writer provenance note: seats-archive/keeper-verbs/DONE-NOTE.md (append each PR; Co-Authored-By Claude = harness default).
- After all verbs merged: ONE batched LIVE-SYNC packet (like briefs/l2-live-sync-governor.md, new L2 via routing L2-LAUNCH): rehearsal on throwaway PG17,
  counts equal, ROLL FORWARD ONLY after first new event type (item_reopened), FOLD ships first or in batch; GO request with rehearsal + backup sha.
## Merge recipe (HIGH): judge ACCEPT -> `gh pr ready N; gh pr merge N --merge --match-head-commit <sha>` -> `ops/pg.sh evidence K --kind commit+ci
--commit <head>` -> submit -> verdicts: `python3 $sv --key K --seat-dir D --writer-family xiaomi --token-dir ~/.config/plangraph` per seat
($sv = `git show origin/main:tools/seat_verdict.py > <tmp>/sv.py`; qwen dirs need ops/qseat.py OUT qwen.json if only review.json) -> DONE to routing.
## In flight elsewhere
- server:RT-P1-TEMPORAL-CHECK (claimed, lease): T3 blocked on operator certs OS-208 (operator back ~02:30Z). Then keeper verify + snapshot 200 within 5 min
  after prox's T3; T5 samples; T9 probes L2 (briefs/l2-temporal-check-probes.md); T11 decision row. Log seats-archive/temporal-check/live-log.md.
- build-L2-rtp1-predeploy (workstation, session_01SNZjahPFHjDQKjYwnPX8UU via Remote Control; launched by workst-L1-autoos): gap matrix done
  (lane/rt-p1-gaps-predeploy 956aaaf); per-item PRs WORKER->PROJECTOR->INBOX->REVIEW; writers Windows tool-less (run-writer-local.ps1). Decided: review
  family ban only reviewer==writer. seat_verdict ban question to routing (default keep).
- ROLLBACK-HOLD after 2026-10-17 06:42Z: ask routing to retire drop-ins live-sync-1b923e5/8165c22/cd85d13 + worktree-proposal f3bac63.
- KEEPER-PG step 4 (delete-or-archive frozen sqlite) GO request >= 2026-10-18 16:43Z.
Peers: routing (uds 7069.sock), server-L1-routing (judge), prox-L1-main [8fa900], workst-L1-autoos [b76c36].
