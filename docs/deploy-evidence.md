# Deployment evidence and the GO gate

Merged work is not live until a host runs it. `plangraph deploy-evidence` reports the work that is
merged but not deployed and gives the router a gate input for GO.

## Inventory

The keeper's `close_merged_auto` dry run reads the landed changes of every item, whether or not the close
is allowed (a sensitive card, a missing seat, an open lane, a commit without a PR and a delivery by
`Merge-Repo:` or `Plan-Item-Part` do not hide a unit). It returns two lists:

- `deployment_pending`: items whose landed work ships a unit or binary (`deploy/systemd/*`, a new
  `rust/*/src/main.rs` or `bin/*.rs`) with no verified `deploy` evidence, or whose `Live:` criterion has none.
  A unit or binary deleted by the head is retired and is not a target.
- `deployment_expected`: items whose targets all have evidence; each row names its `expected` targets.

Rows have `key`, `state`, `missing`, `expected`, `landed` (the landing commits), `merged_at` and `age_hours`.
The age counts from the first landing that touched the missing unit (the item's first landing for a `Live:`
clause), not from an older docs merge; one unreadable landing time makes it unknown.

- Open, claimed and in-review items are always covered.
- Completed (`done`) items are covered by the request field `inventory: true` (a dry run; it closes
  nothing). The keeper setting `deploy_inventory_done` (default `false`) puts them into the merge-index
  warm-up; with it off an `inventory` request is refused `validation`, and the `projects` reply says
  `inventory_done: false`, so an empty project list is never taken for an empty scan.
- Only deploy evidence of the item's current epoch recorded after its last landing (tagged, named by
  `Merge-Repo:`, or a part) clears a unit or a `Live:` clause.
- A unit that starts after its item closed is recorded with the router verb `add_evidence_router` (kind
  `deploy`), which also takes a completed item and no other kind of proof: the key lock (SQLite triggers;
  Postgres migration 28) lets that one evidence row and its `evidence_added` event through on a closed item,
  never on a cancelled one and never with a missing or null kind.

## Report

`plangraph deploy-evidence report --probe PROGRAM --repo CHECKOUT [--project NAME] [--no-done] [--rows FILE]`
prints the JSON report `plangraph.deploy-evidence/1`. For every expected `host:unit` target, receipted or not, it
runs `PROGRAM HOST TARGET`; the first word printed is the state (`active` for a unit, `installed` for a
`bin:NAME` target; a oneshot unit answers for its timer) and an optional second word the commit the build was
made from. `--repo` is the checkout whose history decides whether that commit contains every landing
(`git merge-base --is-ancestor`); without it no revision is believed. Per target:

| status | meaning | blocks |
|---|---|---|
| `verified` | the keeper holds evidence, the host runs it and the build contains every landing | no |
| `revision_unproven` | the keeper holds evidence and the host runs it, but the build is older or unnamed | yes |
| `unit_inactive`, `binary_missing` | the host does not run it (also after a receipt: the router's word is not enough) | yes |
| `unrecorded` | the host runs it, but the keeper holds no evidence | yes |
| `host_unverified` | the probe failed | yes |
| `live_proof_missing` | a `Live:` clause with no deploy proof | yes |
| `hosts_unnamed` | the item names no `Deploy-Hosts:` | yes |
| `scan_unverified` | the keeper could not scan the item (an inventory refusal, or an open-item refusal that names the index, the default ref or `Merge-Repo:`) | yes |

A row is `pending` while any of its targets blocks, else `verified`. `--rows FILE` reads keeper rows from a
file instead of the keeper (fixtures).

## Gate

`plangraph deploy-evidence gate --report FILE [--exceptions FILE] [--now EPOCH] [--max-age-hours N]`
prints a JSON decision and exits 0 (GO), 1 (blocked) or 2 (unreadable or inconsistent input: a wrong schema, no
`rows` list, a `pending` count that disagrees with the pending rows, a row without a key, targets that disagree
with the row status, a `merged_at` that is not text or null, or a timestamp out of range). A pending row older
than 24 hours, or of unknown age (a merge time ahead of the clock beyond five minutes included), blocks; the age
is counted from `merged_at` when the gate runs, so a saved report ages, and a report generated more than an hour
before the gate runs is refused (exit 2). A younger row is listed under `waiting`.

A routing exception is one entry of the exceptions file:
`{"key": "proj:KEY", "decision": "D-1234", "granted": "2026-11-24", "expires": "2026-11-30", "reason": "..."}`.
It covers its own key only, needs a `D-` decision id and a reason, must have begun and not ended (UTC
dates), and may run at most 14 days. An entry that fails any rule is listed under `invalid_exceptions`
and the row still blocks.

The router runs `report`, then `gate`, before it writes a GO row; the wiring in the router checkout is
a separate slice.
