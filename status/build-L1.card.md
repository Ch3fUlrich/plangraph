# build-L1 card · gen-13 LIVE (resumed 16:53Z) · HANDOFF #13 docs/build-L1-handoff.md · NO cron · hb loop bg 8h from 16:53Z, MDSYNC DONE 18:38Z
Control repo is LOCAL ONLY (D-1751): `git push control build-L1` (bare ~/.local/share/plangraph/control.git). NEVER `git push origin build-L1` (public repo).
Brief: /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md. Rules: briefs/l2-rules-common.md (D-1704/1714 model order, judge notes).
D-1442 judge = server-L1-routing (HIGH only); judge never posts keeper verdicts (D-1754); seats post via seat_verdict.py.

## Main = 539ef09d (#64 deploy hardening, #63 91d0cf08, #62 stamp d34da322, #61 bd223f11, #60 70e6576f, #59 f3e2500e)
Keeper LIVE = 2d61e281 on POSTGRES (LIVE-SYNC-7 D-1667, MainPID 2817573, drop-ins z7 keeper+render; record seats-archive/live-sync-7-2d61e281/LIVE-RECORD.md).
Rollback = rm both z7 drop-ins (back to 102e4885 z6). render/proxy/md-sync still use the frozen .claude/worktrees/proposal tree: never retire it.

## D-1851 BUDGET MODE (19:30Z-~03:30Z operator away): no Claude writers/seats, no new Sonnet L2s; priority P1 PH-RT-RUNTIME + deploy install.
D-1854: dev moves to the WORKSTATION (plan control ae87b45b workst-devstack-plan.md: D1 compose/devstack.sh, D2 temporal check dev, D3 keeper+pytest-pg dev, D4 P1 rehearsal, D5 release doc) D1-static+D5 on build-L2-ui-u1-stamp (STOP reversed); D2-D4 unblocked D-1859 (docker WSL on), queued on u1-stamp after D1; item changes sent to graphrestructure (DEVSTACK, RELEASE-DEPLOY, retarget TEMPORAL-CHECK); prox told to HOLD T3 #4. p1-rehearsal L2 PARKED (no go path; ORCH-RUNS-WITHOUT-CLAUDE). R1 audit DONE: 15 non-live gaps (control a080af4e docs/plan/rethink/p1-audit-539ef09d.md) -> u1-stamp D4 lanes L-A..L-E (L-D merge lock = HIGH, judge).
Rethink rev 4 6247312 (graphrestructure) at routing: when routing applies the retarget/ui-deploy-evidence yamls -> release TEMPORAL-CHECK + UI-DEPLOY-AUTO, re-claim, re-attach evidence (commit+ci 7130cf11 / decision D-1850), heartbeat keys, submit UI-DEPLOY-AUTO.
Stopped: public-ready. Finish-then-stop: republish (D-B scrub deferred), keeper-model, ui-u2-browser PARKED. Continue: rtp1-predeploy-2.
## Open
| Item | State | Next |
|---|---|---|
| UI-DEPLOY-AUTO (HIGH) | LIVE 19:25Z (GO D-1850): z8 drop-in, tree live-render, render.env, token gh-live-follow.env (gh auth, rw scopes; OS-GH-RO-TOKEN). Record ~/.local/share/plangraph/deploy-auto/RUN-RECORD-install-539ef09d.md. submit REFUSED: run-record kind wants autoos run dir | BLOCKED to routing: *set_kind decision D-1850 / waive -> submit -> verdicts google+xiaomi |
| UI-BROWSER-TESTS (U2) | PARKED 2026-10-17: WIP lane/ui-browser-tests @6fd8de39 (pushed, no PR) (41 pass/18 skip/100 xfail), verify + W4 hardening + 2nd seat pending; L2 stopped (RAM <12 GB, budget) | relaunch when RAM ok + P1 done; gemini seat from coding.vm |
| PG-PUBLIC-READY | in_review (D-1748 closed it; merged #60/#61). Keeper close needs 2 verdicts: google + xiaomi | wait republish T7 (allowlist value constraint) → seats gemini(agy)+mimo on f3e2500e..fix → seat_verdict |
| PG-REPUBLISH-FRESH (HIGH) | NOT claimable until PUBLIC-READY done. Step1 DONE: private plangraph-work mirror (226 refs identical + 32 local-codingvm/*). D-1751: dev stays PUBLIC; plangraph-work archived after cut | L2 build-L2-republish (workst): T1 3 private-word hits, T2 full-dev manifest, T6 pre-push scrub hook, T7 allowlist, export → step plan → routing GO → steps 4-5 mine |
| server:RT-P1-TEMPORAL-CHECK | claimed; retarget to dev stack (D-1854). Likely T3 #3 cause found: temporal 1.32 image lacks dynamicconfig/docker.yaml (sent prox; fix before release GO). D1 lane/dev-stack-static WIP, F3/F4 wait RAM >= 12 GB | D1 MERGE-READY -> D2 |
| republish D-A/D-B (17:0xZ) | D-A=2: PG_URL + PLANGRAPH_REVIEW_URL required, no default port; set in live units + ops recipes (seat_verdict) BEFORE T1 merges. D-B: scrub the gap files, fixtures keep their shape, cargo test green, 1 PR per area | check the caller list in the T1 PR |
| keeper-model (HIGH) | 3 lanes done by mimo writers: model-loop 6966e9b, pg-ci-forgejo d01ba29, model-names b36c442; told CI-only (workst RAM ~5 GB) + PRs | MERGE-READY -> I run seats qwen(qodercli)+gemini(agy) on coding.vm -> judge -> merge -> live-sync |
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
