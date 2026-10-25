# build-L1-bus unpark lane list (D-2638) · 2026-10-24 18:5xZ
Start on build-L1's freeze-end word. Writers on workst per D-2569 (cx > agy gemini > mimo > qodercli). Lanes PR into phase/rt-p1-bus;
CI by `gh workflow run tests.yml --ref <lane>` (tests.yml runs on PR->main only). Phase base: main 0d3a3898; main now 5bb75e5f (38 files differ) ->
step 0 merges main into the phase branch first.

| # | Lane | Item | Files (owned) | Start |
|---|---|---|---|---|
| 0 | phase/rt-p1-bus | merge origin/main | — | first |
| 1 | lane/rt-p1-bus-discovery @629dafad, PR #168 | RT-P1-BUS-DISCOVERY | src/plangraph/fleet_bus/discovery.py, docs/bus-discovery.md, tests/test_bus_discovery.py, CHANGELOG | CI 40089375376 green on 629dafad -> merge after step 0 + re-CI |
| 2 | lane/rt-p1-bus-keeper @433a92c4 | RT-P1-BUS-KEEPER | keeper/{api,api_routes,db,schema,schema_pg,store}.py, keeper/{messages_poll,messages_schema,store_messages}.py, tests/test_keeper_messages.py | CI dispatch, then ONE fixer brief from failures |
| 3 | lane/rt-p1-bus-wake | RT-P1-BUS-WAKE | fleet_bus/wake*, keeper messages long-poll consumer, routing timer removal (routing repo part separate) | after 2 in phase |
| 4 | lane/bus-any-agent-cli | BUS-ANY-AGENT-CLI | tools/fleet-msg*, MCP tool, fleet_bus/client | after 1+2+3; needs relay :58690 |
| 5a-e | lane/rt-qc-accepts-{a..e} | RT-QC-ACCEPTS | A keeper accept/verdict (keeper/verdict*, accept*), B tests/test_interface_parity + design-rule tests, C gate log redaction, D .github hygiene job (Gate-Edit D-n), E phase status from keeper | parallel now; A touches keeper core -> after 2 merges if overlap |
| 6 | lane/exec-phase-lease | EXEC-PHASE-LEASE | rust/worker phase lease / keeper reaper | when #140 verdict lands |

File overlap with build-L1 (to confirm with build-L1): keeper core files in lane 2 + 5A (api_routes.py, store.py, schema*.py); #139 KEEPER-TRAILER-MULTI
and KEEPER-PG-RECONNECT may touch db.py/store.py -> rebase order: build-L1's keeper lanes merge to main first, phase merges main.
Held (close superseded with BUS-WAKE): BUS-WAKE-STOPGAP-2, BUS-WATCHER-TRACKED. Blocked: RT-P1-BUS-RUST (OCGO-BUS-INBOUND open).
Then: phase review (Haiku + qwen, D-2539/2540) -> fixer -> 1-seat re-review -> phase PR -> GO-REQ.
build-L1 file sets (18:5xZ): W27 spawner/{resolver,stall_watchdog}.py; #139 keeper trailer/evidence; #166 KEEPER-PG-RECONNECT keeper/db.py,store.py,schema_pg.py
(OVERLAPS lane 2); #164 keeper/api_routes.py (OVERLAPS lane 2); #171 keeper define/edit validation. Rule: earlier merge first, later lane merges main;
build-L1 tells me if #166/#164 land before BUS-KEEPER. Lanes 1, 3-6 file-disjoint. Freeze end only on build-L1's word.
D-2643: fix rounds = parallel fix writers by file group (affected tests + CI gate), then ONE review over the collected delta; no seat per writer/fixer.
Fence: #164 (build-L1) adds schema v14 oversight DDL to schema.py and schema_pg.py. Whoever lands first keeps its number; the phase renumbers its v14/v15 after #164 when it merges main.
