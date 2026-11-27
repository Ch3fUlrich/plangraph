# FEEDER-RANKS-BY-GOAL-AND-CAPS-INDEPENDENT-OF-HARDWARE

Outcome. Feeder admission is bounded by configured caps that do not depend on hardware: the per-host policy cap binds below the RAM slots, and there are a fleet-wide writer cap, an hourly token budget and a landing backlog bound. An item family whose last three runs on any host were barren is skipped. Every decision reports category, caps, budget and backlog.

- plan_source. Keeper item plangraph:FEEDER-RANKS-BY-GOAL-AND-CAPS-INDEPENDENT-OF-HARDWARE, 2026-11-27 16:39Z, priority 10. Operator order 2026-11-27 ~16:55Z.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 74a552df4eca520821b71a7a3a0aefea28dc1d3b5f607c25bbe70465f62cbe2e
- tree SHA-256. 6dc666c8218741c0170d94cc48ea1891d90a960e8eeb0bbcad623f761fb02f19
- evidence_after_deploy. After the CLI and policy deploy on the three hosts, record 24 h of feeder event rows. Every decision must carry `writer_cap_configured`, `writer_limit`, `fleet_running`/`max_writers_total`, `started_last_hour`/`tokens_per_hour_max`, `landing_backlog`/`landing_backlog_max` and, on start rows, `category`. Count the refusals per new reason. Deployment is operator work under the feeder rollout GO.

## Checked against base

- `capacity::apply` overwrites the policy cap with `running + RAM slots` (`rust/plangraph-cli/src/feeder/capacity.rs:149-167`). So `[max_writers]` in `deploy/feeder/policy.toml` (coding 3, dell 5, workstation 8) never binds in live mode. There is no fleet-wide cap.
- `estimate_tokens` is only copied into the card (`rust/plangraph-cli/src/feeder/mod.rs:240-243`) and is never budgeted. The quota store gives remaining percentages per window, not token counts (`rust/plangraph-cli/src/feeder/quota.rs:31-47,155-201`). Keeper attempts carry `tokens_in`/`tokens_out`, but all 314 attempts of the 3 days before 2026-11-27 17:30Z have them null.
- Nothing in the feeder reads the landing backlog. The keeper snapshot gives every item's `project` and `state`. At 2026-11-27 ~17:40Z it held 50 items in `in_review`. The feeder already reads the snapshot when writers run (`rust/plangraph-cli/src/feeder/sources.rs:200-212`).
- There is no barren stop on main. The per-host barren stop the item cites lives on an unmerged feeder fix lane that is not on origin. The feeder's item family is `selection::family` (project plus first key segment; `selection.rs:97-100`), already used for batching and overlap.
- Selection orders by class, then effective priority, then age (`selection.rs:200-228`). A ranked category field and its feeder order belong to plangraph:ITEM-CATEGORY-RANKED (open, p9), whose How (3) names `selection.rs`. The `needs_spec` queue belongs to the spec gate of plangraph:LE-ITEMS-ACCEPTED-AND-CLAIMED-WITHOUT-A-SPEC.
- The fleet facts module, `refusals()` and `Fleet` come from plangraph:LE-FEEDER-LIVE-FAULTS-HIDDEN-BEHIND-KILL-SWITCH (spec on this branch). This card builds on that one.

## Decisions

