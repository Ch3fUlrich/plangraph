# FEEDER-RANKS-BY-GOAL-AND-CAPS-INDEPENDENT-OF-HARDWARE

Outcome. Feeder admission is bounded by configured limits that do not depend on hardware. The per-host policy cap binds below the RAM slots, and a landing backlog bound refuses new writers while too much work waits to land. An item family whose last three runs on any host were barren is skipped. Every decision reports category, cap and backlog. The fleet-wide writer cap and the hourly token budget need a shared reservation in the keeper and go to their own item (see Open points).

- plan_source. Keeper item plangraph:FEEDER-RANKS-BY-GOAL-AND-CAPS-INDEPENDENT-OF-HARDWARE, 2026-11-27 16:39Z, priority 10. Operator order 2026-11-27 ~16:55Z.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 1922a6851c6a41150c65e33f117a7f6ff3fbb47f05d26c1c1a1392a46b64ca66
- tree SHA-256. 4d4c13855a27326a0c36e377d2c33b850f096a3ce17fb3548e26af8713e0eec8
- evidence_after_deploy. After the CLI and policy deploy on the three hosts, record 24 h of feeder event rows. Every decision must carry `writer_cap_configured`, `writer_limit` and `landing_backlog`/`landing_backlog_max`. Start rows must carry `category`, and the `skipped` lists must name `barren_family:` entries. Count the refusals per new reason. Deployment is operator work under the feeder rollout GO.

## Checked against base

- `capacity::apply` overwrites the policy cap with `running + RAM slots` (`rust/plangraph-cli/src/feeder/capacity.rs:149-167`). So `[max_writers]` in `deploy/feeder/policy.toml` (coding 3, dell 5, workstation 8) never binds in live mode. The fixture `live_decision_uses_ram_when_load_is_unavailable` pins that a configured cap of 0 still admits (`runtime/live_tests.rs:339-356`).
- `estimate_tokens` is only copied into the card (`rust/plangraph-cli/src/feeder/mod.rs:240-243`). The quota store gives remaining percentages, not token counts (`feeder/quota.rs:31-47,155-201`). Keeper attempts are written only when an attempt ends (`src/plangraph/keeper/store_card_updates.py:41-43`), so they cannot count running writers or in-flight starts. The feeder lock is host-local (`feeder/runtime.rs:200-214`), so two hosts cannot share an allowance through it. A fleet cap or token budget enforced by the feeder alone would let two hosts spend the same last allowance.
- Nothing in the feeder reads the landing backlog. The keeper snapshot gives every item's `project` and `state`. At 2026-11-27 ~17:40Z it held 50 items in `in_review`. Today the feeder reads the snapshot only when a running writer has a plangraph or empty item key (`feeder/sources.rs:200-212`). The rival check reads a fresh snapshot per claimed member after the claim (`feeder/adapters.rs:265-291`). That read is a post-claim race check and must stay fresh.
- There is no barren stop on main. The per-host barren stop the item cites lives on an unmerged feeder fix lane that is not on origin. The feeder's item family is `selection::family` (project plus first key segment; `selection.rs:97-100`), already used for batching and overlap.
- Selection orders by class, then effective priority, then age (`selection.rs:200-228`). A ranked category field and its feeder order belong to plangraph:ITEM-CATEGORY-RANKED (open, p9), whose How (3) names `selection.rs`. The `needs_spec` queue belongs to the spec gate of plangraph:LE-ITEMS-ACCEPTED-AND-CLAIMED-WITHOUT-A-SPEC.
- The fleet facts module (ended keeper attempts), `refusals()`, `Fleet` and `KeeperFacts` come from plangraph:LE-FEEDER-LIVE-FAULTS-HIDDEN-BEHIND-KILL-SWITCH (spec on this branch). This card builds on that one.

## Decisions

