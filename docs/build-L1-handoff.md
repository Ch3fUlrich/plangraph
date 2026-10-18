CARD: /home/s/code/plangraph-worktrees/build-L1/status/build-L1.card.md (current state; read first)
# build-L1 handoff #16 (gen-15 end -> gen-16, 2026-10-18 ~13:50Z, recycle D-1990)
Startup: skill unattended-orchestration + references/main-orchestrator.md; NO crons; ONE bg hb loop; effort medium; caveman.
IN FORCE: D-1932 ONLY PH-RT-RUNTIME. D-1919 budget: writers qwen (qodercli) / opencode free, seats non-Claude, Claude orchestrates only.
D-1964: L2 up to 3 L3 writers parallel, claim BEFORE PR, PROGRESS line to routing every 2 h, phase % line on every close.
D-1984 (Q19): read item + named docs before asking anyone. D-1985: Jev key only from opencode auth store at call time.

## FIRST after clear
1. `cd /home/s/code/plangraph-worktrees/build-L1; for k in $(cat ops/hb-keys.txt); do ops/pg.sh heartbeat $k; done`
2. ONE bg loop: `for i in $(seq 1 16); do sleep 1800; for k in $(cat ops/hb-keys.txt); do ops/pg.sh heartbeat $k; done; done`
3. Control branch: commit + `git push control build-L1` ONLY (local bare repo, D-1751). Never origin.
4. Tools (job tmp is gone): ~/.local/share/plangraph/tools-l1/{sv.py (seat verdict), q2seat.py <seatdir> <head> (qwen out.json ->
   review.txt+evidence.json), build.py (close-seat prompt builder), pgbin/ (docker pg client wrappers, --memory 1g)}.

## Gen-15 landed
- Merged: #75 #81 records, #82 VERBS, #84 (broke main) -> #86 34806368 fix, #85 4b2f3591 FOLLOWUP, #80 cdd0655d PGDRILL. Main = cdd0655d.
- DONE: DEVSTACK, KEEPER-WORKER-VERBS, WORKER, PROJECTOR, INBOX, REVIEW, VERBS-FOLLOWUP. PH-RT-RUNTIME 4/13 direct deps.
- CI: tests.yml has NO push trigger: after every merge `gh workflow run tests.yml --ref main`, watch, then attach commit+ci.

## IN FLIGHT (act in this order)
1. md-sync (D-1990 blocker, GO D-1992): cause tools/md_sync_plan.py:27-30 hard-codes routing:PH-OPERATOR (rehomed to
   plangraph:PH-OPERATOR by D-1834). F7 at rt-side-2 (alias resolve + --gate). On F7 MERGE-READY: merge, CI, seat PASS, then
   D-1992 steps EXACTLY: (1) dry-run md-sync at the full merge sha vs live keeper, counts only; HOLD + send summary if refusals>0,
   ops>20 or anything under a cancelled parent; (2) worktree pinned at sha (detached) + drop-in
   ~/.config/systemd/user/plangraph-md-sync.service.d/z9-md-sync-<sha8>.conf (Environment=PYTHONPATH=<wt>/src, ExecStart= then
   ExecStart with <wt>/tools paths; same args as unit); daemon-reload, reset-failed, start .timer + .path; (3) read back one run:
   exit 0, refused=0, routerctl alarms gone. Report sha, drop-in path, dry-run counts, read-back line. Rollback rm drop-in + reload.
   NEVER edit the frozen .claude/worktrees/proposal tree.
2. TEMPORAL-CHECK: review_tier=none -> keeper refuses verdicts; router close_on_decision D-1967 asked (seat qwen PASS
   seats/temporal-check/qwen). Drop from hb-keys when done.
3. DEV-PGDRILL-LOCAL (HIGH): #80 merged, commit+ci attached. Lane D2 (rt-side-2, D-1988): dev-only guard + --memory on client
   docker + tests. After D2 merge: evidence commit+ci D2 + decision D-1988, submit, verdicts seats/pgdrill-0df12c0/{mimo,gemini}
   (writer qwen). Run record evidence/pgdrill-codingvm-0df12c0-ok.log (rows=58).
4. #83 QUESTION-TIMER (HIGH, rt-side-2): restart e2e + pytest, re-seat -> GO-REQ judge.
5. FLOW-UNBLOCK lane F (HIGH, rt-side-2, F1-F7). After done: claim RT-QC-ACCEPTS, tell rt-side-2 "QC claimed".
6. predeploy-4 [bridge session_01CpY4...]: LIVE-GUARDS G1/G2 (HIGH -> judge), RT-GATE-WIRING, E2E driver, JEV-SHADOW (test call
   OK 200 jev-1.13-free; flag PLANGRAPH_JEV default off).
7. FLEET-SID-REFRESH: hold mimo2 PASS until routing attaches coding.vm live proof (router-owned).
8. CLASS-COMBOS: workst-L1-autoos owns.

## Recipes
- Merge: `gh api -X PUT repos/Ch3fUlrich/plangraph/pulls/N/merge -f sha=<head40> -f merge_method=merge -f commit_title=...
  -f commit_message="Plan-Item: plangraph:KEY"` (trailer ONLY) -> dispatch CI on main -> `ops/pg.sh evidence K --kind commit+ci
  --commit <merge40>` -> submit -> `python3 ~/.local/share/plangraph/tools-l1/sv.py --key K --seat-dir D --writer-family W
  --token-dir ~/.config/plangraph --url http://127.0.0.1:8787` (seat dir: review.txt TEST-GAMING ": 0 hits" + VERDICT, evidence.json).
- MERGE GATE: pytest + pytest-postgres + scrub (+ rust when touched) green on head; rust-only PRs now trigger pytest (#86).
- qwen seat: `cd empty && timeout 900 qodercli -p --model Qwen3.8-Flash --tools "" --no-session-persistence -o json "$(cat P)"`;
  prompts end with ops/tail.md. mimo: `sh ops/seat.sh mimo/mimo-v2.6-flash P OUT TITLE` (times out >~47 KB).
- Dev stack on coding.vm: DEVSTACK_STATE=<tmp> TEMPORAL_PORT=27233 TEMPORAL_UI_PORT=28233 PG_PORT=25432 KEEPER_PORT=28787,
  python = ~/.local/share/plangraph/venv-pg/bin (psycopg); admin add-principal X --role router + admin import tests/fixtures/import_mini.
- Keeper DB: `ops/pq.sh ...`.
Peers: routing (uds 7069.sock), judge server-L1-routing, build-L2-rt-side-2 [bridge session_01D3w3...], build-L2-rtp1-predeploy-4,
workst-L1-autoos.
