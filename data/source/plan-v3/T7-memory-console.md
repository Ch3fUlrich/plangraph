# T7 — Memory graph, fleet console, board

| Lane | What | Owner | DONE (evidence) |
|---|---|---|---|
| T7-GRAPH-R | Routing repo graph: every D-NNN, standing order and operator step in omnigraph `autoos` (project `routing`), synced on every heartbeat (`bin/sync-memory-graph --load`); Rule/Decision ↔ Component links where a decision changes an AutoOS component | routing-00 | query returns all D-ids; last sync < 1 h old |
| T7-GRAPH-A | AutoOS graph: components from the repo structure (graphify communities as input), ADRs/plans/decisions, skill rules (enforced vs advisory), tasks from `docs/tasks.md`; merge by @key, edges once (loaded-ledger), verify by query; nightly incremental sync from git | AutoOS L1-main → L2 | counts per node type; whoami query (CLAUDE.md) returns 1 row; drift report empty |
| T7-GRAPH-USE | Agents use the graph: session start pulls `context_pack(project, role)` ≤ 1.5k tokens; decisions/lessons written through the facade (MEMSPEC P1/P2) | AutoOS L1-main | measured: tokens at relaunch down; recall test 30 questions ≥ 80 % |
| T7-BOARD | Board evaluation (operator: re-evaluate self-hosted options): Plane, Vikunja, Planka, Focalboard/Mattermost Boards, Vibe Kanban (community), console-native board (P4); criteria from PLAN §19 (dependencies/blocked, unassigned pool, pause per project/plan, agent API/MCP, OIDC/Authelia, phone UX, maintenance health, RAM) | L2 researcher + 2 reviewers | scorecard + recommendation → **OPERATOR** chooses (taste) |
| T7-CONSOLE-MSG | Console pages for agent messaging (T2-XMSG: who told whom what, delivery state, threads per lane) and the secret-exchange audit (T2-XSECRET: names/hosts/times, never values) — built in-house, because these parts are what makes the fleet Claude-independent (the board alone is reused) | AutoOS L1-main | messages and exchanges visible within 5 s; phone-usable |
| T7-CONSOLE | Fleet console P2 (cost per run), P3 (act from the phone: answer/approve/steer/stop, push, spawn tree, project pages), P4 (board as the store, decision traces) per FLEETSPEC, using the chosen board | AutoOS L1-main | per-phase acceptance tests in FLEETSPEC |

Graph navigation tools: agents use Serena (symbols), graphify (structure/communities) and omnigraph (decisions/memory)
before reading files wholesale.
