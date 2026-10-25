# WRITER brief: plangraph:BUS-REGISTRY-PERSIST (HIGH, D-2670/D-2671) · lane/bus-registry-persist · base phase/rt-p1-bus e30b2ae3
Load skill unattended-orchestration (L3 worker rules). Leaf: never spawn. Commit early. Report, then stop (D-2442).
Goal: the keeper persists the host registry in table bus_hosts. The in-keeper Registry loop writes it, GET hosts reads it, and it survives a keeper restart.
Done: the table exists in SQLite and PG (parity), plus a restart-survival test. Red test first, then the affected tests green on SQLite AND PG (tests/test_keeper_pg*.py style; PG16 via PLANGRAPH_TEST_PG_DSN if set, else mark skip with a reason).

## Facts (verified at e30b2ae3)
- src/plangraph/fleet_bus/discovery.py: Host(host,node_id,key_id,key_file,endpoint,last_seen,health). Registry.refresh() mutates the hosts; Registry.run(stop, probe, interval, discover, clock) loops refresh.
- src/plangraph/keeper/api.py ~L464: the server builds a Registry from env PLANGRAPH_BUS_INVENTORY and runs Registry.run in thread "bus-registry". The `except Exception: self.bus_registry = None` there swallows errors silently.
- api.py _read name=="hosts" ~L236 serves host/endpoint/key_id/health/last_seen from memory.
- Schema: messages_schema.py (SQLITE/PG strings) is part of SQLite SCHEMA_VERSION 15 (schema.py) and PG Migration(8) (schema_pg.py). v15 and PG 8 are NOT on main yet, so edit them IN PLACE. Do not add a new version.
- copy_to_pg.py copies SQLite to PG and verifies the message chain.

## Do
1. Put DDL `bus_hosts(host TEXT PRIMARY KEY, node_id TEXT NOT NULL, key_id TEXT NOT NULL, endpoint TEXT, health TEXT NOT NULL CHECK (health IN ('offline','unhealthy','healthy')), last_seen DOUBLE PRECISION/REAL, updated_ts BIGINT NOT NULL)` in messages_schema.py, in both the SQLITE and PG strings. Never store key_file or any key path.
2. Add a store method, upsert_bus_hosts(rows), in store_messages.py (or a new store_bus_hosts.py), plus list_bus_hosts(). Use backend-neutral SQL through the Store write context: no raw BEGIN/COMMIT/PRAGMA (that broke PG in G1).
3. Registry persistence hook:
   - Add an optional `on_refresh(hosts)` callback to Registry.run. It is called after each refresh, and an exception in it is caught and logged, never fatal.
   - The keeper passes a callback that upserts the inventory hosts.
   - At start-up, before the first probe, load the persisted rows into the Host objects' endpoint/last_seen/health. On load, health is forced to 'offline' unless the next probe confirms it: resolve() must never trust a pre-restart 'healthy'. The persisted last_seen is kept as is.
4. GET hosts reads list_bus_hosts() joined with the current inventory (hosts not in the inventory are not served). Keep the current fields, and add `persisted: true`.
5. Replace the silent `except Exception` around the Registry setup with logging the error class + message. The fail-closed behaviour stays.
6. copy_to_pg: copy bus_hosts. A missing source table = skip (same rule as G1-8).
7. Tests:
   - upsert/list on both backends;
   - restart survival: start the real keeper server with an inventory, run one refresh with a fake discover/probe, stop the server, start a new server on the same DB, and check GET hosts returns the host with its last_seen and health 'offline' before the probe;
   - the callback's error does not kill the loop;
   - no key_file appears in the table or the response.
8. docs/FLEET-BUS.md: a short "host registry persistence" paragraph.

## Do not
- Touch .github, fleet_bus/client.py, tools/fleet-msg*, wake*, or exec files (those are other lanes).
- Use a site IP literal in code or tests.
- Print the environment or any token.

## Finish
- Push lane/bus-registry-persist (if the clone cannot push, leave commits on the branch).
- Commit trailers: `Plan-Item: plangraph:BUS-REGISTRY-PERSIST` and `Writer: <provider/model> <run id>`.
- Report: commits, the tests run with their pass counts per backend, and anything not done, as `WONTFIX <id>: reason`.
- Output budget: max_tokens 128000 (D-2694).
