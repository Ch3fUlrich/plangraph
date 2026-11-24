# Deployment evidence and the GO gate

Merged work is not live until a host runs it. `plangraph deploy-evidence` reports the work that is
merged but not deployed and gives the router a gate input for GO.

## Inventory

The keeper's `close_merged_auto` dry run lists `deployment_pending` rows: items whose merged work
ships a unit or binary (`deploy/systemd/*`, a new `rust/*/src/main.rs` or `bin/*.rs`) with no
verified `deploy` evidence, or whose `Live:` done criterion has no verified deploy evidence after the
merge. Each row has `key`, `state`, `missing`, `merged_at` and `age_hours`.

- Open, claimed and in-review items are always covered.
- Completed (`done`) items are covered by the request field `inventory: true` (a dry run; it closes
  nothing). The keeper setting `deploy_inventory_done` (default `false`) puts them into the merge-index
  warm-up; with it off an `inventory` request is refused `validation`.

## Report

`plangraph deploy-evidence report --probe PROGRAM [--project NAME] [--no-done] [--rows FILE]` prints the
JSON report `plangraph.deploy-evidence/1`. It runs `PROGRAM HOST TARGET` for every `host:unit` pair and
reads the first word printed (`active` for a unit, `installed` for a `bin:NAME` target; a oneshot unit
answers for its timer). Per target:

| status | meaning | blocks |
|---|---|---|
| `unit_inactive`, `binary_missing` | the host does not run it | yes |
| `host_unverified` | the probe failed; the host is not assumed deployed | yes |
| `live_proof_missing` | a `Live:` clause with no deploy proof | yes |
| `hosts_unnamed` | the item names no `Deploy-Hosts:` | yes |
| `live_unrecorded` | the host runs it, the keeper holds no proof | no |

`--rows FILE` reads keeper rows from a file instead of the keeper (fixtures).

## Gate

`plangraph deploy-evidence gate --report FILE [--exceptions FILE] [--now EPOCH] [--max-age-hours N]`
prints a JSON decision and exits 0 (GO), 1 (blocked) or 2 (unreadable input). A pending row older than
24 hours, or of unknown age, blocks. A younger row is listed under `waiting`.

A routing exception is one entry of the exceptions file:
`{"key": "proj:KEY", "decision": "D-1234", "granted": "2026-11-24", "expires": "2026-11-30", "reason": "..."}`.
It covers its own key only, needs a `D-` decision id and a reason, must have begun and not ended (UTC
dates), and may run at most 14 days. An entry that fails any rule is listed under `invalid_exceptions`
and the row still blocks.

The router runs `report`, then `gate`, before it writes a GO row; the wiring in the router checkout is
a separate slice.
