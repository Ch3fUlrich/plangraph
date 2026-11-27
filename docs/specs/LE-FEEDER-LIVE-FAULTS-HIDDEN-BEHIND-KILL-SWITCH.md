# LE-FEEDER-LIVE-FAULTS-HIDDEN-BEHIND-KILL-SWITCH

Outcome. Every feeder decision lists all admission refusals in check order. A readback pass ignores `feeder.off` and runs every check short of claiming. A host with no recent successful run starts one canary writer at a time, a host whose recent runs end barren pauses itself, and a tick stops starting slots before the unit's start timeout.

- plan_source. Keeper item plangraph:LE-FEEDER-LIVE-FAULTS-HIDDEN-BEHIND-KILL-SWITCH, 2026-11-27, priority 10, D-7041, D-7042, D-7044, D-7047. The canary and the barren-rate stop come from the item's incident of 2026-11-27 10:58Z.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. b33e31c7c85bd1d3fb597c4f0281dbfad3cee2ea5d97a6cdb9f6be4ffcdf0c2d
- tree SHA-256. d9614001356d37526faba96a6cb00a2faa490a5dec486ab37a4c25904bff6bae
- evidence_after_deploy. After the CLI deploy on coding.vm, dell and the workstation: one `plangraph feed --once --live-facts --dry-run` per host with `feeder.off` present, whose `refusals` lists `kill_switch` and nothing else. One `--readback` per host, whose row ends in `would_spawn` or lists only real faults. Then 20 consecutive `pg-feeder` runs per host in the journal without `start operation timed out`. Deployment is operator work under the feeder rollout GO.

## Checked against base

- `admit()` returns the first failing check only, and `kill_switch` is checked first (`rust/plangraph-cli/src/feeder/mod.rs:156-200`). With `feeder.off` present, unknown host, quota and RAM faults stay hidden.
- `decide()` returns a stale quota refusal before `admit()` runs, so a stale quota also hides the kill switch and the host checks (`rust/plangraph-cli/src/feeder/runtime.rs:159-171`). With no eligible item it returns `idle` without any admission check (`runtime.rs:132-135,172-174`).
- Dry runs read Windows RAM from the cache only (`rust/plangraph-cli/src/feeder/capacity.rs:73-82` passes `dry`). A missing `powershell.exe` on the workstation therefore stayed invisible in dry runs.
- `kill_switch` is `~/.config/plangraph/feeder.off` (`rust/plangraph-cli/src/feeder/sources.rs:283-297`).
- The tick fills slots in one loop. Each slot does a claim, a rival snapshot and a spawner start, and every keeper call may take 20 s (`rust/plangraph-cli/src/feeder/workflow.rs:211-310`, `adapters.rs:93-95`). The unit has `TimeoutStartSec=110` and a timer every 2 minutes (`deploy/systemd/pg-feeder.service`, `pg-feeder.timer`). On 2026-11-27 dell and the workstation were killed at 110 s with a curl still running.
- `merge_close.rs::DeadlineKeeper::call_with` already bounds a keeper call by a deadline, rounding the remaining time up to whole curl seconds (`rust/plangraph-cli/src/merge_close.rs:290-325`).
- The keeper's `attempts` verb is open to worker principals (`src/plangraph/keeper/api_routes.py:139`). It returns fleet-wide rows with `item`, `role`, `host`, `started_at`, `ended_at` and `exit_class` (`src/plangraph/keeper/store_card_updates.py:73-110`). Measured over the 3 days before 2026-11-27 17:30Z: 314 attempts, of which 42 had a non-`ok` exit class (13.4%). Feeder no_commit runs appear as `worker_crash`.
- `whoami` returns `name` and `role` (`src/plangraph/keeper/store_read.py:111-115`). docs/writer-feeder.md requires "a dedicated feeder L1/worker principal token".

## Decisions

