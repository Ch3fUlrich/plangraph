# build-L1 card · gen-24 · 2026-10-23 17:2xZ · NO cron · hb loop = job-tmp hb.sh (3x8min, exits -> restart) over ops/hb-keys.txt
Handoff #24 = docs/build-L1-handoff.md top (order list). Control repo LOCAL ONLY (D-1751): `git push control build-L1`.
Writers: cx/gpt-6.1-sol on workst (inline briefs) > mimo/mimo-v2.6-flash (NEVER omniroute/mimo, operator 10-23). Never Claude writers. D-2442 close idle own sessions.
Seats D-2449/2450: qwen qodercli (job-tmp qseat.sh; efficient fallback ok), Haiku (Agent haiku), mimo, luna (workst); deepseek API OUT (402).
Merge: ~/.local/share/plangraph/tools-l1/job-tmp/mergepath.sh PR HEAD40 KEY TITLE (no submit); Gate-Edit variant: GE=D-n job tmp mergepath-ge.sh.
## Main = 2aaaf35f (#144 GOV-CTX DONE). LIVE keeper = live-2d61e281 (never ff)
| Item | Owner | State / next |
|---|---|---|
| RT-P1-EXEC-OWNERSHIP #140 (HIGH) | L1 | sq7 b9a378b7 CI green; Haiku BLOCKER -> sq8 mimo 12aa4f failed 503 (flapping); sq8 cx SCHEDULED 18:47Z workst -> mergemain, CI, qwen+Haiku -> GO-REQ |
| UI-BROWSER-TESTS #143 | L1 | PR #152 head 32051a6a; fix3 6836f4 STUCK cx 429, WIP patch saved; relaunch 18:46Z cx (pr152/FIX3-BRIEF.md) -> CI, qwen+Haiku re-seat -> merge, submit |
| GOV-CTX-METRICS #144 | DONE | merged 2aaaf35f, keeper done |
| KEEPER-TRAILER-MULTI #139 (HIGH) | L1 | head 3190b3fb; fix5: mimo 503 x3, sol 615435 STUCK cx 429; relaunch 18:46Z cx -> CI, seats (mimo: Haiku+luna; sol: Haiku+qwen) -> GO-REQ |
| PG-PUBLIC-FIX | DONE | closed D-2430 |
| PG-PUBLIC-READY (HIGH) | L1 | waits operator item PG-PUBLIC-WORDS-CHECK (D-2455; edge refused on claimed, option A hold submit); claim epoch 2; r4 seats deepseek + luna (workst sha256 review a434ce1e): no code finding; both FIX only on names-list scan + later gates -> after operator scan line: re-run both, submit, GO-REQ, luna asked workst; IDLE: operator runs words --check on c218b921 (D-2434), trigger = his result line; luna r4 workst builds prompt, runs ~13:52Z -> submit, verdicts, GO-REQ |
| KEEPER-CLOSE-SKIP-UNVERIFIED #148 | DONE | closed D-2439; tell routing when LIVE-SYNC-9 runs it (PW-ROTATE+MT-226) |
| JEV-ATTEMPT-THREAD + #96 JEV-SHADOW | DONE | #154 merged de54bb22; JEV-SHADOW closed D-2448 |
| BUS-NONCE-FLOOD p4 | L1 | claimed epoch 1; writer E queued workst (after #139, cx 18:46Z+) (lane/bus-nonce-flood @de54bb22) -> CI, qwen+mimo -> merge, submit |
| EXEC-OBSERVED-STATE | L1 queue | follow-up of #140 (router created) |
| KEEPER-OPERATOR-WAIT-GUARD (p6) | L1 queue | D-2455 standard; operator-wait needs operator dep + edge edit on claimed |
| RT-P1-PHASE-RUNNER #150 (HIGH) | build-L2-phase-runner | C3 1fee33c local (not pushed); C1/C2 cx stalls (quota) -> ruling (a) relaunch C1 then C2 after 18:55Z; no push of #139 merge f6b4f6c; merge after #139 + #140 |
| CARD-CHECKS #147 / WF-VERSIONING | build-L2-rt-cards | head 042e313f CI green; qwen r2 FIX A,B(H) D(M) H(L) -> ruling opt 1: fix A,B,D,H one writer, F -> 2b; re-seat qwen + non-openai -> MERGE-READY; then 2b GO-REQ, WF-VERSIONING |
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
- Keeper refuses reviewer-anthropic verdicts on build-L1 claims (same_family): Haiku seats cannot be recorded; ruling asked 12:4xZ.
- Never eval a range of mergepath.sh. Verdict needs in_review; mergepath does not submit.
- ONE final trailer block per commit; Hand-Edit <=10 lines; workflow edit needs Gate-Edit D-n.
- zsh: use bash -c for chains; never dump environ; never push site strings/seat prompts to GitHub; never touch .claude/worktrees/proposal.
- D-2315 no age-only deletes; D-2378 kill recorded PIDs only (never taskkill /IM).
