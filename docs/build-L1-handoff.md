CARD: /home/s/code/plangraph-worktrees/build-L1/status/build-L1.card.md (current state; read first)
# build-L1 handoff #15 (gen-14 end -> gen-15, 2026-10-18 ~04:20Z, D-1919 budget stop)
Startup unchanged (skill unattended-orchestration + references/main-orchestrator.md; NO crons; ONE bg hb loop; effort medium; caveman).
IN FORCE: D-1919 (15% Claude left, max 5%/day): no new L2s, no Claude writers/seats, free/qwen L3 only; msgs only BLOCKED/MERGED/GO-REQ.

## Gen-14 landed
- Merged: #65 cec80a38 MODEL-LOOP, #66 de5523f6 PG-CI-FORGEJO, #67 c17ea59d MODEL-NAMES, #68 665f68ba, #70 79b7457f,
  #69 c9c11c5a (after NO-GO D-1882 -> fix D-1883), #72 b766859a (hook allow_value + cli no default port). Main = b766859a.
- Keeper DONE: MODEL-LOOP, MODEL-NAMES, PG-CI-FORGEJO (2 verdicts each). hb keys now 3.
- Republish: step plan control 588d6838 accepted as shape (D-1898), no exec GO. P1+P2 closed (#72). Open: P3 T2 manifest
  (D-B scrub lanes), P4 private words list on host, R3 lane-branch list. L2 relaunch WAIT until budget (D-1916). Scope D-1916:
  rust/deploy public scrubbed for real; 8 docs/plan records already moved (private evidence/docs-plan-rethink).
- C1 (judge #66): forgejo status trust filter must be in the forgejo-switch done criteria before any project ci=forgejo:.
- Lessons (not yet sent to skill owner): FAMILYFENCE refuses any --model pin when not_family set (l3-driver);
  opencode/*-free models undeclared in opencode.jsonc -> not spawnable via autoos; glm-5.3 not in registry.
- Evidence recipe: keeper verifies commit+ci at ATTACH time on the exact merge sha: push ci/<sha> branch + gh workflow run
  tests.yml --ref ci/<sha>, attach after green, delete branch. run-record needs job.json/exit.json in server runs dir
  (workstation runs: workst-L1-autoos Taildrop tar -> `tailscale file get`, inspect, cp -n).
- seat_verdict needs --url http://127.0.0.1:8787 now.

## FIRST after clear
1. `cd /home/s/code/plangraph-worktrees/build-L1; for k in $(cat ops/hb-keys.txt); do ops/pg.sh heartbeat $k; done`
2. ONE bg loop: `for i in $(seq 1 16); do sleep 1800; for k in $(cat ops/hb-keys.txt); do ops/pg.sh heartbeat $k; done; done`
   (gen-14 loop restarted 04:02Z, dies with clear.) Keys: UI-BROWSER-TESTS, UI-DEPLOY-AUTO (TEMPORAL-CHECK released D-1933, re-add on re-claim).
3. Control branch: commit + `git push control build-L1` ONLY (local bare repo, D-1751). Never origin.
4. Collect the 6 keeper-model seats (below), then judge.

## IN FLIGHT
- build-L2-ui-u1-stamp: D1 dev-stack-static resumed (GO 20:1xZ, RAM ok) -> MERGE-READY -> seats gemini+mimo (free) -> merge.
- build-L2-rtp1-predeploy-2: #46-#49 (#46/#47 judge).
- UI-DEPLOY-AUTO: waits routing rev 4 apply (release/re-claim/evidence/submit).
- PARKED: republish L2, UI-BROWSER-TESTS, p1-rehearsal.

## Recipes
- Merge: `gh api -X PUT repos/Ch3fUlrich/plangraph/pulls/N/merge -f sha=<head> -f merge_method=merge -f commit_title=... -f commit_message="Plan-Item: <key>"`
  -> `ops/pg.sh evidence K --kind commit+ci --commit <merge sha>` -> `ops/pg.sh submit K` -> verdicts:
  `python3 $sv --key K --seat-dir D --writer-family <writer> --token-dir ~/.config/plangraph --url http://127.0.0.1:8787` (url required after #68) ($sv = git show origin/main:tools/seat_verdict.py > tmp).
- Seat spawn: autoos-agent spawn client=qoder (qwen) or client=agy model=gemini-3.8-flash, card review, cwd = detached worktree at head,
  review_base = base sha, not_family [writer family(+qwen for agy)], no_fallthrough. Seat dir: review.txt (V1..TEST-GAMING) + evidence.json.
- run-record evidence needs an autoos run id (<runs_dir>/<id>/job.json naming the item + exit.json rc 0), not a file path.
- Keeper DB: `ops/pq.sh ...` (see gen-12 handoff in git history).
Peers: routing (uds 7069.sock), judge server-L1-routing, workst-L1-autoos [b76c36], prox-L1-main, plangraph-L1-graphrestructure.
