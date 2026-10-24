# build-L1 card · gen-25 · 2026-10-23 19:4xZ · NO cron · hb loop = job-tmp hb.sh (3x8min, exits -> restart) over ops/hb-keys.txt
Handoff #25 = docs/build-L1-handoff.md top. Control repo LOCAL ONLY (D-1751): `git push control build-L1`. Rules D-2458 (all findings 1 writer) + D-2472 (P1-P6 speed) + D-2459 (mimo/mimo-v2.6-* only).
Writers: cx/gpt-6.1-sol on workst (inline briefs) > mimo/mimo-v2.6-flash (NEVER omniroute/mimo, operator 10-23). Never Claude writers. D-2442 close idle own sessions.
Seats D-2449/2450: qwen qodercli (job-tmp qseat.sh; efficient fallback ok), Haiku (Agent haiku), mimo, luna (workst); deepseek API OUT (402).
Merge: ~/.local/share/plangraph/tools-l1/job-tmp/mergepath.sh PR HEAD40 KEY TITLE (no submit); Gate-Edit variant: GE=D-n job tmp mergepath-ge.sh.
## Main = 34382a1d (#147). LIVE keeper = live-2d61e281 (never ff)
| Item | Owner | State / next |
|---|---|---|
| RT-P1-EXEC-OWNERSHIP #140 (HIGH) | L1 | sq9 seats qwen PASS + Haiku PASS (seats/exec-ownership/sq9); cb0303ac rust-worker RED (semantic merge: #147 4-arg park_and_alarm) -> Hand-Edit cc563b1e pushed, CI running -> green: GO-REQ |
| UI-BROWSER-TESTS #143 | DONE | #152 merged 135b1685, main CI green, keeper done (qwen r3) |
| GOV-CTX-METRICS #144 | DONE | merged 2aaaf35f, keeper done |
| KEEPER-TRAILER-MULTI #139 (HIGH) | L1 | 8274621e CI green, qwen delta PASS -> GO-REQ sent 01:4xZ (residual unknown 2nd model, *A follow-up) -> mergepath |
| PG-PUBLIC-FIX | DONE | closed D-2430 |
| PG-PUBLIC-READY (HIGH) | L1 | criteria = D-2471 (repo public; no squash/flip; real find -> STOP rotate); D-2463 names waived; r5 Haiku fix (87cfb29d, not counted); D-2467 A: writer lane/pg-public-f3 queued workst (F3 allowlist parity + F2 site grep) -> PR, CI -> seats luna + opencode glm (after SEAT-FREE-OPENCODE) -> submit, GO-REQ. luna r5 c218b921 PASS (workst review sha c788ba6c, prompt 3b993a0c; superseded by f3 head) |
| KEEPER-CLOSE-SKIP-UNVERIFIED #148 | DONE | closed D-2439; tell routing when LIVE-SYNC-9 runs it (PW-ROTATE+MT-226) |
| JEV-ATTEMPT-THREAD + #96 JEV-SHADOW | DONE | #154 merged de54bb22; JEV-SHADOW closed D-2448 |
| BUS-NONCE-FLOOD p4 | L1 | PR #159 7ef86569 CI green; r4 qwen PASS + dsfree doc-only -> Hand-Edit; GO-REQ sent 02:4xZ (HIGH auth); follow-up BUS-SENDER-KEY-BIND proposed |
| EXEC-OBSERVED-STATE | L1 queue | follow-up of #140 (router created) |
| WAIT-GUARD #158 + LESSONS B2 #156 | L1 | #156 GO-REQ sent; #158 l1fix2 writer mimo run 20261024-030128-...57f4c8 (cx 429) -> CI + seats qwen+dsfree |
| ROUTING-STALL | routing | no reply since 21:20Z; open: GO #140 #139 #156 #159, PG-PUBLIC ruling, BUS-SENDER-KEY-BIND leaf |
| RT-P1-PHASE-RUNNER #150 (HIGH) | build-L2-phase-runner (parking) | C1-C3 local 1aa74d0 (contains #139 merge f6b4f6c, no push); STOPPED, handoff C:\fleet\handoff\build-L2-phase-runner.card.md (workst); relaunch with "#139+#140 ON MAIN <sha>" + its handoff |
| CARD-CHECKS #147 / WF-VERSIONING | build-L2-rt-cards | #147 MERGED 34382a1d (dispatch flake rerun -> evidence); lane 2b GO D-2478 (presence-based cols, merge after #140) running; then WF-VERSIONING |
| RT-OVERSIGHT-KEEPER-SCOPE #138 (HIGH) | build-L2-flow-oversight-3 | IDLE head 448e80a0 (5551bc done); trigger = #140 on main -> I send "#140 ON MAIN" -> Mig8 merge, CI, luna+Haiku |
| RT-P1-BUS-CONTRACT | DONE | #153 merged 432c5377, keeper done (mimo + deepseek r2) |
| KEEPER-OPCLAIM-CLOSE #127 | DONE | LIVE-SYNC-9 after SPAWNER-ITEM-KEY + mint smoke (HIGH GO) |
| KEEPER-LEGACY-HUMAN-FAMILY | L1 queue | D-2348, bundle w/ LIVE-SYNC-9 |
| PG-BOOTSTRAP-FLEET-ENV (HIGH) | L1 queue | blocked autoos:AO-OCL1-PW-FILE |
| PG-OMNIROUTE-CONFIG-HOME (p6) | L1 queue | D-2359, L2 after PH-RT-RUNTIME |
| #100 SANDBOX | sweep-runtime | Gate-Edit squash pending |
| OCGO-PROBE / OCGO-L1-FALLBACK | released D-2409 | trigger OS-OCL1-ENV |
- D-2418: plangraph CI stays GitHub-hosted (public repo, D-1718); no runs-on change; BUS-RUST windows build = GitHub-hosted windows.
## Traps
- D-2466 DONE: SEAT-FREE-OPENCODE on AutoOS main 5ce05dd9; coding.vm CLAUDE.md mimo block removed (backup job tmp). B1 L2-LAUNCH build-L2-lessons-b1 sent workst ~03:1xZ.
- Keeper refuses reviewer-anthropic verdicts on build-L1 claims (same_family): Haiku seats cannot be recorded; ruling asked 12:4xZ.
- Never eval a range of mergepath.sh. Verdict needs in_review; mergepath does not submit.
- ONE final trailer block per commit; Hand-Edit <=10 lines; workflow edit needs Gate-Edit D-n.
- zsh: use bash -c for chains; never dump environ; never push site strings/seat prompts to GitHub; never touch .claude/worktrees/proposal.
- D-2315 no age-only deletes; D-2378 kill recorded PIDs only (never taskkill /IM).
