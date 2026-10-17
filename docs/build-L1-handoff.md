CARD: /home/s/code/plangraph-worktrees/build-L1/status/build-L1.card.md (current state; read first)
# build-L1 handoff #13 (gen-12 end -> gen-13, 2026-10-17 ~16:55Z)
Startup: skill unattended-orchestration (references/main-orchestrator.md; depth rule: phase >= 2 tasks -> Sonnet L2 via routing L2-LAUNCH;
L3 via autoos-agent spawn; L1 holds plan graph, merges, live steps only). NO crons; wait via ONE bg command that exits. Effort medium.
Msgs only BLOCKED/DONE/MERGE-READY/GO/L2-LAUNCH/incident. Never AskUserQuestion; questions to routing with options + default. Card <= 40 lines.
Output style D-658 caveman. Model paths D-1504; order D-1704/D-1714 (memory model-order-d1704). D-437/D-1554: coding.vm tool-less only.

## FIRST after clear
1. `cd /home/s/code/plangraph-worktrees/build-L1; for k in $(cat ops/hb-keys.txt); do ops/pg.sh heartbeat $k; done`
2. ONE bg loop (8 h; restart when it exits):
   `for i in $(seq 1 16); do sleep 1800; for k in $(cat ops/hb-keys.txt); do ops/pg.sh heartbeat $k; done; done`
   Keys: TEMPORAL-CHECK, MODEL-LOOP, MODEL-NAMES, PG-CI-FORGEJO, UI-BROWSER-TESTS, UI-DEPLOY-AUTO. Drop a key only when the item leaves `claimed`.
3. Control branch: commit + `git push control build-L1` ONLY (local bare repo, D-1751). Never origin.

## Gen-12 landed
- LIVE-SYNC-7 (D-1667): keeper LIVE 2d61e281, MainPID 2817573, dump 42579d25.
- PR #58 opencode cfg (2168134e), #59 CI GitHub-hosted (f3e2500e, D-1718: public repo -> no self-hosted runners).
- PG-PUBLIC-READY: #60 70e6576f (judge GO after G1), #61 bd223f11 (cloud host nit). in_review; D-1748 closed it router-side.
- UI-VERSION-STAMP DONE #62 d34da322 (xiaomi verdict). UI-DEPLOY-AUTO merged #63 91d0cf08 (judge D-1809, G1b met).
- OPENCODE-CFG-WSL-LEGS DONE. OS-208 certs confirmed done. reviewer-google principal created (D-1811; token ~/.config/plangraph/reviewer-google.token 0600).
- Private mirror Ch3fUlrich/plangraph-work (step 1 of PG-REPUBLISH-FRESH); proof ~/.local/share/plangraph/republish/.
- Decisions this gen: D-1748/D-1751 (public history -> fresh one-commit export after running lanes merge; dev stays public; control data
  local only), D-1754 (judge never posts keeper verdicts; seats do), D-1747 (PG-M3 narrowed), D-1766/1767 (router plan cleanups).

## IN FLIGHT (owner -> what I do on its message)
- build-L2-ui-u1-stamp (workst, bridge session_01EbC9mvYMPZuBWbjk4xrDYm): UI-DEPLOY-AUTO follow-up PR (wf pin path/id + run.head_sha,
  STATE outside OUT, sentinel scans all paths). On MERGE-READY: check diff, 2 seats ok -> merge (standard) -> GO-REQ to routing for the
  live install (render unit from deploy/systemd templates as a live-sync drop-in, D-825 steps, rollback = remove drop-in) -> run-record
  evidence -> submit -> seat_verdict google (~/.local/share/plangraph/seats/pr63/google-agy) + xiaomi (rerun mimo on seats/pr63/PROMPT-A1/A2/B).
- build-L2-ui-u2-browser (workst): UI-BROWSER-TESTS. On MERGE-READY: seats ok -> merge -> evidence/submit/verdict. Then L2-LAUNCH U3+U4
  (lane/ui-plan 2bd22de4 docs/plans/ui/briefs), U5 after both.
- build-L2-republish (workst, bridge session_01VCAryp65QuCgxZJ7G9W2ww): brief docs/plan/rethink/briefs/l2-republish.md + D-1751 amendment
  (full-dev manifest incl rust + all workflows; T6 pre-push scrub hook; T7 gitleaks allowlist value constraint; step plan incl cut timing,
  open-PR move onto new root, delete list, archive plangraph-work). Never let it push public main or delete branches. On step plan -> routing GO.
  After T7 merges: PG-PUBLIC-READY seats gemini(agy client) + mimo, post via seat_verdict -> done -> then claim PG-REPUBLISH-FRESH.
- prox-L1-main: TEMPORAL-CHECK T3 #3 failed (temporal unhealthy after start, logs lost). Wait for its log-capture/diag + new GO; log privately.
- build-L2-keeper-model, build-L2-rtp1-predeploy-2: unchanged; MERGE-READY -> judge (HIGH) / self-approve per card.
- MDSYNC 24h check 18:45Z -> DONE to routing.

## Recipes
- Merge: `gh api -X PUT repos/Ch3fUlrich/plangraph/pulls/N/merge -f sha=<head> -f merge_method=merge -f commit_message="Plan-Item: <key>"`
  -> `ops/pg.sh evidence K --kind commit+ci --commit <head>` -> `ops/pg.sh submit K` -> verdicts:
  `python3 $sv --key K --seat-dir D --writer-family <writer> --token-dir ~/.config/plangraph` ($sv = git show origin/main:tools/seat_verdict.py > tmp).
- Local seat: `ops/seat.sh <model> PROMPT OUTDIR TITLE` (gateway; mimo/mimo-v2.6-flash, openrouter/nvidia/nemotron-3-super-120b-a12b:free);
  prompt = vectors + diff + ops/tail.md last. google seat = autoos-agent spawn client=agy model=gemini-3.8-flash card review, review_base,
  not_family ["qwen"], then build seat dir (review.txt from output.log V1..VERDICT, evidence.json served/finish/verdict/test_gaming).
- Keeper DB read/admin: `ops/pq.sh script.py` or `ops/pq.sh -m plangraph.keeper.cli admin ...` (sources ~/.config/plangraph/pg-keeper.dsn,
  PG_KEEPER_BACKEND=postgres PG_SCHEMA=keeper PG_CONFIG=~/.local/share/plangraph/keeper.json, PYTHONPATH live tree, venv-pg python.
Peers: routing (uds 7069.sock), judge server-L1-routing (newest via ListAgents), workst-L1-autoos (bridge session_01JEZtfmsUgLK745neytZL22),
prox-L1-main (bridge session_01Fht3xuBpjDQiqRewSQe1sm).
