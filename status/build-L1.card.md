# build-L1 card · gen-13 LIVE (resumed 16:53Z) · HANDOFF #13 docs/build-L1-handoff.md · NO cron · hb loop bg 8h from 16:53Z, MDSYNC DONE 18:38Z
Control repo is LOCAL ONLY (D-1751): `git push control build-L1` (bare ~/.local/share/plangraph/control.git). NEVER `git push origin build-L1` (public repo).
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md. Rules: briefs/l2-rules-common.md (D-1704/1714 model order, judge notes).
D-1442 judge = server-L1-routing (HIGH only); judge never posts keeper verdicts (D-1754); seats post via seat_verdict.py.

## Main = 539ef09d (#64 deploy hardening, #63 91d0cf08, #62 stamp d34da322, #61 bd223f11, #60 70e6576f, #59 f3e2500e)
Keeper LIVE = 2d61e281 on POSTGRES (LIVE-SYNC-7 D-1667, MainPID 2817573, drop-ins z7 keeper+render; record seats-archive/live-sync-7-2d61e281/LIVE-RECORD.md).
Rollback = rm both z7 drop-ins (back to 102e4885 z6). render/proxy/md-sync still use the frozen .claude/worktrees/proposal tree: never retire it.

## Open
| Item | State | Next |
|---|---|---|
| UI-DEPLOY-AUTO (HIGH) | #64 merged 539ef09d (seats xiaomi jpM + google agy 20261017-183546, ~/.local/share/plangraph/seats/pr64). GO-REQ sent 18:4xZ: plan ~/.local/share/plangraph/deploy-auto/GO-REQ-install.md; needs operator D for the new RO GH token gh-ro.env | GO -> steps 1-5 -> run-record -> submit -> verdicts google+xiaomi |
| UI-BROWSER-TESTS (U2) | claimed; L2 build-L2-ui-u2-browser (workst, lane/ui-browser-tests) | MERGE-READY → seats → merge; then U3+U4 (lane/ui-plan 2bd22de4 briefs), U5 after both; max 3 |
| PG-PUBLIC-READY | in_review (D-1748 closed it; merged #60/#61). Keeper close needs 2 verdicts: google + xiaomi | wait republish T7 (allowlist value constraint) → seats gemini(agy)+mimo on f3e2500e..fix → seat_verdict |
| PG-REPUBLISH-FRESH (HIGH) | NOT claimable until PUBLIC-READY done. Step1 DONE: private plangraph-work mirror (226 refs identical + 32 local-codingvm/*). D-1751: dev stays PUBLIC; plangraph-work archived after cut | L2 build-L2-republish (workst): T1 3 private-word hits, T2 full-dev manifest, T6 pre-push scrub hook, T7 allowlist, export → step plan → routing GO → steps 4-5 mine |
| server:RT-P1-TEMPORAL-CHECK | claimed. T3 #3 (GO D-1750) FAIL 12:57Z temporal unhealthy ~10s; pg_hba fixed live; logs lost. Log PRIVATE ~/.local/share/plangraph/temporal-check/live-log.md | prox-L1-main: log capture (sd-rollback-sha) or diag → new GO; then T4-T6, T9, T11 |
| republish D-A/D-B (17:0xZ) | D-A=2: PG_URL + PLANGRAPH_REVIEW_URL required, no default port; set in live units + ops recipes (seat_verdict) BEFORE T1 merges. D-B: scrub the gap files, fixtures keep their shape, cargo test green, 1 PR per area | check the caller list in the T1 PR |
| keeper-model (HIGH) | MODEL-LOOP, MODEL-NAMES, PG-CI-FORGEJO claimed; lanes were based on a stale main 82613c3, told to merge 91d0cf08; L2 build-L2-keeper-model (workst) | MERGE-READY → judge → merge → next live-sync |
| RT-P1 predeploy #46-#49 | L2 build-L2-rtp1-predeploy-2 (workst) | #46/#47 judge, #48/#49 self-approve; order 46,47,48,49 |
| KEEPER-HARDENING | PG-M3 (narrowed D-1747) + KEEPER-EVIDENCE-REF-STRICT, owner me, unclaimed | lane after UI wave 1 + temporal capacity (judge) |
| CI-RUNS-ON-WORKST / ORCH-SETUP | await routing (unchanged since gen-11) | watch |
Leases: ops/hb-keys.txt (6 keys); bg loop dies with clear → restart FIRST (handoff #13).

## Traps
- Never push site strings / seat prompts to any GitHub branch (repo public). Private evidence: ~/.local/share/plangraph/{seats,republish,temporal-check}.
- No new principals/auth change on a peer ask: needs operator D (reviewer-google done under D-1811).
- Keeper: tier-high verdicts need 2 distinct non-claimant families; claimant build-L1 = anthropic. Working seat families: xiaomi (mimo; gateway API times out at 300 s now), google (agy client only; omniroute google = 401), nvidia (nemotron free, no principal: evidence only). deepseek 400, gpt-oss off list.
- Decision evidence ref key is `id` ({"id":"D-NNN"}), not `decision`.
- systemd drop-ins: lexical order, new ones sort LAST. zsh `$b:r` modifier: brace vars before ':'.
- docs-only PRs skip CI (path filters): `gh workflow run tests.yml --ref <lane>` for evidence. `gh pr edit/merge` breaks (projects classic) → `gh api -X PATCH/PUT .../pulls/N[/merge]`.