- D1. All reasons. Add `pub fn refusals(f: &HostFacts, p: &Policy) -> Vec<String>`. It evaluates every check in the base order and appends each failing reason: kill_switch, invalid_policy, stale_facts, unknown_host, cpu_pressure, then the new barren_rate and canary, then max_writers, ram, disk, provider_quota. `max_writers` is skipped when the host is unknown, because `unknown_host` already covers it. `admit()` returns `Refuse(first)` or `Admit`, so `reason` strings, exit codes and existing tests keep their meaning.
- D2. Every row that `decide()` returns carries `"refusals": [...]` (empty when admitted), computed once after the facts are final. In the stale quota case, the list is `refusals()` with `provider_quota` replaced by the full `quota_stale: ...` text. `reason` is the first entry, so with `feeder.off` present it is `kill_switch`. The tick's own refusal row carries the same list from `sim_facts`.
- D3. Readback. Add the flag `--readback` to `plangraph feed`. It implies `--dry-run` (no claim, no release, no spawn, no receipt pruning, no event log). It sets `kill_switch` to false for admission and adds `"kill_switch_ignored": true` when `feeder.off` exists. It reads Windows RAM live, so the dry cache-only mode is off for readback. It calls `whoami` and adds the refusal `keeper_principal:<name>:<role>` when the role is not `l1` or `worker`. docs/writer-feeder.md makes `plangraph feed --once --live-facts --readback` the deploy readback, run after installing the units and before removing `feeder.off`.
- D4. Fleet facts. A new module `feeder/attempts.rs` reads `attempts?fmt=json&since=1d` once per decision in `runtime::decision`, beside the ready read. It reduces the rows to `Fleet { known, error, host_recent: Vec<String>, host_ok_24h: usize }`. `host_recent` holds the exit classes of this host's ended `implement` attempts, newest first, at most `barren_window_runs`. Hosts match with `same_host`. `HostFacts` gains `#[serde(skip)] pub fleet: Fleet`. `Fleet::default()` has `known: false`, which skips D5 and D6, so facts files and existing tests keep their behaviour. A failed read sets `known: true` and `error`, and adds the refusal `attempts_unavailable` (fail closed, like a stale quota).
- D5. Barren-rate pause. When `host_recent` has at least `barren_pause_runs` entries that are not `ok`, refuse `barren_rate:<k>/<n>`. Policy defaults are `barren_window_runs = 6` and `barren_pause_runs = 4`, valid when `1 <= pause <= window <= 50` (else invalid_policy). EXPERIMENT: at the measured 13.4% non-ok rate, P(4 or more of 6) is about 0.4%, so false pauses are rare. The 10:21Z incident (34 of 34 barren) pauses after the fourth. The pause ends as the barren attempts leave the 1-day window or when new ok attempts arrive. Measure pauses per host per day in the feeder log for 7 days.
- D6. Canary. When `known` and `host_ok_24h == 0`, the host admits only while `running_writers == 0` and refuses `canary` otherwise. The tick's simulated start raises `running_writers`, so one tick starts one canary writer.
- D7. Tick deadline. Add `pub const TICK_DEADLINE_SECS: u64 = 100` (the unit's 110 s less 10 s to write the event row) and `SLOT_RESERVE_SECS = 45` (two 20 s keeper calls plus 5 s spawner start). `runtime::run` takes `Instant::now()` at entry. The tick's `Adapters` hold `deadline = start + TICK_DEADLINE`. The `Launch` trait gains `fn time_left(&self) -> bool { true }`, and `Adapters` returns `deadline - now >= SLOT_RESERVE`. Before claiming the next batch (live only), the tick stops with `{"decision":"idle","reason":"tick_deadline"}` when no time is left, keeping the rows already started.
- D8. Bounded calls. `adapters::Keeper` gains `deadline: Option<Instant>`. Its `call` limits curl to `min(20, remaining seconds rounded up)` and returns `Err("tick_deadline")` without starting curl when no time remains. The rounding moves into `adapters::request_limit(deadline, now, cap_secs) -> Option<u32>`, and `merge_close::DeadlineKeeper::call_with` uses it: one implementation. Every other `Keeper { env }` gets `deadline: None`.

## Entry points

- E1. `feeder::refusals` and `admit`.
- E2. `runtime::decide`, `runtime::decision` and the `--readback` option.
- E3. `feeder::attempts` (the fleet facts) and the D5 and D6 checks.
- E4. `workflow::tick` with `Launch::time_left`, and `adapters::Keeper` with `request_limit`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | Facts with kill_switch, an unknown host and no quota give `refusals == ["kill_switch","unknown_host:<h>","provider_quota"]` and `admit == Refuse("kill_switch")`. | X1 |
| B2 | E2 | A live dry decision with `feeder.off`, an unlisted host and a stale center quota lists `kill_switch`, `unknown_host:<h>` and the `quota_stale:` text in that order. Its `reason` is `kill_switch`. An idle decision also carries `refusals`. | X2 |
| B3 | E2 | Readback with `feeder.off` present ends in `would_spawn` with `kill_switch_ignored: true`. It reads Windows RAM live and lists a `keeper_principal` refusal for a non-l1/worker role. | X3 |
| B4 | E3 | Four non-ok of the last six host attempts refuse `barren_rate:4/6`, and three do not. A host with no ok attempt in 24 h admits one writer and refuses the second with `canary`. An unreadable attempts list refuses `attempts_unavailable`. | X4 X5 |
| B5 | E4 | A tick whose time runs out after one start stops with `tick_deadline`, keeps the start and claims nothing more. | X6 |
| B6 | E4 | A keeper call after the deadline fails with `tick_deadline` without curl. 3.2 s left gives a 4 s limit, and 60 s left gives 20 s. The unit's `TimeoutStartSec` is at least `TICK_DEADLINE_SECS + 10`. | X7 X8 |

## Verification

Write X1-X8 first. On the base they fail on the missing symbols, or on `refusals` absent from the row.

- X1. `rust/plangraph-cli/tests/feeder.rs::admit_lists_every_refusal_in_check_order`. Also add `fleet: Fleet::default()` to `facts()` there. Keep `admit_all_refusal_reasons_and_boundaries` unchanged.
- X2. `runtime/live_tests.rs::kill_switch_unknown_host_and_stale_quota_are_listed_together`. Use the `Live` fixture on an unlisted host and write `.config/plangraph/feeder.off` under the fixture home. Write a stale center quota as in `center_quota_drives_live_admission_and_preserves_capacity_when_stale`. Assert B2, and assert `refusals` on an idle decision with no eligible item.
- X3. `runtime/live_tests.rs::readback_ignores_the_kill_switch_and_checks_the_principal`. `Options` gains `readback: true`. Inject the whoami result through the `KeeperFacts` value that `decide` now takes (D4: `decision()` reads attempts and whoami and passes `KeeperFacts { fleet, principal }`). Assert B3 for roles `worker` and `router`.
- X4. `feeder/attempts.rs` unit tests `host_attempts_reduce_to_recent_classes_and_ok_count` and `barren_rate_and_canary_refusals_follow_the_host_history`. Build row JSON with mixed hosts (`coding-vm`, `coding.vm`, `dell`), roles and open attempts. Assert the reduction and the B4 refusals through `refusals()`.
- X5. `feeder/attempts.rs` unit test `an_unreadable_attempt_list_refuses_admission`. Use a `Transport` fake returning an error.
- X6. `rust/plangraph-cli/tests/feeder_slots.rs::a_tick_without_time_for_another_slot_stops_and_keeps_its_start`. `feeder_support::Host` gains `time_left_after: Option<usize>`. Its `time_left` is false once that many runs started. With two eligible items and `Some(1)`: one start, `stopped == "tick_deadline"`, and only the first item claimed.
- X7. `adapters.rs` unit test `request_limit_rounds_up_and_refuses_after_the_deadline`. Also test `Keeper` with a past deadline and no `PG_URL`: the error is `tick_deadline`, not `PG_URL missing`. The existing `merge_close` tests stay green unchanged.
- X8. `rust/plangraph-cli/tests/feeder_transport.rs::unit_timeout_leaves_room_after_the_tick_deadline`. Parse `TimeoutStartSec` from the included unit and assert it is at least `TICK_DEADLINE_SECS + 10`.
- X9. Held out: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p plangraph-cli --test feeder --test feeder_batches --test feeder_contracts --test merge_close`.

## Scope and reuse

- R1. One admission function. `admit` is the first entry of `refusals`, with no second rule set.
- R2. One deadline arithmetic (`request_limit`), shared by the feeder keeper and merge-close.
- R3. The fleet facts module is the single reader of keeper attempts for the feeder. FEEDER-RANKS-BY-GOAL-AND-CAPS-INDEPENDENT-OF-HARDWARE extends `Fleet`. It adds no second reader.
- N1. No change to the keeper, to the units' timeout or timer, or to the spawner. Deployment and the 20-run journal check are operator work.

### File budgets

Final line caps are in the card. Additions: `mod.rs` <= 70, `runtime.rs` <= 60, `workflow.rs` <= 15, `adapters.rs` <= 45, `merge_close.rs` <= 10 (net <= 0), `capacity.rs` <= 3, `sources.rs` <= 6, `runtime/live_tests.rs` <= 90, `tests/feeder.rs` <= 35, `tests/feeder_slots.rs` <= 25, `tests/feeder_support/mod.rs` <= 10, `tests/feeder_transport.rs` <= 15, `deploy/feeder/policy.toml` <= 3, `docs/writer-feeder.md` <= 20. New: `feeder/attempts.rs` <= 220 (with tests), `changelog.d/<ID>.md` <= 6.

### Implementation

- `mod.rs`: `Fleet` (Default, Clone, Debug), the two policy fields and their validation inside the invalid_policy check, `refusals`, `admit` via `refusals`, and `pub const TICK_DEADLINE_SECS`.
- `attempts.rs`: `read(keeper: &impl Transport, host: &str, policy: &Policy) -> Fleet` and the pure `reduce(rows: &Value, host: &str, policy: &Policy, now: i64) -> Fleet`. Unparseable rows are skipped. A non-array body is an error.
- `runtime.rs`: the `--readback` option and usage line, `KeeperFacts`, a `decide` parameter, `refusals` on every row, `kill_switch_ignored`, and the deadline handed to `Adapters`. `capacity::sample` gets `dry && !readback`.
- `workflow.rs`: the `time_left` check before `claim_batch` in the live branch, and `refusals` on the refusal row.
- `capacity.rs` and `sources.rs`: add `fleet: Fleet::default()` to the `HostFacts` literals and `deadline: None` to `Keeper` literals.
- `policy.toml`: `barren_window_runs = 6` and `barren_pause_runs = 4` with a comment naming the experiment.
- `docs/writer-feeder.md`: refusal lists, readback in the deploy steps, canary, barren pause and tick deadline.

## Open points for design approval

None.