- D1. Per-host cap. `capacity::apply` reads the configured cap first: `configured = writer_limit(host, policy)`. It writes `limit = min(configured, running + new_writers_allowed)` for a listed host. `Budget` gains `configured: Option<usize>`, reported as `writer_cap_configured`. The shipped caps stay 3, 5 and 8 and now bind. A configured cap of 0 refuses every new writer (`max_writers`).
- D2. Landing backlog. New policy field `landing_backlog_max`, default 60. `runtime::decision` reads one admission snapshot (`snapshot?fmt=json`) per decision, bounded by the work deadline of LE-FEEDER-LIVE-FAULTS. It passes that snapshot to `sources::runs`, which then no longer reads its own conditional copy, and to `attempts::backlog`. The rival check keeps its own fresh post-claim reads. The admission snapshot is never used for them. `backlog(snapshot)` counts items with `project == "plangraph"` and `state == "in_review"`. The refusal is `landing_backlog:<n>/<max>` when `n > max`. Starts do not change the backlog, so two hosts admitting at once spend no shared allowance and no reservation is needed. EXPERIMENT: 50 at 2026-11-27 ~17:40Z, so 60 allows the present backlog plus one round. Measure the backlog per decision and the refusals for 7 days and tune it in policy.
- D3. Barren families. `Fleet.barren_families` holds each `selection::family` whose last 3 ended `implement` attempts in the 1-day window, on any host, all have a non-`ok` exit class. Ended attempts are exactly what the keeper records. Before its loop, `workflow::tick` removes ready items of those families and lists them in `skipped` with reason `barren_family:<FAMILY>`. A family recovers when an `ok` attempt ends or its barren attempts leave the window. A race between hosts can at worst start one more run of a family that just turned barren, which the next tick then skips.
- D4. Order. `landing_backlog` comes after `canary` and before `max_writers` in `refusals()`.
- D5. Category. The category field and its sort are not built here. Start rows report the seed item's `category` field from the ready row (`null` when absent), so the 24 h log shows it once plangraph:ITEM-CATEGORY-RANKED lands.
- D6. Validation. `landing_backlog_max >= 1`, else `invalid_policy`.
- D7. Out of this card, pending the planner's scope split (Open points): the fleet-wide writer cap and the hourly token budget (How 2 second half, How 3), the category ranking (How 1) and the `needs_spec` counts by category (How 6).

## Entry points

- E1. `capacity::apply` and the capacity line.
- E2. `feeder::refusals` with the backlog bound, and `attempts::{reduce, backlog}` for the barren families and the backlog.
- E3. `workflow::tick` for the barren family filter and the category on rows.
- E4. `runtime::decision` and `sources::runs` sharing one admission snapshot.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | With RAM for 8 writers, 8 eligible disjoint items, a recent ok attempt on the host and a dell cap of 3, the live dry tick starts 3 and reports `slots_by_ram` 8, `writer_cap_configured` 3 and `writer_limit` 3. A configured cap of 0 refuses `max_writers` with `writer_limit` 0. | X1 X2 |
| B2 | E2 | `backlog()` counts only plangraph items in `in_review`, out of a snapshot of mixed projects and states. 61 refuse `landing_backlog:61/60`, and 60 admit. | X3 X4 |
| B3 | E2 | The reduction marks a family barren after 3 non-ok ended attempts on any hosts. A family with an `ok` after its barren runs is not barren. | X5 |
| B4 | E3 | A family with three barren runs on any hosts is skipped with its reason, and other families start. | X6 |
| B5 | E3 | Start rows carry the seed's `category`, or `null`. | X7 |
| B6 | E4 | One decision reads the admission snapshot once. A claim's rival check still reads its own snapshot. | X8 |

## Verification

Write X1-X8 first. On the base, X1 starts 8, X2 admits, and the others fail on the missing fields.

