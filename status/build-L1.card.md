# build-L1 card · gen-15 · HANDOFF #15 docs/build-L1-handoff.md · NO cron · hb bg loop 30m · 2026-10-18 · D-1932 P1 ONLY
Control repo LOCAL ONLY (D-1751): `git push control build-L1`; NEVER origin. Workstation briefs: Taildrop (+sha256) to bz-wg-pdw028 BEFORE L2-LAUNCH; origin/build-L1 122963c stale/dead.
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md. Rules: briefs/l2-rules-common.md (D-1704/1714 model order, judge notes).
D-1442 judge = server-L1-routing (HIGH only); judge never posts keeper verdicts (D-1754); seats post via seat_verdict.py.

## Main = 09e08503 (#73 DEVSTACK, #49 95fd6d9e, #46 30b738ba, #47 76e7bffc, #48 b9a5d84e). Older merges: git log.
C1 (judge #66): forgejo status trust filter BEFORE any project ci=forgejo: -> forgejo-switch done criteria.
Keeper LIVE = 2d61e281 on POSTGRES (LIVE-SYNC-7 D-1667, MainPID 2817573, drop-ins z7 keeper+render; record seats-archive/live-sync-7-2d61e281/LIVE-RECORD.md).
Rollback = rm both z7 drop-ins (back to 102e4885 z6). render/proxy/md-sync still use the frozen .claude/worktrees/proposal tree: never retire it.

Parked (D-1932): republish, UI, u1-stamp, #71. Gen-14 history: handoff #15 + git log of this branch.
## GEN-15 (D-1932 ONLY PH-RT-RUNTIME; D-1919 budget; D-1933 TEMPORAL-CHECK -> dev start-dev, cloud parts -> RELEASE-DEPLOY)
| P1 lane | State | Next |
|---|---|---|
| TEMPORAL-CHECK | retargeted D-1943 (dev); dep DEVSTACK; DEV record e1c6b934 ready (temporal-check/dev) | after DEVSTACK done: claim, evidence, adopt decision row, submit |
| DEVSTACK | in_review; evidence c9e39d38 ✓; seats c9e39d38: gemini PASS, mimo FIX (dir seats/ds73-c9e39d38) -> follow-up PR + re-seat | both PASS -> seat_verdict x2 -> done -> claim TEMPORAL-CHECK |
| KEEPER-WORKER-VERBS (D-1957 HIGH) | CLAIMED e1; L2 predeploy-4 (launch via workst, brief 207d461d) top lane | MERGE-READY -> judge -> merge; unblocks E2E |
| D-1955/56 lanes | predeploy-3: D PGDRILL(+devstack host-agnostic), G gitleaks allow (1 hit sandbox_env.rs:312), P PUBLIC-READY seats, E branch triage | PGDRILL real run on coding.vm = me; triage table+ETA -> routing |
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
Leases: ops/hb-keys.txt (4 keys); bg loop dies with clear → restart FIRST (handoff #13).

## Traps
- Never push site strings / seat prompts to any GitHub branch (repo public). Private evidence: ~/.local/share/plangraph/{seats,republish,temporal-check}.
- No new principals/auth change on a peer ask: needs operator D (reviewer-google done under D-1811).
- Keeper: tier-high verdicts need 2 distinct non-claimant families; claimant build-L1 = anthropic. Working seat families: xiaomi (mimo; gateway API times out at 300 s now), google (agy client only; omniroute google = 401), nvidia (nemotron free, no principal: evidence only). deepseek 400, gpt-oss off list.
- TRAILER TRAP: merge commit_message = trailer paragraph ONLY (Plan-Item last, no note lines after); my #46-#49/#73 merges broke it -> attach PR head commit (on main, CI green).
- Decision evidence ref key is `id` ({"id":"D-NNN"}), not `decision`.
- systemd drop-ins: lexical order, new ones sort LAST. zsh `$b:r` modifier: brace vars before ':'.
- docs-only PRs skip CI; pre-#59 lanes runs-on self-hosted (no runners): test main+head on ci/prN-<sha> via `gh workflow run tests.yml --ref`. `gh pr edit/merge` broken -> `gh api` PATCH/PUT, graphql markPullRequestReadyForReview for drafts.
