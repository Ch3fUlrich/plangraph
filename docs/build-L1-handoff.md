CARD: /home/s/code/plangraph-worktrees/build-L1/status/build-L1.card.md (current state; read first)
# build-L1 handoff #14 (gen-13 end -> gen-14, 2026-10-17 ~20:05Z)
Startup: skill unattended-orchestration (references/main-orchestrator.md; depth rule: phase >= 2 tasks -> L2; L3 via autoos-agent spawn;
L1 holds plan graph, merges, live steps only). NO crons; wait via ONE bg command that exits. Effort medium. Output D-658 caveman.
Msgs only BLOCKED/DONE/MERGE-READY/GO/L2-LAUNCH/incident. Never AskUserQuestion; questions to routing with options + default. Card <= 40 lines.
IN FORCE: D-1851 budget mode (no Claude writers/seats, no new Sonnet L2s, stop idle L2s; operator away until ~03:30Z, no operator questions).
D-1854: plangraph dev on the WORKSTATION dev stack; server only via ONE release GO. Model order D-1704/1714, go first D-1840. D-437 coding.vm tool-less.

## FIRST after clear
1. `cd /home/s/code/plangraph-worktrees/build-L1; for k in $(cat ops/hb-keys.txt); do ops/pg.sh heartbeat $k; done`
2. ONE bg loop: `for i in $(seq 1 16); do sleep 1800; for k in $(cat ops/hb-keys.txt); do ops/pg.sh heartbeat $k; done; done`
   (gen-13 loop started 16:53Z, dies with clear.) Keys: TEMPORAL-CHECK, MODEL-LOOP, MODEL-NAMES, PG-CI-FORGEJO, UI-BROWSER-TESTS, UI-DEPLOY-AUTO.
3. Control branch: commit + `git push control build-L1` ONLY (local bare repo, D-1751). Never origin.
4. Collect the 6 keeper-model seats (below), then judge.

## Gen-13 landed
- PR #64 UI-DEPLOY-AUTO hardening merged 539ef09d (seats xiaomi jpM + google agy 20261017-183546; ~/.local/share/plangraph/seats/pr64).
- UI-DEPLOY-AUTO LIVE 19:25Z (GO D-1850): z8 drop-in plangraph-render.service.d/z8-deploy-auto-539ef09d.conf, tree .claude/worktrees/live-render,
  ~/.config/plangraph/render.env, token file gh-live-follow.env (gh auth token, rw scopes -> OS-GH-RO-TOKEN). Record
  ~/.local/share/plangraph/deploy-auto/RUN-RECORD-install-539ef09d.md. Rollback: rm z8, daemon-reload, start render.
- MDSYNC 24h DONE to routing (18:38Z + 18:45Z recheck green).
- P1 audit (qwen L3): 15 non-live gaps, control docs/plan/rethink/p1-audit-539ef09d.md -> u1-stamp D4 lanes L-A..L-E (L-D merge lock HIGH).
- Plans (control): docs/plan/rethink/workst-devstack-plan.md (D1-D5), briefs/l2-p1-rehearsal-ocgo.md (PARKED: no opencode-go L2 path).
- Rethink rev 4 6247312 (graphrestructure) at routing: NEW RT-P1-DEVSTACK, RT-P1-RELEASE-DEPLOY (+drill dep), E2E scope; retarget TEMPORAL-CHECK
  + UI-DEPLOY-AUTO evidence kinds -> decision: when routing applies: release both claims, apply, re-claim, re-attach evidence
  (commit+ci 7130cf11; decision {"id":"D-1850"}), heartbeat keys, submit UI-DEPLOY-AUTO, verdicts google + xiaomi via seat_verdict.
