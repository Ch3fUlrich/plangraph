# build-L1 card · gen-15 · HANDOFF #15 docs/build-L1-handoff.md · NO cron · hb bg loop 30m · 2026-10-18 · D-1932 P1 ONLY
Control repo LOCAL ONLY (D-1751): `git push control build-L1`; NEVER origin. Workstation briefs: PASTE inline (+sha256) to workst-L1-autoos (Taildrop to bz-wg-pdw028 never arrives); origin/build-L1 122963c stale/dead.
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md. Rules: briefs/l2-rules-common.md (D-1704/1714 model order, judge notes).
D-1442 judge = server-L1-routing (HIGH only); judge never posts keeper verdicts (D-1754); seats post via seat_verdict.py.

## Main = 4b2f3591 (#85, #86 34806368 main-red fix, #82 VERBS 1449b01e, #81, #79, #78, #77, #76 DEVSTACK-FU, #75 records, #74). Older: git log.
Keeper LIVE = 2d61e281 on POSTGRES (LIVE-SYNC-7 D-1667, MainPID 2817573, drop-ins z7 keeper+render; record seats-archive/live-sync-7-2d61e281/LIVE-RECORD.md).
Rollback = rm both z7 drop-ins (back to 102e4885 z6). render/proxy/md-sync still use the frozen .claude/worktrees/proposal tree: never retire it.

Parked (D-1932): republish, UI, u1-stamp, #71. Gen-14 history: handoff #15 + git log of this branch.
## GEN-15 (D-1932 ONLY PH-RT-RUNTIME; D-1919 budget; D-1933 TEMPORAL-CHECK -> dev start-dev, cloud parts -> RELEASE-DEPLOY)
| P1 item | State | Next |
|---|---|---|
| DONE today | DEVSTACK, KEEPER-WORKER-VERBS, WORKER, PROJECTOR, INBOX, REVIEW, VERBS-FOLLOWUP (#85 4b2f3591) | phase 4/13 direct deps |
| TEMPORAL-CHECK (server) | in_review, D-1967 attached | router close |
| VERBS-LIVE-GUARDS (HIGH) | claimed; predeploy-4 G1/G2 | MERGE-READY -> judge |
| E2E | predeploy-4 lane 3 driver on real verbs | after FOLLOWUP |
| DEV-PGDRILL-LOCAL (HIGH) | #80 head 0df12c06 seats mimo+gemini PASS; my coding.vm run ok rows=58 (evidence/pgdrill-codingvm-0df12c0-ok.log) | GO-REQ judge -> merge -> evidence + 2 verdicts |
| FLOW-UNBLOCK (HIGH, D-1968) | claimed; rt-side lane F (brief 5dbd1f31) | after #80 |
| FLEET-SID-REFRESH | in_review; seats FIX; routing fixing in routing repo (D-1971) | re-seat on routing's new sha |
| PG-PUBLIC-READY | in_review until history cut (D-1966); options record republish/public-cut-options-20261018.md (A default) | operator after P1 |
| CLASS-COMBOS | workst-L1-autoos | MERGE-READY + probe |
## Open
| Item | State | Next |
|---|---|---|
| UI-DEPLOY-AUTO (HIGH) | LIVE 19:25Z (GO D-1850): z8 drop-in, tree live-render, render.env, token gh-live-follow.env (gh auth, rw scopes; OS-GH-RO-TOKEN). Record ~/.local/share/plangraph/deploy-auto/RUN-RECORD-install-539ef09d.md. submit REFUSED: run-record kind wants autoos run dir | BLOCKED to routing: *set_kind decision D-1850 / waive -> submit -> verdicts google+xiaomi |
| UI-BROWSER-TESTS (U2) | PARKED 2026-10-17: WIP lane/ui-browser-tests @6fd8de39 (pushed, no PR) (41 pass/18 skip/100 xfail), verify + W4 hardening + 2nd seat pending; L2 stopped (RAM <12 GB, budget) | relaunch when RAM ok + P1 done; gemini seat from coding.vm |
| PG-REPUBLISH-FRESH (HIGH) | NOT claimable until PUBLIC-READY done. Step1 DONE: private plangraph-work mirror (226 refs identical + 32 local-codingvm/*). D-1751: dev stays PUBLIC; plangraph-work archived after cut | L2 build-L2-republish (workst): T1 3 private-word hits, T2 full-dev manifest, T6 pre-push scrub hook, T7 allowlist, export → step plan → routing GO → steps 4-5 mine |
| republish GO | D-1898: step plan 588d6838 shape accepted, NO exec GO; L2 relaunch WAIT until Claude budget recovers (D-1916); scope confirmed rust+deploy public, 8 docs/plan private. Before GO request: P1+P2 CLOSED (#72); P3 T2 manifest + P4 words list open, R1 timer pause step, R2 mirror path D-row, R3 lane-branch merged/parked list to routing | wait operator |
Leases: ops/hb-keys.txt (8 keys); bg loop dies with clear → restart FIRST (handoff #13).

## Traps
- Never push site strings / seat prompts to any GitHub branch (repo public). Private evidence: ~/.local/share/plangraph/{seats,republish,temporal-check}.
- Keeper: tier-high verdicts need 2 distinct non-claimant families; claimant build-L1 = anthropic. Working seat families: xiaomi (mimo; gateway API times out at 300 s now), google (agy client only; omniroute google = 401), nvidia (nemotron free, no principal: evidence only). deepseek 400, gpt-oss off list.
- TRAILER TRAP: merge commit_message = trailer paragraph ONLY (Plan-Item last, no note lines after); my #46-#49/#73 merges broke it -> attach PR head commit (on main, CI green).
- Claim BEFORE the PR: keeper refuses commits older than the claim (records-PR recipe #75; lease_until = claim+2h). 'commit' kind is router-only.
- MERGE GATE: every suite (pytest + pytest-postgres + rust) green on the head; path filters skip pytest on rust-only PRs (#84 broke main).
- Decision evidence ref key is `id` ({"id":"D-NNN"}), not `decision`.
- docs-only PRs skip CI; pre-#59 lanes runs-on self-hosted (no runners): test main+head on ci/prN-<sha> via `gh workflow run tests.yml --ref`. `gh pr edit/merge` broken -> `gh api` PATCH/PUT, graphql markPullRequestReadyForReview for drafts.