- D1. Per-host cap. `capacity::apply` reads the configured cap first: `configured = writer_limit(host, policy)`. It writes `limit = min(configured, running + new_writers_allowed)` for a listed host. `Budget` gains `configured: Option<usize>`, reported as `writer_cap_configured`. The shipped caps stay 3, 5 and 8 and now bind.
- D2. Fleet-wide cap. New policy field `max_writers_total`, default 16 (the sum of the shipped per-host caps). `Fleet.fleet_running` counts keeper attempts with role `implement`, no `ended_at` and `started_at` within the 1-day read window, on any host. The refusal is `max_writers_total:<running>/<cap>` when `running >= cap`.
- D3. Hourly token budget. New policy field `tokens_per_hour_max`, default 2,000,000. `Fleet.started_last_hour` counts `implement` attempts started in the last 3,600 s on any host. The refusal is `token_budget:<used>/<max>` when `(started_last_hour + 1) * estimate_tokens > tokens_per_hour_max`, with `used = started_last_hour * estimate_tokens`. The budget uses the feeder's own estimate because the store holds no token counts. EXPERIMENT: 20 runs of 100,000 estimated tokens per hour, against a measured 3-day average of 4.4 attempts per hour and the 10:21Z incident rate of about 200 per hour. Measure refusals and the hourly start count for 7 days and tune it in policy.
- D4. Landing backlog. New policy field `landing_backlog_max`, default 60. `runtime::decision` reads `snapshot?fmt=json` once and passes it to `sources::runs`, which no longer reads it itself. `Fleet.landing_backlog` counts snapshot items with `project == "plangraph"` and `state == "in_review"`. The refusal is `landing_backlog:<n>/<max>` when `n > max`. EXPERIMENT: 50 at 2026-11-27 ~17:40Z, so 60 allows the present backlog plus one round. Measure the same way as D3.
- D5. Barren families. `Fleet.barren_families` holds each `selection::family` whose last 3 ended `implement` attempts in the 1-day window, on any host, all have a non-`ok` exit class. Before its loop, `workflow::tick` removes ready items of those families and lists them in `skipped` with reason `barren_family:<FAMILY>`. A family recovers when an `ok` attempt ends or its barren attempts leave the window.
- D6. Order. The new refusals come after `canary` and before `max_writers`: `max_writers_total`, `token_budget`, `landing_backlog`. Each simulated start in the tick raises `fleet_running` and `started_last_hour` by one, like `running_writers`.
- D7. Category. The category field and its sort are not built here. Start rows report the seed item's `category` field from the ready row (`null` when absent), so the 24 h log shows it once plangraph:ITEM-CATEGORY-RANKED lands. The item's done test "a speed item outranks an other item of equal priority" is that item's test.
- D8. Validation. `max_writers_total >= 1`, `tokens_per_hour_max >= estimate_tokens` and `landing_backlog_max >= 1`, else `invalid_policy`.

## Entry points

- E1. `capacity::apply` and the capacity line.
- E2. `feeder::refusals` with the fleet caps, and `feeder/attempts.rs::reduce` with the new fleet fields.
- E3. `workflow::tick` for the barren family filter, the simulated fleet counts and the category on rows.
- E4. `runtime::decision` and `sources::runs` sharing one snapshot read.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | With RAM for 8 writers and a dell cap of 3, the live dry tick starts 3 and reports `slots_by_ram` 8, `writer_cap_configured` 3 and `writer_limit` 3. | X1 |
| B2 | E2 | 16 running of 16 refuse `max_writers_total:16/16`, and 15 admit. 20 starts in the hour refuse `token_budget:2000000/2000000`, and 19 admit. Backlog 61 refuses `landing_backlog:61/60`, and 60 admits. | X2 |
| B3 | E2 | The reduction counts open implement attempts, hourly starts and barren families. A family with an `ok` after its barren runs is not barren. | X3 |
| B4 | E3 | A family with three barren runs on any hosts is skipped with its reason, and other families start. | X4 |
| B5 | E3 | With 15 running fleet-wide and a cap of 16, one tick starts one batch and stops on `max_writers_total`. | X5 |
| B6 | E3 | Start rows carry the seed's `category`, or `null`. | X6 |

## Verification

Write X1-X6 first. On the base, X1 starts 8 and the others fail on the missing fields.

