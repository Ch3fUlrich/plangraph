# build-L1-bus card · gen-1 · 2026-10-24 07:1xZ · NO cron · parent routing (D-2534)
Owner build-L1-bus; claims via build-L1.token (D-2536). hb loop: job 56e7e287 tmp/hb.sh over ops/hb-keys.txt (3x8min, restart on exit).
Control branch build-L1-bus (local control.git only). Phase branch phase/rt-p1-bus (origin) base main 0d3a3898, head 12a182f9. CI only PR->main: lanes run via `gh workflow run tests.yml --ref <lane>`.
Review rule D-2534: lanes -> PR into phase branch, green CI, no per-task seats; then ONE HIGH review of phase diff vs main
D-2539: Haiku 5.5 first + 1 non-Claude family != every writer (cx writers -> qwen | mimo/mimo-v2.6-flash | deepseek-direct-flash; luna only w/o openai writer; never sol). Haiku verdict refused by keeper until KEEPER-SEAT-FAMILY-WRITER: keep .out+sha256, post after. D-2540: 1st review = 2 seats (Haiku + 1 non-Claude); then ONE fixer brief -> re-review ONE seat (delta + touched files), loop <=3 rounds then replan; phase PR; GO-REQ to routing.
D-2665 (cx cooldown): mimo > deepseek > agy; D-2569 base cx first; probe first, record skips. Phase writer families so far: xiaomi (EOS r1), openai (KEEPER r2, DISCOVERY), google (KEEPER r3 agy) -> seats Haiku + qwen (never gemini).
UNPARKED D-2657 ~23:2xZ. Phase @66d3cad6 (main 748fe7f3 + EOS + DISCOVERY + KEEPER + WAKE). Plan status/unpark-plan.md. D-2643 batch review.
| Item | State / next |
|---|---|
| RT-P1-BUS-KEEPER (HIGH p8) | IN PHASE: #176 merged a6a17c63 (head b0648e54, CI 40146063397 green; r3 writer agy gemini; b0648e5 L1 1-line w/o Hand-Edit trailer, note in phase PR) |
| EXEC-OBSERVED-STATE (HIGH p5) | IN PHASE: #165 merged 12a182f9 (head 73a24850 incl Hand-Edit fmt), CI 40087453166 green; awaits phase review |
| BUS-WATCHER-TRACKED (p7) | claimed; HELD (D-2564), close superseded when BUS-WAKE lands |
| RT-P1-BUS-WAKE (p7) | IN PHASE: #179 merged 66d3cad6 (head 66c80ccc, CI 40150820122, writer mimo). routing#3 head 864abc10 (cx) ready, merge HELD until wake daemon live; seat at merge time; host steps in PR body |
| RT-QC-ACCEPTS (HIGH p1) | L2 build-L2-qc-accepts ses_e8fd201b9ffeOBldPtSE9uA5my (workst oc_l2, cx; reports via workst inbox relay) |
| BUS-WAKE-STOPGAP-2 | claimed; D-2539 hold, close superseded when BUS-WAKE lands |
| RT-P1-BUS-RUST (p6) | deps CI-WORKST-RUNNER(in_review), OCGO-BUS-INBOUND(open) |
| RT-P1-BUS-DISCOVERY (p6) | IN PHASE: #168 merged 5c741554 (head e6bd35f1, CI 40141473514 green, writer cx) |
| BUS-ANY-AGENT-CLI (HIGH p10) | split 02:3xZ: W2a lane/bus-msg-envelope (keeper sig/nonce/msg_id, schema v15) || W2b lane/bus-any-agent-cli (client, spool, shims, MCP, WakeHook); W2a merges first. Relay :58690 up (unsupervised). Then prod round trip |
| D-2666 routerctl relay '!' | DONE routing#2 merged fc917840; qwen pass (seats routing2-5886eb8c); DONE sent routing (local routing session gone 01:xxZ, queued to RC routing) |
| EXEC-PHASE-LEASE (p8) | dep #140 RT-P1-EXEC-OWNERSHIP in_review (verdict pending) |
07:55Z workst-L1-autoos writing DIRECT (no L2): lane/rt-p1-bus-keeper + lane/exec-observed-state (cx 429 -> mimo/mimo-v2.6-flash, runs 20261024-075304-rt-p1-bus-keeper-a514ed, 20261024-075350-exec-observed-state-86cb4f). Writer families so far: xiaomi (+openai if cx later) -> phase seats Haiku + qwen. Queue: QC A-E in free slots; after KEEPER merge WAKE || DISCOVERY -> ANY-AGENT-CLI. Workst opencode mass-kill 07:20Z (routing informed).
