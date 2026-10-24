# build-L1 card · gen-28 · 2026-10-24 10:5xZ · NO cron · hb loop = jobs/6931599d/tmp/hb.sh (3x8min, exits -> restart) over ops/hb-keys.txt
D-2534 PHASE-BATCH: new phase work = all tasks merged onto phase/<key> (green CI, no per-task seats), ONE review round over phase diff (HIGH=2 families), one fixer brief, <=3 rounds, one phase PR + GO-REQ; in-flight seated items keep seats. Cheapest capable models.
D-2643 BATCH REVIEW (supersedes per-W seats): writers in parallel by file group, gate = affected tests + CI only; ONE review over the whole collected delta at the end. D-2626 affected tests only, full suite at main merge; >10 findings split by file. D-2539/2540 seat families still apply to the final review.
Handoff = status/build-L1.handoff.md (gen-27 -> gen-28). Control repo LOCAL ONLY (D-1751): `git push control build-L1`. Rules D-2458 + D-2472 + D-2459.
Writers (D-2569): cx/gpt-6.1-sol on workst (inline briefs); cx 429/cooldown -> agy/gemini-3.8-flash > mimo/mimo-v2.6-flash; probe first, record skips; gemini writer = no google seat (NEVER omniroute/mimo, operator 10-23). Never Claude writers. D-2442 close idle own sessions.
Seats ONLY via spawner AutoOS main>=5ce05dd9 (workst C:\fleet\wt\ao-lessons-b1\tools\autoos-agent.py), never C:\fleet\clones\AutoOS or bare opencode run (D-2552). qodercli Efficient = qwen (D-1714). deepseek-direct 402 on workst.
Merge: ~/.local/share/plangraph/tools-l1/job-tmp/mergepath.sh PR HEAD40 KEY TITLE (no submit); Gate-Edit variant: GE=D-n job tmp mergepath-ge.sh.
## PH-MODEL-ROUTE: #170 4d8f41e7, #172 W27 2b163d6e, #173 W28-W34 ce92c5fe, #174 W35b/c+W36 748fe7f3 MERGED (qwen seat pass), main green. Prod code host 2/2 OK (A1 card, A2 EMPTY catalog; gemini via agy; prod-run-findings.md). D-2655 FREEZE END MET: workst run-7401b982 rc0 (cx skip->agy) + #174; Windows watchdog task Disabled; gaps G6/G7 -> HARDENING. Watchdog timers installed DISABLED (OS-WATCHDOG-IDENTITY operator). Then FREEZE-END + D-2638 lane list to routing.
| Item | Owner | State / next |
|---|---|---|
| KG-LINK-REPAIR (p9, D-2541 freeze exc.) | L1 + workst cx | LIVE done 07:09Z: kg.conf -> live Omnigraph (backup backups/kg-conf-pre-d2541.conf), export 2041 nodes 0 err, token 0 hits; code brief sent (loud failures, --check alarm, example placeholder) |
| KEEPER-SEAT-FAMILY-WRITER (HIGH p10, gates PH-RT-RUNTIME) | L1 via workst cx | claimed 07:0xZ; brief sent (A store_support writer families, B Hand-Edit CHANGELOG-only excl, separate commit for #139 rebase); seats Haiku + qwen (D-2539) |
| BUS cluster, EXEC-OBSERVED-STATE, EXEC-PHASE-LEASE, RT-QC-ACCEPTS + L2s bus-keeper/qc-accepts | build-L1-bus (D-2534) | released + handed over 07:1xZ; never claim |
| RT-P1-PHASE-RUNNER (HIGH) | DONE | #163 a6587b14 + #167 S4 09f67846 (GO D-2571), keeper done 2/2 (alibaba+google seats/phase-runner-s4). L2 stopped. Follow-up PHASE-HOLDER-EXEC-GATE-ONCE p3 (gates HARDEN) |
| PG-PUBLIC-READY #162 (HIGH) | L1 | MERGED 5bb75e5f (D-2577), main CI green, commit+ci attached; NOT submitted: D-2579 rewrite AFTER PH-MODEL-ROUTE, one announced window + private bundle of old history, then submit + qwen(r8)/gemini(r9,r10) verdicts |
| RT-P1-EXEC-OWNERSHIP #140 (HIGH) | L1 | MERGED 0d3a3898 main green; evidence refused (in_review) -> router |
| UI-BROWSER-TESTS #143 | DONE | #152 merged 135b1685, main CI green, keeper done (qwen r3) |
| D-2657 FREEZE ENDED; D-2638 lanes | L1 | L2s build-L2-review-method d3c8d22a, -hardening cb077891 (+TEST-SPEED-AFFECTED), -pg-public ce88608e (D-2659). #171 -> #175 r2 5b02ffbc GO asked (migration note briefs/lanes-d2657). Keeper lanes serial: #164 > #166 > SEAT-FAMILY-WRITER > #139. D-2660 closed 5 PH-MODEL-ROUTE items; #175 MERGED 0a65abac. LIVE-SYNC-9 (D-2664): live stays 2d61e281; a) autoos:SPAWNER-ITEM-KEY (workst) + smoke briefs/lanes-d2657/ls9-mint-smoke.sh, b) OS-KEEPER-LS9-MIGRATE operator, c) z8 recreate; worktree .claude/worktrees/live-0a65abac. Keeper lanes: asked workst to push #164 r5/SEAT-FAMILY/KG-LINK |
| KEEPER-TRAILER-MULTI (HIGH) | L1 via workst | D-2570: #139 CLOSED (fix13 HIGH L1-repro: token-order fail-open). Re-cut lane/keeper-trailer-strict from a6587b14, brief seats/trailer-multi/recut sent 11:4xZ (cx else agy, no mimo). First review 2 non-Claude families != writer, <=3 rounds then stop+report |
| JEV-ATTEMPT-THREAD + #96 JEV-SHADOW | DONE | #154 merged de54bb22; JEV-SHADOW closed D-2448 |
| BUS-NONCE-FLOOD #159 | DONE | merged e612b836, keeper done 2/2 |
| WAIT-GUARD #158 | L1 | MERGED 27082207, evidence, in_review; LOWs leaf KEEPER-WAIT-GUARD-PRECISION asked. B1-KEEPER lane/lessons-b1-keeper (HIGH) in flight |
| SEAT-VERDICT-QODER-ALIAS p9 | L1 | #161 MERGED f8e5f051, main CI green, evidence, in_review; agy APPROVE .out 3b8217c6 (prompt afd766bb); verdict = agy re-run via GW-QODER-401 adapter. D-2515 re-seats after GW-QODER-401 adapter: RT-P1-WORKER-CODE (1 fix part) + RT-P1-KEEPER-PG-CODE (HIGH, 2 fix parts), fresh qoder on original findings at main; qseat.py --adjudication retired. OLD: D-2495 claimed; writer queued workst after sq10 -> 1 dsfree seat |
| CARD-CHECKS #157 / WF-VERSIONING | build-L2-rt-cards | WFV lane 3 r3 off 7e53f0d1: legs A-F go, Gate-Edit D-n asked router (H1-H4 tests.yml + ci_select); HIGH -> 2 seats + GO. D-2506: standard closes on 1 verdict; tier-check fwd to B1. CARD-CHECKS DONE: merged 7e53f0d1, main CI green, keeper done 2/2 (alibaba). Next lane 3 WF-VERSIONING GO-REQ (Gate-Edit). OLD: 2b b9fc8a7f CI green, qwen delta FIX -> (a) writer F2+F3+sweep -> re-seat -> MERGE-READY |
| RT-OVERSIGHT-KEEPER-SCOPE #164 (HIGH) | L1 via workst | REPLAN asked router: r5 75b1761 (local) incomplete + REGRESSION test_fix_d tier rule (r4 raise-only for all principals). Default (a) r5 + 1 narrow cx round, hash-chain -> follow-up leaf | OS-OVERSIGHT-TIMER D-2562: units on code DISABLED until LIVE-SYNC-9 (schema 5->7), then enable + first run in LIVE-SYNC-9 DONE.
| RT-P1-BUS-CONTRACT | DONE | #153 merged 432c5377, keeper done (mimo + deepseek r2) |
| KEEPER-OPCLAIM-CLOSE #127 | DONE | LIVE-SYNC-9 after SPAWNER-ITEM-KEY + mint smoke (HIGH GO) |
| KEEPER-LEGACY-HUMAN-FAMILY | L1 queue | D-2348, bundle w/ LIVE-SYNC-9 |
| PG-BOOTSTRAP-FLEET-ENV (HIGH) | L1 queue | blocked autoos:AO-OCL1-PW-FILE |
| PG-OMNIROUTE-CONFIG-HOME (p6) | L1 queue | D-2359, L2 after PH-RT-RUNTIME |
| #100 SANDBOX | sweep-runtime | Gate-Edit squash pending |
| OCGO-PROBE / OCGO-L1-FALLBACK | released D-2409 | trigger OS-OCL1-ENV |
- D-2418 CI GitHub-hosted (D-1718). D-2315 no age-only deletes; D-2378 kill recorded PIDs only.
## Traps
- D-2555 workst L2 launch/recycle: cwd = C:\fleet\AutoOS (main 5ce05dd9) or --mcp-config C:\fleet\AutoOS\tools\autoos_agent_mcp.py; else MCP tools are stale. phase-runner: CLI spawner only till recycle.
- D-2466 DONE: SEAT-FREE-OPENCODE on AutoOS main 5ce05dd9; coding.vm CLAUDE.md mimo block removed (backup job tmp). B1 build-L2-lessons-b1 STARTED 03:0xZ workst as Sonnet L2 (D-2476 A override; OpenCode l2_start blocked: AUTOOS_OCL1_PW unset, AO-OCL1-PW-FILE unmerged).
- Keeper refuses reviewer-anthropic verdicts on build-L1 claims (same_family): Haiku seats cannot be recorded; ruling asked 12:4xZ.
- Never eval a range of mergepath.sh. Verdict needs in_review; mergepath does not submit.
- ONE final trailer block per commit; Hand-Edit <=10 lines; workflow edit needs Gate-Edit D-n.
- zsh: use bash -c for chains; never dump environ; never push site strings/seat prompts to GitHub; never touch .claude/worktrees/proposal.