- X1. `runtime/live_tests.rs::ram_for_eight_and_a_cap_of_three_starts_three`. Use `Live::new("dell", "[max_writers]\ndell = 3\n")`, overwrite `proc/meminfo` with `MemAvailable: 35000000 kB` and give five items with distinct families and no shared files. Assert B1 on the dry decision (`slots == 3`). Change `live_slots_follow_free_ram_above_the_floor_not_load` to insert `dell = 10` so RAM still decides there. Its assertions stay.
- X2. `rust/plangraph-cli/tests/feeder.rs::fleet_caps_refuse_with_their_reasons_at_their_bounds`. Use facts with `Fleet { known: true, .. }` and assert B2 through `refusals()`.
- X3. `feeder/attempts.rs` unit test `fleet_counts_running_hourly_starts_and_barren_families`. Use fixed `now` and row JSON across three hosts.
- X4. `rust/plangraph-cli/tests/feeder_slots.rs::a_family_with_three_barren_runs_is_skipped_on_every_host`. Use items `AAA-1` and `BBB-1` and `barren_families = {"PLANGRAPH:AAA"}`. Assert B4.
- X5. `rust/plangraph-cli/tests/feeder_slots.rs::each_start_counts_against_the_fleet_cap`. Assert B5 with the policy `max_writers_total = 16` and `fleet_running = 15`.
- X6. `rust/plangraph-cli/tests/feeder_slots.rs::start_rows_report_the_seed_category`. One item has `"category":"speed"` and one has none.
- X7. Held out: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p plangraph-cli --test feeder --test feeder_batches --test feeder_contracts`.

## Scope and reuse

- R1. Extend `Fleet`, `attempts::reduce` and `refusals()` from LE-FEEDER-LIVE-FAULTS-HIDDEN-BEHIND-KILL-SWITCH. Use no second attempts reader and no second admission function.
- R2. Use `selection::family` (made `pub(super)`) as the one family definition.
- R3. One snapshot read per decision, shared by run decoding and the backlog count.
- N1. Not here: the keeper category field and its ranked sort (plangraph:ITEM-CATEGORY-RANKED), `needs_spec` counts by category (after plangraph:LE-ITEMS-ACCEPTED-AND-CLAIMED-WITHOUT-A-SPEC and ITEM-CATEGORY-RANKED), and the unmerged per-host barren stop of the feeder fix lane.

### File budgets

Final line caps are in the card. Additions: `capacity.rs` <= 15, `mod.rs` <= 40, `attempts.rs` <= 70, `workflow.rs` <= 30, `runtime.rs` <= 25, `sources.rs` net <= 0, `selection.rs` <= 2, `runtime/live_tests.rs` <= 45, `tests/feeder.rs` <= 50, `tests/feeder_slots.rs` <= 90, `tests/feeder_support/mod.rs` <= 10, `deploy/feeder/policy.toml` <= 8, `docs/writer-feeder.md` <= 20. New: `changelog.d/<ID>.md` <= 6.

### Implementation

- `capacity.rs`: read `configured` before the insert, then use the min and the new `Budget` field. Add `writer_cap_configured` and the fleet values to `runtime::capacity_line`.
- `mod.rs`: the three policy fields with defaults and validation, the `Fleet` fields `fleet_running`, `started_last_hour`, `landing_backlog` and `barren_families: BTreeSet<String>`, and the three checks in `refusals`.
- `attempts.rs`: extend `reduce` with the counts and families. A `backlog(snapshot: &Value) -> usize` helper serves D4.
- `runtime.rs`: read the snapshot in `decision`, pass it to `sources::runs` and the backlog count into `KeeperFacts.fleet`.
- `workflow.rs`: the barren family filter after `prepare_ready`, the simulated fleet increments, and `category` on each start row.
- `policy.toml`: `max_writers_total = 16`, `tokens_per_hour_max = 2000000` and `landing_backlog_max = 60`, each with its experiment note.
- `docs/writer-feeder.md`: caps, budget, backlog, barren families and the new decision fields.

## Open points for design approval

None for the writer. Planner follow-ups: add keeper dependency edges from this item to plangraph:LE-FEEDER-LIVE-FAULTS-HIDDEN-BEHIND-KILL-SWITCH and to plangraph:ITEM-CATEGORY-RANKED (done test "speed outranks other"), and move done point (6) to a new item after the spec gate lands.
