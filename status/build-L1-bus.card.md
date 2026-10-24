# build-L1-bus card · gen-1 · 2026-10-24 07:1xZ · NO cron · parent routing (D-2534)
Owner build-L1-bus; claims via build-L1.token (D-2536). hb loop: job 56e7e287 tmp/hb.sh over ops/hb-keys.txt (3x8min, restart on exit).
Control branch build-L1-bus (local control.git only). Phase branch phase/rt-p1-bus (origin) base main 0d3a3898.
Review rule D-2534: lanes -> PR into phase branch, green CI, no per-task seats; then ONE HIGH review of phase diff vs main
D-2539: Haiku 5.5 first + 1 non-Claude family != every writer (cx writers -> qwen | mimo/mimo-v2.6-flash | deepseek-direct-flash; luna only w/o openai writer; never sol). Haiku verdict refused by keeper until KEEPER-SEAT-FAMILY-WRITER: keep .out+sha256, post after. D-2540: 1st review = 2 seats (Haiku + 1 non-Claude); then ONE fixer brief -> re-review ONE seat (delta + touched files), loop <=3 rounds then replan; phase PR; GO-REQ to routing.
| Item | State / next |
|---|---|
| RT-P1-BUS-KEEPER (HIGH p8) | claimed; L2 build-L2-bus-keeper lane 1 |
| EXEC-OBSERVED-STATE (HIGH p5) | claimed; bus-keeper lane 2 |
| BUS-WATCHER-TRACKED (p7) | claimed; HELD (D-2564), close superseded when BUS-WAKE lands |
| RT-P1-BUS-WAKE (p7) | after BUS-KEEPER in phase; bus-keeper lane 4; claim then |
| RT-QC-ACCEPTS (HIGH p1) | claimed; L2 build-L2-qc-accepts |
| BUS-WAKE-STOPGAP-2 | claimed; D-2539 hold, close superseded when BUS-WAKE lands |
| RT-P1-BUS-RUST (p6) | deps CI-WORKST-RUNNER(in_review), OCGO-BUS-INBOUND(open) |
| RT-P1-BUS-DISCOVERY (p6) | D-2564 edge dropped; claimed 10:33Z; brief sent workst (lane/rt-p1-bus-discovery, next slot, resolve() API for ANY-AGENT-CLI) |
| BUS-ANY-AGENT-CLI (HIGH p10, D-2562) | after KEEPER; needs WAKE + DISCOVERY; prod round trip routing<->workst Claude + 1 OpenCode; relay :58690 down (FLEET-BUS-RELAY-RESTORE) |
| EXEC-PHASE-LEASE (p8) | dep #140 RT-P1-EXEC-OWNERSHIP in_review (verdict pending) |
09:5xZ both runs alive ~1h52m, no final suite; 3h rule: stop + push WIP + slim relaunch (~10:55Z). 07:55Z workst-L1-autoos writing DIRECT (no L2): lane/rt-p1-bus-keeper + lane/exec-observed-state (cx 429 -> mimo/mimo-v2.6-flash, runs 20261024-075304-rt-p1-bus-keeper-a514ed, 20261024-075350-exec-observed-state-86cb4f). Writer families so far: xiaomi (+openai if cx later) -> phase seats Haiku + qwen. Queue: QC A-E in free slots; after KEEPER merge WAKE || DISCOVERY -> ANY-AGENT-CLI. Workst opencode mass-kill 07:20Z (routing informed).
