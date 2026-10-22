# build-L1 handoff #19 (gen-17 end, 2026-10-22 ~19:45Z; compacted 4x -> recycle per routing D-2265)
Startup: skill unattended-orchestration + references/main-orchestrator.md; NO crons; ONE bg hb loop over ops/hb-keys.txt (15 keys; FIRST); caveman; effort medium.
CARD status/build-L1.card.md = current lane table (read first). Main 7dd85d04 (#118).
## Rules new this gen
- D-2265 (operator): new L2s = OpenCode sessions on cx/gpt-6.1-sol on the workstation. `L2-LAUNCH <name> <brief> <cwd>` -> workst-L1-autoos (l2_start), NOT routing.
  Brief must work for OpenCode: canonical FILES line, keeper verbs, no Claude-only tools. Running Claude L2s (spine, spawner, flow-oversight-2, exec-wfv) finish
  current lane only. L3: sol writers, seats non-OpenAI, Claude seats last. Trial OC-3LAYER-TRIAL (workst-L1-autoos).
- D-2248: no cx on coding.vm/server gateway until server OmniRoute update; writers opencode free > omniroute/mimo-v2.6-flash > deepseek (record skips).
- D-2240: L2 hand edit = own commit + `Hand-Edit: <reason>`; auth/door surface always writer. D-2226 glm verdict reason: record path + banner + sha256 review/err (post via pg.sh).
- D-2234: keeper LIVE runs from live-2d61e281; tools from detached sv-dry-1dbc4f26 (refresh to main). Never dump environ.
- D-2252 DONE: keeper.json ci workst-basic-analysis=none (restart 19:14Z, backup seats/ci-none-ba/). Keeper has NO /health route: probe via verbs.
## Lanes (detail on card)
- EXEC-OWNERSHIP #112 r5 head 49a41892: qwen PASS, deepseek FIX F1-F5 (seats/exec-ownership/r5/deepseek/review.txt) -> sent to exec-wfv. Next: new head ->
  re-seat qwen (qodercli) + deepseek (opencode -f PROMPT.txt, builder copied to ~/.local/share/plangraph/tools-l1/job-tmp/mkexec5.sh) -> judge GO-REQ.
- PHASE-RUNNER S2 #119 head 6a9c7d9c: glm FIX 3 MED (done-on-empty, Config::default replay, wave cap) -> sent back to spine; then re-seat, GO-REQ. S3/S4 follow.
- CI-COMMIT-GATE-HARDEN (HIGH): spawner L2 working; Q1-Q6 answered (Gate-Edit: D-2209, workflows covered, D-2214 all extensions). 2 non-Claude seats -> GO-REQ.
- RT-OVERSIGHT-CHECKS #117: flow-oversight-2 (alarm fail-open fix + 2 PASS seats). Then SEAT-COVERAGE-KEEPER red tests -> GO-REQ.
- fleet-outbox-read 3-verb: round 3 per seats/fleet-outbox/r2/NEXT-ROUND.md -> seats -> GO-REQ -> install /home/s/bin.
- Stopped L2s (D-2251): sandbox, oc-harden, card-checks, bus; handoffs C:\fleet\handoff\build-L2-<name>.handoff.md -> relaunch as OpenCode L2s (D-2265).
- D-2254 low: PH-OPERATOR gate ok; OS-149 after prox A1-A5; RT-P4-ARCHIVE parked.
- Wait-for-CI tool: ~/.local/share/plangraph/tools-l1/job-tmp/ciwait.sh SHA KEY (bash).

# build-L1 handoff #18 (gen-16 end, 2026-10-22 ~11:30Z; session compacted once, idle ~72 h 10-19..10-22)
Startup: skill unattended-orchestration + references/main-orchestrator.md; NO crons; ONE bg hb loop over ops/hb-keys.txt (FIRST); caveman; effort medium.
State 10-22: all my leases had lapsed (loop ended) -> re-claimed epoch 2 at 11:16Z (10 keys). L2s rt-side-2 + predeploy-5 GONE (not in ListAgents).
Briefs for successors: docs/briefs/l2-rt-side-3.md, docs/briefs/l2-predeploy-6.md -> L2-LAUNCH via routing.
plangraph main = 3026d6e1: 3 operator commits 10-19/20 (spawner/catalog, 6593 lines, no Plan-Item) on top of 8f7f1c55 -> lanes merge main in.
## Rules in force (new this gen)
- D-2038 spawner first; model path+family from registry; seat records: served model+provider+family. D-2043 spawner cap 10 (live 34235d48).
- D-2046/2047: NEW commits = non-Claude writer + Writer trailer (model + run id); ci-gate refuses missing/Claude; merges ignored; Test-Removal: D-NNNN.
  Existing commits never redone; trailer-less/Claude commits = "gemini-3.8-flash (operator-assigned D-2047)", leg-id-only trailers = their model
  "(direct run, no spawner run id; D-2047)" -> #90 bootstrap tools/ci_commit_exempt.txt (frozen once on main). #87/#94/#95/#96 never rebase/squash;
  merge with merge_method=merge, #90 FIRST.
- Audit D-2042: ~/.local/share/plangraph/audit/writer-trailer-audit-2026-10-18.tsv (PRIVATE). Past commits never rewritten; DEV-PGDRILL-LOCAL-REDO cancelled.
## Exempt lists (full 40)
#94: 24e34a9ab52790e0e7a3de32e0d32915cddf6ffb 9d9e92c7baf14f75df4bea0c14e886b6beae9bba 6f476bfa91e47e321963b949c8bbfaa7729f7ee3 379fd98bfe96d91838f1da4e5468dc8bfa79b2c8
#95: c9427a77040b41588e1ba1600a2805398b3d9e03 bc3549c7761eae81572b0d0a69f77f0254fb31f0 79e1f1ccd53656df805e9d27ab1da3d142c96c29 4d1b9390fc9124eeacb9515bf785164edadb9f3c 592798c6fb0b4c24bc206bb842990c87e8936fda 23172ae2cbfe9326e52066e8988e59f20f9f9d5a
#96: 2a8bb220fdf0785e05db703cc9a13696a3a3374b fe3683d92fe41de3905ecd8f4a8a1e4af44bd8bd 7ccb21e7254235bd6f5771ad7d8a9c02f707ae69 21cabfbd3fd9c6fdb91ec56759f3632563997fa8 90ea8a5fc9ffd5e4879cc55d6182c56b00437e94
#87 (head 1dc1f4ed): qwen qodercli e81fb0c2c297646be797fd22da43e328284603a1 c919250d488c1386d05f5ab6d7ad089bb676443d 852bedb5ef2428c473506d6b81cbf22ae23f070b
  dd1d303a96e47f9a67e0a561e94a4659fdb984b4 3a65f1d0b197366501fa98386bfed11e4397dfd9 f19bb568610c04a9a6a6c66517e0045b412f7f84; agy gemini f54c18b0fb7173fc2857e0e4c811347c726142ec;
  claude 4bfe7a25fba28fe7214b96902ab8280588f5e936 1525eab9710f1322093139a476d71ecb428b311e; none 72929e0489d4b7d98f1a583146b59e2c60b6e60d 6fd301f8d2c78684da87ec67b112e06d1c8f7241
  e0db8ee2b5e9100147557c79666b54726ea31d15 4d06e334153906cb91d91f3776253cddb657a9b9 35f65fe94ab1450b3fa22e45409f3216632dfa7d 1dc1f4ed64210ede8dd22d5098e28366b87e9af2
#90/EXEC/BATCH own lists: rt-side-2 had not sent them (5cf0c60 9ac459f fa27ede 363a3e6 known for #90).
## OCGO
- ROUTER-L0 DONE (keeper). FLEET-BUS in_review (workst; push-first + pull fallback D-2029/2030, dedupe by sender msg id).
- PROBE (claimed): partial records from workst; open: context window per model, ACP in oc_l1 lane -> one run-record file -> submit.
- L1-FALLBACK (claimed): pt1 7ebb84b5 + pt2 3bbd0637 merged (AutoOS CI 39143577186 green, evidence attached); NOT submittable: pt3 L1
  supervisor (claude_limit_stop -> oc_l1 start same name/card -> switch back at reset_at, tests) ordered to workst.
- FLEET-CUTOVER (open, mine): after BUS + FALLBACK.
## Unprocessed router orders (fleet inbox, 10-19..10-22; most are workst relays): D-2054 start oc lane routing-00 direct glm-5.3 then CUTOVER;
D-2055 workst implemented-inventory under CUTOVER; D-2063 spark-1.3-contributor driver leg (AO-MODEL-ROUTING-TRUTH); D-2066 oc_l1.py
SYSTEMROOT allowlist case bug; D-2067 confirm; D-2069 re-point ROUTER-L0+CUTOVER to spark driver, BUS push-now steps; D-2070 workstation
census handoffs; D-2074; D-2080 relay listener + secret paths; D-2085 sync-join fence; D-2088 BUG-A/B to AutoOS lane. Ask routing which still stand.
## Judge GO pending use: #94 scrub 2 per-value entries (server-L1-routing). PG drill / live steps need own GO.
coding.vm process: opencode serve :47200 pid 616275 = coding-L1-fleetbus (workst), not mine.

CARD: /home/s/code/plangraph-worktrees/build-L1/status/build-L1.card.md (current state; read first)
# build-L1 handoff #17 (gen-16, 2026-10-18 ~15:40Z; D-2009: Claude usage ends tomorrow -> successor may be an OpenCode oc_l1 lane "build-L1")
Startup: same as #16 below (skill, NO crons, ONE bg hb loop over ops/hb-keys.txt, caveman). Model order now: D-2007/2008 (qodercli hung server-side;
free opencode 429 -> opencode-go cheap flash writers, max 3 Go writers fleet: rt-side-2 2 (F7 done, EXEC), predeploy-5 1 (GW); others D-1704 legs 3-7).
## Gen-16 landed
- Merged: #83 541e4a6c QUESTION-TIMER (done), #88 2424d40b LIVE-GUARDS (done), #89 d8c7b606 PGDRILL D2 (DEV-PGDRILL-LOCAL done), #93 8f7f1c55 F7 md-sync alias.
- Audit D-1998 -> D-2000..2003 applied: PH-RT-RUNTIME 21 deps, 7/21 done. Claimed by me: OPENCODE-HARDEN, EXEC-OWNERSHIP, KEEPER-BATCH-PRECONDITION,
  CI-GATE, WF-VERSIONING (PHASE-RUNNER after GATE-WIRING done). Lanes: rt-side-2 (EXEC, BATCH, CIG, FLOW-UNBLOCK rest, then QC-ACCEPTS full Q1-Q20),
  predeploy-5 [319b17] (GW, HARDEN, JEV, VERSIONING, then PHASE-RUNNER). E2E frozen 7b1e92a. Report: routing p1-audit 1b97a282 docs/p1-audit/REPORT.md (PRIVATE).