- X1. `runtime/live_tests.rs::ram_for_eight_and_a_cap_of_three_starts_three`. Use `Live::new("dell", "[max_writers]\ndell = 3\n")` and overwrite `proc/meminfo` with `MemAvailable: 35000000 kB`: (35.84 GB - 3 GB floor) / 4 GB gives 8 slots. Use eight items `plangraph:A1`..`plangraph:H1` (distinct families, no files). Pass `KeeperFacts` with `Fleet { known: true, host_ok_24h: 1, .. }` so the canary permits more than one. Assert B1 on the dry decision (`slots == 3`). Change `live_slots_follow_free_ram_above_the_floor_not_load` to insert `dell = 10` so RAM still decides there. Its assertions stay.
- X2. `runtime/live_tests.rs::live_decision_uses_ram_when_load_is_unavailable`, changed to the new zero-cap behaviour. With `dell = 0`, it asserts `slots_by_ram` 3, `writer_limit` 0 and `reason == "max_writers"`, and `load1`/`load_paced` as today. Rename it `live_decision_without_load_still_obeys_a_zero_cap`.
- X3. `feeder/attempts.rs` unit test `backlog_counts_plangraph_items_in_review_only`. A snapshot with plangraph `in_review` (2), plangraph `open`, `claimed`, `done` and another project's `in_review` gives 2.
- X4. `rust/plangraph-cli/tests/feeder.rs::landing_backlog_refuses_above_its_bound`. Use facts with `Fleet { known: true, landing_backlog: 61, .. }` and 60, and assert B2 through `refusals()`.
- X5. `feeder/attempts.rs` unit test `barren_families_need_three_non_ok_ended_attempts_in_a_row`. Use fixed `now` and row JSON across three hosts.
- X6. `rust/plangraph-cli/tests/feeder_slots.rs::a_family_with_three_barren_runs_is_skipped_on_every_host`. Use items `AAA-1` and `BBB-1` and `barren_families = {"PLANGRAPH:AAA"}`. Assert B4.
- X7. `rust/plangraph-cli/tests/feeder_slots.rs::start_rows_report_the_seed_category`. One item has `"category":"speed"` and one has none.
- X8. `runtime/live_tests.rs::one_admission_snapshot_per_decision`. Use the `Live` fixture with a counting `Transport` fake for the keeper in `decision()`, as injected by LE-FEEDER-LIVE-FAULTS's `KeeperFacts` read. Exactly one `snapshot` call precedes the tick, and `sources::runs` uses it.
- X9. Held out: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p plangraph-cli --test feeder_batches --test feeder_contracts`.

## Scope and reuse

- R1. Extend `Fleet`, `attempts::reduce` and `refusals()` from LE-FEEDER-LIVE-FAULTS-HIDDEN-BEHIND-KILL-SWITCH. Use no second attempts reader and no second admission function.
- R2. Use `selection::family` (made `pub(super)`) as the one family definition.
- R3. One admission snapshot per decision, shared by run decoding and the backlog count. The post-claim rival reads stay separate and fresh.
- N1. Not here (D7): the fleet-wide cap and hourly token budget, the keeper category field and its ranked sort (plangraph:ITEM-CATEGORY-RANKED), the `needs_spec` counts by category, and the unmerged per-host barren stop of the feeder fix lane.

### File budgets

Final line caps are in the card. Additions: `capacity.rs` <= 15, `mod.rs` <= 25, `attempts.rs` <= 70, `workflow.rs` <= 25, `runtime.rs` <= 20, `sources.rs` net <= 0, `selection.rs` <= 2, `runtime/live_tests.rs` <= 80, `tests/feeder.rs` <= 30, `tests/feeder_slots.rs` <= 60, `tests/feeder_support/mod.rs` <= 10, `deploy/feeder/policy.toml` <= 4, `docs/writer-feeder.md` <= 15. New: `changelog.d/<ID>.md` <= 6.

### Implementation

- `capacity.rs`: read `configured` before the insert, then use the min and the new `Budget` field. Add `writer_cap_configured`, `landing_backlog` and `landing_backlog_max` to `runtime::capacity_line`.
- `mod.rs`: the policy field with default and validation, the `Fleet` fields `landing_backlog: usize` and `barren_families: BTreeSet<String>`, and the check in `refusals`.
- `attempts.rs`: extend `reduce` with the families, and add `backlog(snapshot: &Value) -> usize`.
- `runtime.rs`: read the admission snapshot in `decision`, pass it to `sources::runs`, and put the backlog into `KeeperFacts.fleet`.
- `sources.rs`: `runs` takes `snapshot: &Value` in place of its conditional read.
- `workflow.rs`: the barren family filter after `prepare_ready`, and `category` on each start row.
- `policy.toml`: `landing_backlog_max = 60` with its experiment note.
- `docs/writer-feeder.md`: cap binding, backlog, barren families and the new decision fields.

## Open points for design approval

The planner recorded this scope split on the keeper item on 2026-11-27 18:40Z (plan-roadmap-0969; review r1 D2-D3, D6, D7); the fleet cap and token budget are now plangraph:FLEET-WRITER-CAP-AND-TOKEN-BUDGET-AT-THE-KEEPER-CLAIM:
- New item, keeper work: a fleet-wide writer cap and an hourly token budget enforced atomically at the keeper claim. The claim is refused with `fleet_cap` or `token_budget`, computed from the keeper's own claim and attempt records under the claim transaction. A host-local feeder cannot reserve a shared allowance (`feeder/runtime.rs:200-214`), and attempts exist only after a run ends (`store_card_updates.py:41-43`). The feeder then only reports the keeper's refusal code. Its test must have two hosts contend for the last allowance.
- Done points (1) "a speed item outranks an other item of equal priority" and (6) "needs_spec counts by category" move to plangraph:ITEM-CATEGORY-RANKED and to a follow-up of plangraph:LE-ITEMS-ACCEPTED-AND-CLAIMED-WITHOUT-A-SPEC.
- Add the keeper dependency edge from this item to plangraph:LE-FEEDER-LIVE-FAULTS-HIDDEN-BEHIND-KILL-SWITCH.
