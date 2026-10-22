# build-L1 card · gen-16 end · HANDOFF #18 docs/build-L1-handoff.md · NO cron · hb bg loop 25m · 2026-10-22 · D-1932 P1 ONLY
Control repo LOCAL ONLY (D-1751): `git push control build-L1`; NEVER origin. Workstation briefs: PASTE inline (+sha256) to workst-L1-autoos (Taildrop to bz-wg-pdw028 never arrives); origin/build-L1 122963c stale/dead.
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md. Rules: briefs/l2-rules-common.md (D-1704/1714 model order, judge notes). D-2019: NO Go writers/seats (Go window = orchestrators); writers D-1704 legs 6-7 (mimo API, deepseek API 7.75 USD) via omniroute baseURL (workst adding); seats tool-less qwen or agy gemini.
D-1442 judge = server-L1-routing (HIGH only). D-2038 spawner first. D-2046/2047 Writer trailer rule (handoff #18). Spawner cap 10 (D-2043).
## Main = 3026d6e1 (3 operator spawner commits 10-19/20 on 8f7f1c55 #93). Older: git log.
Keeper LIVE = 2d61e281 on POSTGRES (LIVE-SYNC-7 D-1667, MainPID 2817573, drop-ins z7 keeper+render; record seats-archive/live-sync-7-2d61e281/LIVE-RECORD.md).
Rollback = rm both z7 drop-ins (back to 102e4885 z6). render/proxy/md-sync still use the frozen .claude/worktrees/proposal tree: never retire it.
## P1 (D-1932) after AUDIT D-2000..2003 (report routing p1-audit 1b97a282 docs/p1-audit/REPORT.md, PRIVATE): 21 deps, 7/21 done
| Item | Owner | State / next |
|---|---|---|
| DONE | - | DEVSTACK, KEEPER-WORKER-VERBS, WORKER, PROJECTOR, INBOX, REVIEW (+FOLLOWUP, TEMPORAL-CHECK server); QUESTION-TIMER 541e4a6c (deploy conds -> WF-VERSIONING); LIVE-GUARDS 2424d40b (live cond -> EXEC-OWNERSHIP) |
| md-sync | - | LIVE 15:28Z on wt md-sync-8f7f1c55 (drop-in z9, judge GO, 2 seats mimo+gemini); OS-OCGO-LOGIN DONE 15:30Z (router fixed D-rows, D-2016); follow-up: exclude kind=gate from gone |
| OCGO | - | ROUTER-L0 DONE; FLEET-BUS in_review (workst); PROBE + L1-FALLBACK claimed (pt3 supervisor open, workst); FLEET-CUTOVER mine last |
| DEV-PGDRILL | - | DONE #89 d8c7b606 (judge GO, D-1988 closed; xiaomi+google). FLAKES for CI-GATE/DRILLS: test_devstack_sh::test_start_launches_temporal_set_current_and_one_docker_pg; pg_drill t4 (diag only) |
| FLOW-UNBLOCK | L2 gone | #87 1dc1f4ed: restore deleted test + asserts only (D-2047), merge after #90; then RT-QC-ACCEPTS |
| EXEC / BATCH #97 / CI-GATE #90 | L2 gone -> rt-side-3 | brief docs/briefs/l2-rt-side-3.md; #90 strict gate + exempt file FIRST |
| GATE-WIRING | L2 gone -> predeploy-6 | #94 afae587 (HIGH): judged scrub entries, rename, fixture builder; after #90 |
| HARDEN #95 / JEV #96 / WF-VERSIONING | L2 gone -> predeploy-6 | brief docs/briefs/l2-predeploy-6.md |
| PHASE-RUNNER | predeploy-5 | needs GATE-WIRING done -> I claim; Jev shadow call site lives here (D-2002) |
| E2E | frozen 7b1e92a | new criteria: phase runner, >=2 cards, pinned opencode Go/free leg, phase PR + ci-gate; driver = FAILURE-DRILLS harness |
| FAILURE-DRILLS / CARD-CHECKS | dogfood (Q-3) | phase runner builds them after PHASE-RUNNER+HARDEN+OWNERSHIP merge; I plan/judge only |
| RT-P0 / FLEET-SID-REFRESH / CLASS-COMBOS / AO-QC-SPAWNER | router / workst | not mine; CLASS-COMBOS D-1991 |
| PG-PUBLIC-READY | me | in_review until history cut after P1 (D-1966) |
## Open (parked D-1932: republish, UI, u1-stamp, #71)
| Item | State | Next |
|---|---|---|
| UI-DEPLOY-AUTO (HIGH) | LIVE 19:25Z (GO D-1850): z8 drop-in, tree live-render, render.env, token gh-live-follow.env (gh auth, rw scopes; OS-GH-RO-TOKEN). Record ~/.local/share/plangraph/deploy-auto/RUN-RECORD-install-539ef09d.md. submit REFUSED: run-record kind wants autoos run dir | BLOCKED to routing: *set_kind decision D-1850 / waive -> submit -> verdicts google+xiaomi |
| UI-BROWSER-TESTS (U2) | PARKED 2026-10-17: WIP lane/ui-browser-tests @6fd8de39 (pushed, no PR) (41 pass/18 skip/100 xfail), verify + W4 hardening + 2nd seat pending; L2 stopped (RAM <12 GB, budget) | relaunch when RAM ok + P1 done; gemini seat from coding.vm |
| PG-REPUBLISH-FRESH (HIGH) | NOT claimable until PUBLIC-READY done. Step1 DONE: private plangraph-work mirror (226 refs identical + 32 local-codingvm/*). D-1751: dev stays PUBLIC; plangraph-work archived after cut | L2 build-L2-republish (workst): T1 3 private-word hits, T2 full-dev manifest, T6 pre-push scrub hook, T7 allowlist, export → step plan → routing GO → steps 4-5 mine |
| republish GO | D-1898: step plan 588d6838 shape accepted, NO exec GO; L2 relaunch WAIT until Claude budget recovers (D-1916); scope confirmed rust+deploy public, 8 docs/plan private. Before GO request: P1+P2 CLOSED (#72); P3 T2 manifest + P4 words list open, R1 timer pause step, R2 mirror path D-row, R3 lane-branch merged/parked list to routing | wait operator |
Leases: ops/hb-keys.txt (10 keys re-claimed epoch 2 2026-10-22 11:16Z after lapse); loop bweqyutz5; restart FIRST after clear.
## Traps
- Never push site strings / seat prompts to any GitHub branch (repo public). Private evidence: ~/.local/share/plangraph/{seats,republish,temporal-check}.
- Keeper: tier-high verdicts need 2 distinct non-claimant families; claimant build-L1 = anthropic. Working seat families: xiaomi (mimo; gateway API times out at 300 s now), google (agy client only; omniroute google = 401), nvidia (nemotron free, no principal: evidence only). deepseek 400, gpt-oss off list.
- TRAILER TRAP: merge commit_message = trailer paragraph ONLY (Plan-Item last, no note lines after); my #46-#49/#73 merges broke it -> attach PR head commit (on main, CI green).
- Claim BEFORE the PR: keeper refuses commits older than the claim (records-PR recipe #75; lease_until = claim+2h). 'commit' kind is router-only.
- MERGE GATE: every suite (pytest + pytest-postgres + rust) green on the head; path filters skip pytest on rust-only PRs (#84 broke main).
- Decision evidence ref key is `id` ({"id":"D-NNN"}), not `decision`.
- docs-only PRs skip CI; pre-#59 lanes runs-on self-hosted (no runners): test main+head on ci/prN-<sha> via `gh workflow run tests.yml --ref`. `gh pr edit/merge` broken -> `gh api` PATCH/PUT, graphql markPullRequestReadyForReview for drafts.
