# build-L1-bus card · gen-2 · 2026-10-25 · NO cron · parent routing (D-2534)
D-2694 (10-25): L3 writer+seat max_tokens=128000 default, clamp only to leg ceiling; finish=length+empty -> retry at ceiling, never skip. Put in every brief.
D-2706 (10-25): spawn floor MemAvailable 2000 MB (replaces D-2686 4000); max 4 concurrent writers on coding.vm, rest workst; L1 restart = clear context; 1 compaction = recycle.
Owner build-L1-bus; claims via build-L1.token (D-2536). hb loop: ops/hb.sh over ops/hb-keys.txt (3x8min, restart on exit).
Control branch build-L1-bus (local control.git only). Phase branch phase/rt-p1-bus (origin) base main 0d3a3898, head 12a182f9. CI only PR->main: lanes run via `gh workflow run tests.yml --ref <lane>`.
Review rule D-2534: lanes -> PR into phase branch, green CI, no per-task seats; then ONE HIGH review of phase diff vs main
D-2539: Haiku 5.5 first + 1 non-Claude family != every writer (cx writers -> qwen | mimo/mimo-v2.6-flash | deepseek-direct-flash; luna only w/o openai writer; never sol). Haiku verdict refused by keeper until KEEPER-SEAT-FAMILY-WRITER: keep .out+sha256, post after. D-2540: 1st review = 2 seats (Haiku + 1 non-Claude); then ONE fixer brief -> re-review ONE seat (delta + touched files), loop <=3 rounds then replan; phase PR; GO-REQ to routing.
D-2665 (cx cooldown): mimo > deepseek > agy; D-2684: writers gemini > mimo-pro > openrouter/free + opencode free > mimo-flash; keeper multi-file gemini else mimo-pro (cx out ~134h, deepseek 402); fix r1 writers G1 agy, G3 mimo, G2 mimo; D-2569 base cx first; probe first, record skips. Phase writer families so far: xiaomi (EOS r1), openai (KEEPER r2, DISCOVERY), google (KEEPER r3 agy), maybe deepseek (W2) -> seats Haiku + qwen (never gemini/deepseek).
UNPARKED D-2657 ~23:2xZ. Phase @aded8d82 (main 4608fd3f + EOS + DISCOVERY + KEEPER + WAKE + W2a #180 + W2b #181), CI 40160578251. CI green. PHASE REVIEW r1 DONE: Haiku fix (sha c1ed9464) + qwen fix (out sha b92a2af1), SHA256SUMS in build-L1-bus-seats/phase-r1-aded8d82. FIX r1 (G1+G3 LAUNCHED, G2 queued) sent to workst 04:2xZ: G1 keeper / G2 client / G3 exec+wake (l2-cards/phase-fix-r1/brief.md), merge G1>G3>G2, then 1-seat delta re-review (qwen). QC lanes join via delta. Follow-up leaves SENT routing 04:3xZ (queued, RC offline): V4-5 routing/AutoOS hardcoded peer -> discovery; V4-3 keeper-persisted registry. Plan status/unpark-plan.md. D-2643 batch review.
| Item | State / next |
|---|---|
| RT-P1-BUS-KEEPER (HIGH p8) | IN PHASE: #176 merged a6a17c63 (head b0648e54, CI 40146063397 green; r3 writer agy gemini; b0648e5 L1 1-line w/o Hand-Edit trailer, note in phase PR) |
| EXEC-OBSERVED-STATE (HIGH p5) | IN PHASE: #165 merged 12a182f9 (head 73a24850 incl Hand-Edit fmt), CI 40087453166 green; awaits phase review |
| BUS-WATCHER-TRACKED (p7) | claimed; HELD (D-2564), close superseded when BUS-WAKE lands |
| RT-P1-BUS-WAKE (p7) | IN PHASE: #179 merged 66d3cad6 (head 66c80ccc, CI 40150820122, writer mimo). routing#3 head 864abc10 (cx) ready, merge HELD until wake daemon live; seat at merge time; host steps in PR body |
| RT-QC-ACCEPTS (HIGH p1) | L2 ses_e8fd201b STOPPED, no lane pushed (cx out). Relaunched 05:0xZ as DIRECT workst writers A-E (D-2683): B/C/E on free slot, A after G1, D needs Gate-Edit GO-REQ. Brief l2-cards/qc-accepts/brief.md |
| BUS-WAKE-STOPGAP-2 | claimed; D-2539 hold, close superseded when BUS-WAKE lands |
| RT-P1-BUS-RUST (p6) | deps CI-WORKST-RUNNER(in_review), OCGO-BUS-INBOUND(open) |
| RT-P1-BUS-DISCOVERY (p6) | IN PHASE: #168 merged 5c741554 (head e6bd35f1, CI 40141473514 green, writer cx) |
| BUS-ANY-AGENT-CLI (HIGH p10) | IN PHASE: W2a #180 (503c70df, agy) + W2b #181 (09ca839e, mimo + Hand-Edit bf46b49 exec bit), CI 40160072272. Prod round trip after phase on main + keeper deploy |
| D-2666 routerctl relay '!' | DONE routing#2 merged fc917840; qwen pass (seats routing2-5886eb8c); DONE sent routing (local routing session gone 01:xxZ, queued to RC routing) |
| BUS-REGISTRY-PERSIST (HIGH p8, D-2671) | owner me; dep DISCOVERY (claim refused till done); lane in phase AFTER G1 merges (keeper table bus_hosts) |
| BUS-PEER-DISCOVERY-ADOPT (HIGH p8, D-2671) | owner me; routing + AutoOS repos; gates ANY-AGENT-CLI; after G1 (/v1/hosts) |
| EXEC-PHASE-LEASE (p8) | dep #140 RT-P1-EXEC-OWNERSHIP in_review (verdict pending) |
07:55Z workst-L1-autoos writing DIRECT (no L2): lane/rt-p1-bus-keeper + lane/exec-observed-state (cx 429 -> mimo/mimo-v2.6-flash, runs 20261024-075304-rt-p1-bus-keeper-a514ed, 20261024-075350-exec-observed-state-86cb4f). Writer families so far: xiaomi (+openai if cx later) -> phase seats Haiku + qwen. Queue: QC A-E in free slots; after KEEPER merge WAKE || DISCOVERY -> ANY-AGENT-CLI. Workst opencode mass-kill 07:20Z (routing informed).
10-25 gen-2: fix r1 NOT ready. G1 lane/phase-fix-r1-g1 181bdc15 CI 40166052005 PG red (10 test_keeper_messages; SQLite-only BEGIN IMMEDIATE/PRAGMA in shared store) -> agy continuation pfg1pg. G2 mimo truncated no code -> agy pfg2r2 queued. G3 mimo running. Watch: base aded8d82 local PG16 fails test_nonce_table_append_only_triggers (CI 40160578251 was green) -> check before phase PR.
06:3xZ G1 MERGED phase #186 -> e30b2ae3 (head c9d4bc38, CI 40168236971 green incl PG). G3 next, then G2 (mimo-pro; agy 429 ~07:09Z).
BUS-REGISTRY-PERSIST: writer mimo-v2.6-pro (agy 429 skip) run 20261025-062933-bus-registry-persist-wri-a2456f, worktree build-L1-bus-persist lane/bus-registry-persist, brief l2-cards/registry-persist/brief.md; bus_hosts in v15/PG8 in place; schema fence sent build-L1 [51fc4d].
BUS-PEER-DISCOVERY-ADOPT: L2-LAUNCH bus-discovery-adopt sent routing 06:3xZ; brief l2-cards/discovery-adopt/brief.md; lanes routing-worktrees/ + AutoOS-worktrees/bus-discovery-adopt (lane/bus-discovery-adopt off main).
Schema fence agreed w/ build-L1: #164 claims SQLite v14 + PG Migration(8) oversight (collides with bus PG 8). First to main keeps its numbers; second renumbers on its main merge (bus -> PG 9 / SQLite next). build-L1 pings on #164 GO.
D-2712: L2 bus-discovery-adopt = session b1957139 (sonnet medium). Routing lane MERGE-READY -> routing merges routing main. Future L2 briefs: add stop-on-denial rule.
07:0xZ G3 MERGED phase #189 (head 5feefbf5, CI 40170369570; Hand-Edit fmt). G2 mimo-pro running on workst since 06:15Z.
07:5xZ PERSIST writer done: lane/bus-registry-persist 18c9322c (2 commits cherry-picked, Writer mimo-v2.6-pro xiaomi; PG16 9 passed; FLEET-BUS.md doc para MISSING -> delta finding); CI 40173851581 running; pg16-bus-reg container removed.
