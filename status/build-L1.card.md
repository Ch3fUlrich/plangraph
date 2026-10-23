# build-L1 card · gen-23 · 2026-10-23 14:5xZ · NO cron · hb loop = job-tmp hb.sh (3x8min, exits -> restart) over ops/hb-keys.txt
Handoff #23 = docs/build-L1-handoff.md top (order list). Control repo LOCAL ONLY (D-1751): `git push control build-L1`.
Writers: omniroute/mimo-v2.6-flash via autoos-agent spawn (sol back ~13:52Z D-2432; free muse-spark-1.3-contributor ok). Never Claude writers.
Seats D-2399: deepseek (`sh ops/seat.sh deepseek/deepseek-flash`), Haiku (Agent model haiku), mimo, luna, qwen (429 to 10-24 15:30Z). No opencode free/OR free/agy.
Merge: ~/.local/share/plangraph/tools-l1/job-tmp/mergepath.sh PR HEAD40 KEY TITLE (no submit); Gate-Edit variant: GE=D-n job tmp mergepath-ge.sh.
## Main = 5671ac77 (#145 RT95 DONE). LIVE keeper = live-2d61e281 (never ff)
| Item | Owner | State / next |
|---|---|---|
| RT-P1-EXEC-OWNERSHIP #140 (HIGH) | L1 | sq6 0f516bc (sol 6ab8f2, verb requeue; unverified full/PG) + main merge -> head cc0edd70 CI running -> seats deepseek+Haiku (recipe sq5/PROMPT.txt) -> re-GO-REQ |
| UI-BROWSER-TESTS #143 | L1 | PR #152 head 32051a6a CI green; r2 seats both FIX (Haiku MED edit scan unscoped; deepseek evidence gaps) -> fix3 sol on workst queued after B -> CI, re-seat (r2b prompt w/ writer-report.txt) |
| GOV-CTX-METRICS #144 | L1 | r3 seats: deepseek MED None-ts sort TypeError, Haiku LOW id-less doc -> fix3 sol RUNNING on workst (brief B) -> CI, r4 seats -> merge |
| KEEPER-TRAILER-MULTI #139 (HIGH) | L1 | fix4 470492 taken -> head 3190b3fb CI green; seats FIX: Haiku HIGH subject not scanned, luna HIGH real-repo 2-writer verdict test + f6fadc23 -> fix5 MIMO writer (brief C, workst) -> CI, Haiku+luna -> GO-REQ |
| PG-PUBLIC-FIX | DONE | closed D-2430 |
| PG-PUBLIC-READY (HIGH) | L1 | claim epoch 2; r4 seats deepseek + luna (workst sha256 review a434ce1e): no code finding; both FIX only on names-list scan + later gates -> after operator scan line: re-run both, submit, GO-REQ, luna asked workst; IDLE: operator runs words --check on c218b921 (D-2434), trigger = his result line; luna r4 workst builds prompt, runs ~13:52Z -> submit, verdicts, GO-REQ |
| #96 JEV-SHADOW (HIGH) | L1 | in_review; closes with JEV-ATTEMPT-THREAD (1 seat combined diff) |
| KEEPER-CLOSE-SKIP-UNVERIFIED #148 | DONE | closed D-2439; tell routing when LIVE-SYNC-9 runs it (PW-ROTATE+MT-226) |
| JEV-ATTEMPT-THREAD p7 | L1 | PR #154 head 31bd5a4e (d305c64 sol f55fce + main) CI running -> 2 non-openai seats (deepseek+Haiku) -> GO-REQ (HIGH via #96) |
| BUS-NONCE-FLOOD p4 | L1 queue | D-2422 created (PH-RT-CONSOLIDATE); unclaimed, starts after P1 writers free |
| EXEC-OBSERVED-STATE | L1 queue | follow-up of #140 (router created) |
| RT-P1-PHASE-RUNNER (HIGH) | build-L2-phase-runner (workst) | S3d writers f55416 + a131e1 (sol) done; role fix must be writer (D-2240, told); suite -> PR -> 2 seats -> MERGE-READY after #140 |
| CARD-CHECKS / WF-VERSIONING (RT95 done 5671ac77) | build-L2-rt-cards (workst) | brief briefs/rt-cards-wfv-rt95.md |
| RT-OVERSIGHT-KEEPER-SCOPE #138 (HIGH) | build-L2-flow-oversight-3 | IDLE head 448e80a0 (5551bc done); trigger = #140 on main -> I send "#140 ON MAIN" -> Mig8 merge, CI, luna+Haiku |
| RT-P1-BUS-CONTRACT (HIGH) | L1 | vectors PR #153 head ee86215f CI green; seats deepseek+mimo RUNNING (seats/pr153) -> GO-REQ, merge, submit |
| KEEPER-OPCLAIM-CLOSE #127 | DONE | LIVE-SYNC-9 after SPAWNER-ITEM-KEY + mint smoke (HIGH GO) |
| KEEPER-LEGACY-HUMAN-FAMILY | L1 queue | D-2348, bundle w/ LIVE-SYNC-9 |
| PG-BOOTSTRAP-FLEET-ENV (HIGH) | L1 queue | blocked autoos:AO-OCL1-PW-FILE |
| PG-OMNIROUTE-CONFIG-HOME (p6) | L1 queue | D-2359, L2 after PH-RT-RUNTIME |
| #100 SANDBOX | sweep-runtime | Gate-Edit squash pending |
| OCGO-PROBE / OCGO-L1-FALLBACK | released D-2409 | trigger OS-OCL1-ENV |
- D-2418: plangraph CI stays GitHub-hosted (public repo, D-1718); no runs-on change; BUS-RUST windows build = GitHub-hosted windows.
## Traps
- Keeper refuses reviewer-anthropic verdicts on build-L1 claims (same_family): Haiku seats cannot be recorded; ruling asked 12:4xZ.
- Never eval a range of mergepath.sh. Verdict needs in_review; mergepath does not submit.
- ONE final trailer block per commit; Hand-Edit <=10 lines; workflow edit needs Gate-Edit D-n.
- zsh: use bash -c for chains; never dump environ; never push site strings/seat prompts to GitHub; never touch .claude/worktrees/proposal.
- D-2315 no age-only deletes; D-2378 kill recorded PIDs only (never taskkill /IM).
