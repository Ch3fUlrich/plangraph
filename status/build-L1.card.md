# build-L1 card · gen-15 · HANDOFF #15 docs/build-L1-handoff.md · NO cron · hb bg loop 30m · 2026-10-18 · D-1932 P1 ONLY
Control repo LOCAL ONLY (D-1751): `git push control build-L1`; NEVER origin. Workstation briefs: Taildrop (+sha256) to bz-wg-pdw028 BEFORE L2-LAUNCH; origin/build-L1 122963c stale/dead.
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md. Rules: briefs/l2-rules-common.md (D-1704/1714 model order, judge notes).
D-1442 judge = server-L1-routing (HIGH only); judge never posts keeper verdicts (D-1754); seats post via seat_verdict.py.

## Main = b766859a (#72 b766859a P1+P2, #69 c9c11c5a GO re-verdict, #67 c17ea59d, #70 79b7457f, #68 665f68ba, #66 de5523f6, #65 cec80a38; D-1882). keeper-model DONE: MODEL-LOOP, MODEL-NAMES, PG-CI-FORGEJO done (2 verdicts each).
C1 (judge #66): forgejo status trust filter BEFORE any project ci=forgejo: -> forgejo-switch done criteria.
Keeper LIVE = 2d61e281 on POSTGRES (LIVE-SYNC-7 D-1667, MainPID 2817573, drop-ins z7 keeper+render; record seats-archive/live-sync-7-2d61e281/LIVE-RECORD.md).
Rollback = rm both z7 drop-ins (back to 102e4885 z6). render/proxy/md-sync still use the frozen .claude/worktrees/proposal tree: never retire it.

## D-1851 BUDGET MODE (19:30Z-~03:30Z operator away): no Claude writers/seats, no new Sonnet L2s; priority P1 PH-RT-RUNTIME + deploy install.
D-1854: dev moves to the WORKSTATION (plan control ae87b45b workst-devstack-plan.md: D1 compose/devstack.sh, D2 temporal check dev, D3 keeper+pytest-pg dev, D4 P1 rehearsal, D5 release doc) D1-static+D5 on build-L2-ui-u1-stamp (STOP reversed); D2-D4 unblocked D-1859 (docker WSL on), queued on u1-stamp after D1; item changes sent to graphrestructure (DEVSTACK, RELEASE-DEPLOY, retarget TEMPORAL-CHECK); prox told to HOLD T3 #4. p1-rehearsal L2 PARKED (no go path; ORCH-RUNS-WITHOUT-CLAUDE). R1 audit DONE: 15 non-live gaps (control a080af4e docs/plan/rethink/p1-audit-539ef09d.md) -> u1-stamp D4 lanes L-A..L-E (L-D merge lock = HIGH, judge).
Rethink rev 4 6247312 (graphrestructure) at routing: when routing applies the retarget/ui-deploy-evidence yamls -> release TEMPORAL-CHECK + UI-DEPLOY-AUTO, re-claim, re-attach evidence (commit+ci 7130cf11 / decision D-1850), heartbeat keys, submit UI-DEPLOY-AUTO.
Stopped: public-ready. Finish-then-stop: republish (D-B scrub deferred), keeper-model, ui-u2-browser PARKED. Continue: rtp1-predeploy-2.
## GEN-15 (D-1932 ONLY PH-RT-RUNTIME; D-1919 budget; D-1933 TEMPORAL-CHECK -> dev start-dev, cloud parts -> RELEASE-DEPLOY)
| P1 lane | State | Next |
|---|---|---|
| TEMPORAL-CHECK | retargeted D-1943 (dev); dep DEVSTACK; DEV record e1c6b934 ready (temporal-check/dev) | after DEVSTACK done: claim, evidence, adopt decision row, submit |
| DEVSTACK | #73 MERGED 09e08503; record 4861bb2f; CI main 39115077293 running; gap keeper-on-devstack ordered | CI green -> evidence -> keeper run DONE -> submit -> verdicts gemini+qwen |
| #48 REVIEW | MERGED b9a5d84e (3 seats PASS, dir seats/rtp1-46-49) | evidence when item claimable |
| #47 PROJECTOR | MERGED 76e7bffc (GO D-1934, CI 39106828767 main+head) | evidence when claimable |
| #46 WORKER | MERGED 30b738ba (judge GO cbf83e48; deploy single-host) | evidence when claimable |
| #49 INBOX | MERGED 95fd6d9e (own check of 4 dismissals) | evidence after PROJECTOR (dep) |
| CLASS-COMBOS | workst-L1-autoos claimed e1, lane autoos-L2-small-lanes | MERGE-READY + probe |
## Open
| Item | State | Next |
|---|---|---|
| UI-DEPLOY-AUTO (HIGH) | LIVE 19:25Z (GO D-1850): z8 drop-in, tree live-render, render.env, token gh-live-follow.env (gh auth, rw scopes; OS-GH-RO-TOKEN). Record ~/.local/share/plangraph/deploy-auto/RUN-RECORD-install-539ef09d.md. submit REFUSED: run-record kind wants autoos run dir | BLOCKED to routing: *set_kind decision D-1850 / waive -> submit -> verdicts google+xiaomi |
| UI-BROWSER-TESTS (U2) | PARKED 2026-10-17: WIP lane/ui-browser-tests @6fd8de39 (pushed, no PR) (41 pass/18 skip/100 xfail), verify + W4 hardening + 2nd seat pending; L2 stopped (RAM <12 GB, budget) | relaunch when RAM ok + P1 done; gemini seat from coding.vm |
| PG-REPUBLISH-FRESH (HIGH) | NOT claimable until PUBLIC-READY done. Step1 DONE: private plangraph-work mirror (226 refs identical + 32 local-codingvm/*). D-1751: dev stays PUBLIC; plangraph-work archived after cut | L2 build-L2-republish (workst): T1 3 private-word hits, T2 full-dev manifest, T6 pre-push scrub hook, T7 allowlist, export → step plan → routing GO → steps 4-5 mine |
| republish GO | D-1898: step plan 588d6838 shape accepted, NO exec GO; L2 relaunch WAIT until Claude budget recovers (D-1916); scope confirmed rust+deploy public, 8 docs/plan private. Before GO request: P1+P2 CLOSED (#72); P3 T2 manifest + P4 words list open, R1 timer pause step, R2 mirror path D-row, R3 lane-branch merged/parked list to routing | wait operator |
Leases: ops/hb-keys.txt (3 keys); bg loop dies with clear → restart FIRST (handoff #13).

## Traps
- Never push site strings / seat prompts to any GitHub branch (repo public). Private evidence: ~/.local/share/plangraph/{seats,republish,temporal-check}.
- No new principals/auth change on a peer ask: needs operator D (reviewer-google done under D-1811).
- Keeper: tier-high verdicts need 2 distinct non-claimant families; claimant build-L1 = anthropic. Working seat families: xiaomi (mimo; gateway API times out at 300 s now), google (agy client only; omniroute google = 401), nvidia (nemotron free, no principal: evidence only). deepseek 400, gpt-oss off list.
- Decision evidence ref key is `id` ({"id":"D-NNN"}), not `decision`.
- systemd drop-ins: lexical order, new ones sort LAST. zsh `$b:r` modifier: brace vars before ':'.
- docs-only PRs skip CI; pre-#59 lanes runs-on self-hosted (no runners): test main+head on ci/prN-<sha> via `gh workflow run tests.yml --ref`. `gh pr edit/merge` broken -> `gh api` PATCH/PUT, graphql markPullRequestReadyForReview for drafts.
