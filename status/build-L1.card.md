# build-L1 card · gen-27 · 2026-10-24 07:0xZ · NO cron · hb loop = jobs/da7add91/tmp/hb.sh (3x8min, exits -> restart) over ops/hb-keys.txt
D-2534 PHASE-BATCH: new phase work = all tasks merged onto phase/<key> (green CI, no per-task seats), ONE review round over phase diff (HIGH=2 families), one fixer brief, <=3 rounds, one phase PR + GO-REQ; in-flight seated items keep seats. Cheapest capable models.
D-2539+D-2540 seats: Haiku FIRST when no Claude writer; first review of a phase diff ALWAYS 2 seats (Haiku + 1 non-Claude, both != every writer; luna never over cx/sol); fix re-review = 1 seat (delta + touched), <=3 rounds then replan; agy/oc-free = recorded fallback.
Handoff = status/build-L1.handoff.md (gen-25 -> gen-26). Control repo LOCAL ONLY (D-1751): `git push control build-L1`. Rules D-2458 + D-2472 + D-2459.
Writers: cx/gpt-6.1-sol on workst (inline briefs) > mimo/mimo-v2.6-flash (NEVER omniroute/mimo, operator 10-23). Never Claude writers. D-2442 close idle own sessions.
Seats ONLY via spawner AutoOS main>=5ce05dd9 (workst C:\fleet\wt\ao-lessons-b1\tools\autoos-agent.py), never C:\fleet\clones\AutoOS or bare opencode run (D-2552). qodercli Efficient = qwen (D-1714). deepseek-direct 402 on workst.
Merge: ~/.local/share/plangraph/tools-l1/job-tmp/mergepath.sh PR HEAD40 KEY TITLE (no submit); Gate-Edit variant: GE=D-n job tmp mergepath-ge.sh.
## Main = 0d3a3898 (#160). D-2532 P1 FREEZE (see handoff). LIVE keeper restarted 06:41Z (D-2525). KEEPER-PG-RECONNECT run 20261024-072226-keeper-pg-reconnect-d5921f (mimo) running
| Item | Owner | State / next |
|---|---|---|
| KG-LINK-REPAIR (p9, D-2541 freeze exc.) | L1 + workst cx | LIVE done 07:09Z: kg.conf -> live Omnigraph (backup backups/kg-conf-pre-d2541.conf), export 2041 nodes 0 err, token 0 hits; code brief sent (loud failures, --check alarm, example placeholder) |
| KEEPER-SEAT-FAMILY-WRITER (HIGH p10, gates PH-RT-RUNTIME) | L1 via workst cx | claimed 07:0xZ; brief sent (A store_support writer families, B Hand-Edit CHANGELOG-only excl, separate commit for #139 rebase); seats Haiku + qwen (D-2539) |
| BUS cluster, EXEC-OBSERVED-STATE, EXEC-PHASE-LEASE, RT-QC-ACCEPTS + L2s bus-keeper/qc-accepts | build-L1-bus (D-2534) | released + handed over 07:1xZ; never claim |
| RT-P1-PHASE-RUNNER #163 (HIGH, was #150 closed) | build-L2-phase-runner | #163 8721941 spine/phase-runner-s3d-r2 (D-2545); CI running; seats Haiku+qwen; prove 3 WSL env fails in CI -> MERGE-READY -> GO-REQ |
| PG-PUBLIC-READY #162 (HIGH) | L1 via workst cx | r4 08bf8000 gitleaks clean (L1-verified); seats r6: qwen PASS, Haiku FIX (1 HIGH README site literals, 3 MED test pins) -> r5 fixer brief sent; then 1 seat delta -> GO -> D-2501 rewrite |
| RT-P1-EXEC-OWNERSHIP #140 (HIGH) | L1 | MERGED 0d3a3898 main green; evidence refused (in_review) -> router |
| UI-BROWSER-TESTS #143 | DONE | #152 merged 135b1685, main CI green, keeper done (qwen r3) |
| GOV-CTX-METRICS #144 | DONE | merged 2aaaf35f, keeper done |
| KEEPER-TRAILER-MULTI #139 (HIGH) | L1 via workst | fix12 run 20261024-072324-keeper-trailer-multi-fix-5e9e11 (mimo, cx 429; relaunched after 07:20Z kill). Writers now openai+xiaomi -> seats Haiku + qwen |
| JEV-ATTEMPT-THREAD + #96 JEV-SHADOW | DONE | #154 merged de54bb22; JEV-SHADOW closed D-2448 |
| BUS-NONCE-FLOOD #159 | DONE | merged e612b836, keeper done 2/2 |
| WAIT-GUARD #158 | L1 | MERGED 27082207, evidence, in_review; LOWs leaf KEEPER-WAIT-GUARD-PRECISION asked. B1-KEEPER lane/lessons-b1-keeper (HIGH) in flight |
| SEAT-VERDICT-QODER-ALIAS p9 | L1 | #161 MERGED f8e5f051, main CI green, evidence, in_review; agy APPROVE .out 3b8217c6 (prompt afd766bb); verdict = agy re-run via GW-QODER-401 adapter. D-2515 re-seats after GW-QODER-401 adapter: RT-P1-WORKER-CODE (1 fix part) + RT-P1-KEEPER-PG-CODE (HIGH, 2 fix parts), fresh qoder on original findings at main; qseat.py --adjudication retired. OLD: D-2495 claimed; writer queued workst after sq10 -> 1 dsfree seat |
| CARD-CHECKS #157 / WF-VERSIONING | build-L2-rt-cards | WFV lane 3 r3 off 7e53f0d1: legs A-F go, Gate-Edit D-n asked router (H1-H4 tests.yml + ci_select); HIGH -> 2 seats + GO. D-2506: standard closes on 1 verdict; tier-check fwd to B1. CARD-CHECKS DONE: merged 7e53f0d1, main CI green, keeper done 2/2 (alibaba). Next lane 3 WF-VERSIONING GO-REQ (Gate-Edit). OLD: 2b b9fc8a7f CI green, qwen delta FIX -> (a) writer F2+F3+sweep -> re-seat -> MERGE-READY |
| RT-OVERSIGHT-KEEPER-SCOPE #138 (HIGH) | L1 via workst (L2 flow-oversight-3 recycled) | lane/keeper-scope-repair LOCAL f8e5f051+e565a9c(cx)+851db9d(mimo); r3 test writer + full suite running; deviation: SQLite SCHEMA_VERSION 13 kept, OVERSIGHT_SCHEMA_VERSION 14, PG Mig 8 -> put in GO-REQ. Seats Haiku+qwen after main merge + push |
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