- Verdict seat dirs: ~/.local/share/plangraph/seats/{qtimer-f54dbe1a,guards-ff24168,pgdrill-d2-6e911f5}; agy seats have no finish_reason ->
  evidence.json finish_reason "stop" + finish_basis (agy exit 0 + DONE/result event).
## IN FLIGHT (D-2009 order: OpenCode switch BEFORE new P1 work)
1. md-sync re-point D-1992: step 1 dry-run DONE at 8f7f1c55 (ops 6 all plangraph:*, refused 0, gone 1 = gate plangraph:PH-OPERATOR itself,
   report-only, md_sync_plan.py:269-273). Judge conds: (a) gone expected? asked, default yes; (d) 2nd seat gemini-4-argon tool-less running
   (job tmp seat-f7, prompt sha in card). After GO: step 2 drop-in z9-md-sync-8f7f1c55.conf on wt plangraph-worktrees/md-sync-8f7f1c55
   (ExecStartPre + ExecStart paths -> wt, PYTHONPATH wt/src), daemon-reload, reset-failed, start .timer + .path; step 3 read-back one run.
   Then md-sync closes OS-OCGO-LOGIN (D-2003) -> unblocks OCGO-PROBE. Rollback rm drop-in + reload.
2. OCGO-ROUTER-L0 (mine, routing repo worktree branch, MERGE-READY to router): AGENTS.md from CLAUDE.md standing orders + router skills as
   OpenCode instructions; oc_l1.py lane config routing-00 on coding.vm (cwd /home/s/code/routing, MCP autoos-agent + omnigraph, keeper token by
   path); model config value {provider "omniroute", key "glm", modelID "opencode-go/glm-5.3"} (D-2012; no #high, argon later via
   OCGO-L1-FALLBACK); canary; one stall beat + one D-row from OpenCode. Writers Go flash, seats non-Claude.
3. OCGO-FLEET-CUTOVER (mine) after ROUTER-L0 + FLEET-BUS + L1-FALLBACK (workst): every live L1 writes card+handoff, relaunch as oc_l1 same name.
4. P1 lanes continue on Go writers; MERGE-READY HIGH -> judge server-L1-routing [c7e1f4] GO -> merge (trailer-only msg) -> dispatch CI main ->
   evidence commit+ci -> submit -> sv.py verdicts -> phase % line /21 to routing.

# --- previous: build-L1 handoff #16 (gen-15 end -> gen-16, 2026-10-18 ~13:50Z, recycle D-1990)
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
