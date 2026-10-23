# build-L1 card · gen-21 · 2026-10-23 ~11:25Z · NO cron · hb loop br1ugphrz (dies w/ gen-21; restart FIRST) over ops/hb-keys.txt
Handoff #22 = docs/build-L1-handoff.md top (order list). Control repo LOCAL ONLY (D-1751): `git push control build-L1`.
Writers: omniroute/mimo-v2.6-flash via autoos-agent spawn (cx/gpt-6.1-sol not served coding.vm, skip). Never Claude writers.
Seats D-2399: deepseek (`sh ops/seat.sh deepseek/deepseek-flash`), Haiku (Agent model haiku), mimo, luna, qwen (429 to 10-24 15:30Z). No opencode free/OR free/agy.
Merge: ~/.local/share/plangraph/tools-l1/job-tmp/mergepath.sh PR HEAD40 KEY TITLE (no submit); Gate-Edit variant: GE=D-n job tmp mergepath-ge.sh.
## Main = 92f041d3 (#143). LIVE keeper = live-2d61e281 (never ff)
| Item | Owner | State / next |
|---|---|---|
| RT-P1-EXEC-OWNERSHIP #140 (HIGH) | L1 | D-2410 A: fix writer 3c26b3 mimo (seats/exec-ownership/sq5/FIX-BRIEF.md) -> push sq2 -> CI -> seats deepseek+Haiku -> re-GO-REQ |
| UI-BROWSER-TESTS #143 | L1 | merged 92f041d3, claimed (not submitted). D-2406 A: fix writer cd8772 (seats/pr143/FIX1-BRIEF.md, wt ui-fix1) -> re-seat 2 -> merge -> submit -> verdicts |
| GOV-CTX-METRICS #144 | L1 | both seats fix (dedup, persist) -> fix writer 1d6c24 -> CI -> re-seat |
| KEEPER-TRAILER-MULTI #139 (HIGH) | L1 | c61f80 provider-blocked -> ca364b (deepseek, D-2409) -> CI -> Haiku+luna -> GO-REQ |
| PG-PUBLIC-FIX (HIGH p8) | L1 | cont writer 1a3af8 (wt pg-public-fix) -> PR, gitleaks 0, CI, 2 seats, merge; then re-seat PG-PUBLIC-READY |
| #96 JEV-SHADOW (HIGH) | L1 | merged 0f588e1d, in_review; D-2396 -> JEV-ATTEMPT-THREAD p7 (spawn next); close w/ 1 seat on combined diff |
| KEEPER-CLOSE-SKIP-UNVERIFIED (HIGH p7) | L1 queue | D-2411; define refused unreachable_goal -> routing asked (item.json seats/close-skip-unverified); writer after slot frees |
| JEV-ATTEMPT-THREAD p7 | L1 queue | brief seats/jev-attempt-thread/WRITER-BRIEF.md; spawn+claim when slot frees |
| EXEC-OBSERVED-STATE | L1 queue | follow-up of #140 (router created) |
| RT-P1-PHASE-RUNNER (HIGH) | build-L2-phase-runner (workst) | writer id chased 11:2xZ (D-2407); S3d -> MERGE-READY; merge after #140 |
| RT95 / CARD-CHECKS / WF-VERSIONING | build-L2-rt-cards (workst) | brief briefs/rt-cards-wfv-rt95.md |
| RT-OVERSIGHT-KEEPER-SCOPE #138 (HIGH) | build-L2-flow-oversight-3 | expect MERGE-READY/GO-REQ |
| RT-P1-BUS-CONTRACT | build-L2-bus | expect MERGE-READY (watch qwen 429) |
| KEEPER-OPCLAIM-CLOSE #127 | DONE | LIVE-SYNC-9 after SPAWNER-ITEM-KEY + mint smoke (HIGH GO) |
| KEEPER-LEGACY-HUMAN-FAMILY | L1 queue | D-2348, bundle w/ LIVE-SYNC-9 |
| PG-BOOTSTRAP-FLEET-ENV (HIGH) | L1 queue | blocked autoos:AO-OCL1-PW-FILE |
| PG-OMNIROUTE-CONFIG-HOME (p6) | L1 queue | D-2359, L2 after PH-RT-RUNTIME |
| #100 SANDBOX | sweep-runtime | Gate-Edit squash pending |
| OCGO-PROBE / OCGO-L1-FALLBACK | released D-2409 | trigger OS-OCL1-ENV |
## Traps
- Never eval a range of mergepath.sh. Verdict needs in_review; mergepath does not submit.
- ONE final trailer block per commit; Hand-Edit <=10 lines; workflow edit needs Gate-Edit D-n.
- zsh: use bash -c for chains; never dump environ; never push site strings/seat prompts to GitHub; never touch .claude/worktrees/proposal.
- D-2315 no age-only deletes; D-2378 kill recorded PIDs only (never taskkill /IM).