- Temporal root cause (likely, T3 #3): temporalio/server 1.32 lacks dynamicconfig/docker.yaml; sent prox (fix before release GO). Log private.

## IN FLIGHT (owner -> what I do on its message)
- keeper-model MERGE-READY x3 (HIGH, CI green, base 539ef09d, writers mimo/xiaomi): PR #65 MODEL-LOOP 6966e9b (CI 39064575568), PR #66
  PG-CI-FORGEJO d01ba29 (39064578492), PR #67 MODEL-NAMES b36c442 (39064581546). L2 stopped. Seats spawned by me (worktrees
  plangraph-worktrees/seat-{loop,forgejo,names}): qwen 20261017-195940-pr65-loop-qwen-seat-84eb4b, gemini 20261017-195944-pr65-loop-gemini-seat-d0fc49,
  qwen 20261017-195951-pr66-forgejo-qwen-seat-074f04, gemini 20261017-195956-pr66-forgejo-gemini-seat-fbb0ac, qwen 20261017-200001-pr67-names-qwen-seat-ef164c,
  gemini 20261017-200006-pr67-names-gemini-seat-0ea008 (output in /home/s/code/AutoOS/logs/agents/<id>/output.log, exit.json). Seat dirs ->
  ~/.local/share/plangraph/seats/pr6{5,6,7}/{qwen,google}-* (review.txt V..TEST-GAMING + evidence.json like pr64). Both PASS -> JUDGE-REQ
  server-L1-routing (HIGH) -> merge (recipe) -> evidence/submit -> verdicts. Then remove seat worktrees. No live-sync impact (no schema/event).
- build-L2-ui-u1-stamp (bridge session_01EbC9mvYMPZuBWbjk4xrDYm): D1 lane/dev-stack-static (WIP push, F3 test helper + F4 dynamicconfig wait
  RAM >= 12 GB), then D1-live, D2, D3, D4 (audit lanes), D5 release doc. MERGE-READY -> seats (gemini agy from coding.vm if host lacks) -> merge.
- build-L2-republish (bridge session_01VCAryp65QuCgxZJ7G9W2ww): finish T1/T2/T6/T7 PRs then stop; D-A=2 (no default port; set
  PG_URL/PLANGRAPH_REVIEW_URL in live units + ops recipes BEFORE T1 merges); D-B scrub DEFERRED.
- build-L2-rtp1-predeploy-2: #46-#49 continue (P1); #46/#47 judge, #48/#49 self-approve.
- workst-L1-autoos [b76c36]: freeing workstation RAM (~5 GB < 12 GB floor; incident sent).
- prox-L1-main: T3 #4 HOLD (D-1854); dynamicconfig fix for cloud stack; sd-rollback-sha log-capture lane merge ok.
- routing: UI-DEPLOY-AUTO evidence decision (asked: set_kind decision D-1850 / waive) -> covered by rev 4 item 5.
- PARKED: UI-BROWSER-TESTS (WIP lane/ui-browser-tests 6fd8de39, L2 stopped), p1-rehearsal opencode-go L2 (needs ORCH-RUNS-WITHOUT-CLAUDE).

## Recipes
- Merge: `gh api -X PUT repos/Ch3fUlrich/plangraph/pulls/N/merge -f sha=<head> -f merge_method=merge -f commit_title=... -f commit_message="Plan-Item: <key>"`
  -> `ops/pg.sh evidence K --kind commit+ci --commit <merge sha>` -> `ops/pg.sh submit K` -> verdicts:
  `python3 $sv --key K --seat-dir D --writer-family <writer> --token-dir ~/.config/plangraph` ($sv = git show origin/main:tools/seat_verdict.py > tmp).
- Seat spawn: autoos-agent spawn client=qoder (qwen) or client=agy model=gemini-3.8-flash, card review, cwd = detached worktree at head,
  review_base = base sha, not_family [writer family(+qwen for agy)], no_fallthrough. Seat dir: review.txt (V1..TEST-GAMING) + evidence.json.
- run-record evidence needs an autoos run id (<runs_dir>/<id>/job.json naming the item + exit.json rc 0), not a file path.
- Keeper DB: `ops/pq.sh ...` (see gen-12 handoff in git history).
Peers: routing (uds 7069.sock), judge server-L1-routing, workst-L1-autoos [b76c36], prox-L1-main, plangraph-L1-graphrestructure.
