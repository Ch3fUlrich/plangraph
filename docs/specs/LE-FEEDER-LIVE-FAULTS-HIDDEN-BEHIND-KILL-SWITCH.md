# LE-FEEDER-LIVE-FAULTS-HIDDEN-BEHIND-KILL-SWITCH

Outcome. Every feeder decision lists all admission refusals in check order. A readback pass ignores `feeder.off` and runs every check short of claiming. A host with no recent successful run starts one canary writer at a time, a host whose recent runs end barren pauses itself, and a tick stops starting slots before the unit's start timeout.

- plan_source. Keeper item plangraph:LE-FEEDER-LIVE-FAULTS-HIDDEN-BEHIND-KILL-SWITCH, 2026-11-27, priority 10, D-7041, D-7042, D-7044, D-7047. The canary and the barren-rate stop come from the item's incident of 2026-11-27 10:58Z.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 74925df2ee4906c440d901f10ba6e2149a5b4ceb1834c19900ac8e42439d1d5c
- tree SHA-256. bb3d21e73cc20b206d6fbab87aeef8865b72ef23a371e720447b4663c2df6b32
- evidence_after_deploy. After the CLI deploy on coding.vm, dell and the workstation: one `plangraph feed --once --live-facts --dry-run` per host with `feeder.off` present, whose `refusals` lists `kill_switch` and nothing else. One `--readback` per host, whose row ends in `would_spawn` or lists only real faults. Then 20 consecutive `pg-feeder` runs per host in the journal without `start operation timed out`. Deployment is operator work under the feeder rollout GO.

## Checked against base

- `admit()` returns the first failing check only, and `kill_switch` is checked first (`rust/plangraph-cli/src/feeder/mod.rs:156-200`). With `feeder.off` present, unknown host, quota and RAM faults stay hidden.
- `decide()` returns a stale quota refusal before `admit()` runs, so a stale quota also hides the kill switch and the host checks (`rust/plangraph-cli/src/feeder/runtime.rs:159-171`). With no eligible item it returns `idle` without any admission check (`runtime.rs:132-135,172-174`).
- Dry runs already probe Windows RAM live through PowerShell. `dry` only suppresses the cache write (`rust/plangraph-cli/src/feeder/windows_ram.rs:59-94`, called from `capacity.rs:73-82`). A failed live probe falls back silently to a cached value up to `facts_max_age_seconds` old (source `cached`), so a missing `powershell.exe` stays invisible while a fresh cache exists.
- `kill_switch` is `~/.config/plangraph/feeder.off` (`rust/plangraph-cli/src/feeder/sources.rs:283-297`).
- The tick fills slots in one loop. Each batch claims every member, reads a rival snapshot per member, and releases every held member on a later failure. Then the spawner starts, and every keeper call may take 20 s (`rust/plangraph-cli/src/feeder/workflow.rs:35-103,211-310`, `adapters.rs:93-95`). The spawner start (`adapters.rs:323-346`) and the Python adapters for `ps` and quota (`adapters.rs:23-41`) are not bounded at all. So no fixed number of keeper calls bounds a slot. The unit has `TimeoutStartSec=110` and a timer every 2 minutes (`deploy/systemd/pg-feeder.service`, `pg-feeder.timer`). On 2026-11-27 dell and the workstation were killed at 110 s with a curl still running.
- `merge_close.rs::DeadlineKeeper::call_with` already bounds a keeper call by a deadline, rounding the remaining time up to whole curl seconds (`rust/plangraph-cli/src/merge_close.rs:290-325`).
- The keeper serves `GET attempts` at `src/plangraph/keeper/api.py:398-399`. Rows are written only when an attempt ends, since `ended` is required (`store_card_updates.py:41-43`), and they are filtered to the principal's projects (`store_card_updates.py:100-102`). A plangraph feeder sees the plangraph attempts. Each row has `item`, `role`, `host`, `started_at`, `ended_at` and `exit_class` (`store_card_updates.py:89-110`). Measured over the 3 days before 2026-11-27 17:30Z: 314 attempts, of which 42 had a non-`ok` exit class (13.4%). Feeder no_commit runs appear as `worker_crash`. The fleet facts below use ended attempts only.
- `whoami` returns `name` and `role` (`src/plangraph/keeper/store_read.py:111-115`). docs/writer-feeder.md requires "a dedicated feeder L1/worker principal token".

## Decisions

- D1. All reasons. Add `pub fn refusals(f: &HostFacts, p: &Policy) -> Vec<String>`. It evaluates every check in the base order and appends each failing reason: kill_switch, invalid_policy, stale_facts, unknown_host, cpu_pressure, then the new barren_rate and canary, then max_writers, ram, disk, provider_quota. `max_writers` is skipped when the host is unknown, because `unknown_host` already covers it. `admit()` returns `Refuse(first)` or `Admit`, so `reason` strings, exit codes and existing tests keep their meaning.
- D2. Every row that `decide()` returns carries `"refusals": [...]` (empty when admitted). `refusals` takes the stale quota explicitly: `refusals(f, p, quota_stale: Option<&str>)`. At the quota check's position it appends the full `quota_stale: ...` text when that is present, whatever `quota_remaining_percent` holds, and otherwise `provider_quota` when the value fails. So a stale quota beside a sampled healthy value is still listed. `reason` is the first entry, so with `feeder.off` present it is `kill_switch`. The tick's refusal row carries the list computed from `sim_facts` at the stop. When the tick already started a batch, the result row keeps that list. `decide()` adds `refusals` only to rows that have none.
- D3. Readback. Add the flag `--readback` to `plangraph feed`. It implies `--dry-run` (no claim, no release, no spawn, no receipt pruning, no event log, no Windows RAM cache write). It sets `kill_switch` to false for admission and adds `"kill_switch_ignored": true` when `feeder.off` exists. On WSL, readback also runs one direct live probe, `windows_ram::read_powershell(env)`. When that fails, it adds the refusal `windows_ram_live_probe: <why>` even if a fresh cache let the sample succeed. It calls `whoami` and adds the refusal `keeper_principal:<name>:<role>` when the role is not `l1` or `worker`. docs/writer-feeder.md makes `plangraph feed --once --live-facts --readback` the deploy readback, run after installing the units and before removing `feeder.off`.
- D4. Fleet facts. A new module `feeder/attempts.rs` reads `attempts?fmt=json&since=1d` once per decision in `runtime::decision`, beside the ready read. It reduces the rows to `Fleet { known, error, host_recent: Vec<String>, host_ok_24h: usize }`. `host_recent` holds the exit classes of this host's ended `implement` attempts, newest first, at most `barren_window_runs`. Hosts match with `same_host`. `HostFacts` gains `#[serde(skip)] pub fleet: Fleet`. `Fleet::default()` has `known: false`, which skips D5 and D6, so facts files and existing tests keep their behaviour. A failed read sets `known: true` and `error`, and adds the refusal `attempts_unavailable` (fail closed, like a stale quota).
- D5. Barren-rate pause. When `host_recent` has at least `barren_pause_runs` entries that are not `ok`, refuse `barren_rate:<k>/<n>`. Policy defaults are `barren_window_runs = 6` and `barren_pause_runs = 4`, valid when `1 <= pause <= window <= 50` (else invalid_policy). EXPERIMENT: at the measured 13.4% non-ok rate, P(4 or more of 6) is about 0.4%, so false pauses are rare. The 10:21Z incident (34 of 34 barren) pauses after the fourth. The pause ends as the barren attempts leave the 1-day window or when new ok attempts arrive. Measure pauses per host per day in the feeder log for 7 days.
- D6. Canary. When `known` and `host_ok_24h == 0`, the host admits only while `running_writers == 0` and refuses `canary` otherwise. The tick's simulated start raises `running_writers`, so one tick starts one canary writer.
- D7. Two deadlines from process start. `runtime::run` takes `Instant::now()` at entry and installs `adapters::Deadlines { work: start + TICK_WORK_SECS, hard: start + TICK_DEADLINE_SECS }` in a thread-local through a guard that clears it on drop. `pub const TICK_WORK_SECS: u64 = 60` and `pub const TICK_DEADLINE_SECS: u64 = 100` (the unit's 110 s less 10 s to write the event row). The 40 s between them is reserved for compensation. The `Launch` trait gains `fn time_left(&self) -> bool { true }`, and `Adapters` returns `now < work`. Before claiming the next batch (live only), the tick stops with `{"decision":"idle","reason":"tick_deadline"}` when no work time is left, keeping the rows already started.
- D8. Every subprocess of a feeder run is bounded. Discovery and new work run against `work`: keeper `ready`, `snapshot`, `attempts`, `whoami`, `claim` and the rival snapshots, plus the Python `ps` and quota adapters. Compensation and launch run against `hard`: keeper `release` and the spawner start. Keeper calls limit curl to `min(20, remaining seconds rounded up)` and return `Err("tick_deadline")` without starting curl when no time remains. The rounding is `adapters::request_limit(deadline, now, cap_secs) -> Option<u32>`, which `merge_close::DeadlineKeeper::call_with` also uses: one implementation. Python adapters and the spawner run through `adapters::output_until(cmd, deadline)`, which polls `try_wait` every 50 ms and kills the child at the deadline (`Err("tick_deadline")`). The existing `run_spawner` contract stays: once the process exists, a lost receipt does not prove that no writer started. A claim on a timed-out batch member makes the batch fail, and its held members are released inside the `hard` window. A release that still cannot finish leaves the claim to the keeper's lease expiry and is listed in `skipped` as `release failed: tick_deadline`. Without installed deadlines (tests, other callers), the calls behave as today.

## Entry points

- E1. `feeder::refusals` and `admit`.
- E2. `runtime::decide`, `runtime::decision` and the `--readback` option.
- E3. `feeder::attempts` (the fleet facts) and the D5 and D6 checks.
- E4. `workflow::tick` with `Launch::time_left`, and `adapters::Keeper` with `request_limit`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | Facts with kill_switch, an unknown host and no quota give `refusals == ["kill_switch","unknown_host:<h>","provider_quota"]` and `admit == Refuse("kill_switch")`. | X1 |
| B2 | E2 | A live dry decision with `feeder.off`, an unlisted host and a stale center quota lists `kill_switch`, `unknown_host:<h>` and the `quota_stale:` text in that order. Its `reason` is `kill_switch`. A stale quota beside a supplied quota of 80 is still listed as stale. An idle decision also carries `refusals`. A tick that started one batch and then stopped keeps the stop's refusal list. | X1 X2 X13 |
| B3 | E2 | Readback with `feeder.off` present ends in `would_spawn` with `kill_switch_ignored: true` and writes no Windows cache. On WSL, a failing live probe is listed as `windows_ram_live_probe:` even with a fresh cache, and a working one is not. A non-l1/worker role is listed as a `keeper_principal` refusal. | X3 X12 |
| B4 | E3 | Four non-ok of the last six host attempts refuse `barren_rate:4/6`, and three do not. A host with no ok attempt in 24 h admits one writer and refuses the second with `canary`. An unreadable attempts list refuses `attempts_unavailable`. | X4 X5 |
| B5 | E4 | A tick whose work time runs out after one start stops with `tick_deadline`, keeps the start and claims nothing more. A batch whose second member's claim times out starts nothing and releases its first member. | X6 X10 |
| B6 | E4 | A keeper call after its deadline fails with `tick_deadline` without curl, and `release` still runs between `work` and `hard`. 3.2 s left gives a 4 s limit, and 60 s left gives 20 s. A subprocess still running at its deadline is killed, and its call fails with `tick_deadline`. The unit's `TimeoutStartSec` is at least `TICK_DEADLINE_SECS + 10`. | X7 X8 X11 |

## Verification

Write X1-X8 and X10-X13 first. On the base they fail on the missing symbols, or on `refusals` absent from the row.

- X1. `rust/plangraph-cli/tests/feeder.rs::admit_lists_every_refusal_in_check_order`. Also assert `refusals(&f, &p, Some("quota_stale: x"))` on facts with quota 80 lists `quota_stale: x` and no `provider_quota`. Also add `fleet: Fleet::default()` to `facts()` there. Keep `admit_all_refusal_reasons_and_boundaries` unchanged.
- X2. `runtime/live_tests.rs::kill_switch_unknown_host_and_stale_quota_are_listed_together`. Use the `Live` fixture on an unlisted host and write `.config/plangraph/feeder.off` under the fixture home. Write a stale center quota as in `center_quota_drives_live_admission_and_preserves_capacity_when_stale`. Assert B2, and assert `refusals` on an idle decision with no eligible item.
- X3. `runtime/live_tests.rs::readback_ignores_the_kill_switch_and_checks_the_principal`. `Options` gains `readback: true`. Inject the whoami result through the `KeeperFacts` value that `decide` now takes (D4: `decision()` reads attempts and whoami and passes `KeeperFacts { fleet, principal }`). Assert B3 for roles `worker` and `router`.
- X4. `feeder/attempts.rs` unit tests `host_attempts_reduce_to_recent_classes_and_ok_count` and `barren_rate_and_canary_refusals_follow_the_host_history`. Build row JSON with mixed hosts (`coding-vm`, `coding.vm`, `dell`), roles and open attempts. Assert the reduction and the B4 refusals through `refusals()`.
- X5. `feeder/attempts.rs` unit test `an_unreadable_attempt_list_refuses_admission`. Use a `Transport` fake returning an error.
- X6. `rust/plangraph-cli/tests/feeder_slots.rs::a_tick_without_time_for_another_slot_stops_and_keeps_its_start`. `feeder_support::Host` gains `time_left_after: Option<usize>`. Its `time_left` is false once that many runs started. With two eligible items and `Some(1)`: one start, `stopped == "tick_deadline"`, and only the first item claimed.
- X7. `adapters.rs` unit test `request_limit_rounds_up_and_refuses_after_the_deadline`. Also test `Keeper` with a past deadline and no `PG_URL`: the error is `tick_deadline`, not `PG_URL missing`. The existing `merge_close` tests stay green unchanged.
- X8. `rust/plangraph-cli/tests/feeder_transport.rs::unit_timeout_leaves_room_after_the_tick_deadline`. Parse `TimeoutStartSec` from the included unit and assert it is at least `TICK_DEADLINE_SECS + 10`.
- X10. `rust/plangraph-cli/tests/feeder_slots.rs::a_batch_whose_claim_runs_out_of_time_releases_what_it_held`. Use `policy(3)`, two related items (`AAA-1`, `AAA-2`) and `Host.unavailable_at = Some("plangraph:AAA-2")`. A timed-out keeper claim reaches `claim_batch` as `ClaimError::Unavailable` (`adapters.rs:219-231`), the same path as the fixture's unavailable claim (`workflow.rs:51-54`). Assert no start, that `released` contains `plangraph:AAA-1`, and that the result is a refusal with `error: true`.
- X11. `adapters.rs` unit test `output_until_kills_a_child_at_its_deadline`. `sh -c 'sleep 5; echo {}'` with a deadline 200 ms ahead returns `Err("tick_deadline")` in under 2 s. `sh -c 'echo {}'` returns `Ok`.
- X12. `runtime/live_tests.rs::readback_surfaces_a_failed_live_windows_probe_despite_a_fresh_cache`. Use the `Live` fixture with `WSL_INTEROP` set, write a fresh `windows_ram` cache file into the state dir, and run without a `powershell.exe` on the fixture PATH: `refusals` contains `windows_ram_live_probe:`, and the cache file is unchanged. Then add a `powershell.exe` stub that prints a free-KiB value `parse_free_kb` accepts: no such refusal, `windows_ram_source == "live"`, and the cache file is still unchanged.
- X13. `rust/plangraph-cli/tests/feeder_slots.rs::a_stopped_tick_keeps_the_refusals_of_its_stop`. Two disjoint items, facts with RAM for exactly one writer: one start, `stopped == "ram"`, and `refusals == ["ram"]` on the result row.
- X9. Held out: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p plangraph-cli --test feeder --test feeder_batches --test feeder_contracts --test merge_close`.

## Scope and reuse

- R1. One admission function. `admit` is the first entry of `refusals`, with no second rule set.
- R2. One deadline arithmetic (`request_limit`), shared by the feeder keeper and merge-close, and one subprocess bound (`output_until`) for every feeder adapter.
- R3. The fleet facts module is the single reader of keeper attempts for the feeder. FEEDER-RANKS-BY-GOAL-AND-CAPS-INDEPENDENT-OF-HARDWARE extends `Fleet`. It adds no second reader.
- N1. No change to the keeper, to the units' timeout or timer, or to the spawner. Deployment and the 20-run journal check are operator work.

### File budgets

Final line caps are in the card. Additions: `mod.rs` <= 70, `runtime.rs` <= 60, `workflow.rs` <= 15, `adapters.rs` <= 90, `merge_close.rs` <= 10 (net <= 0), `capacity.rs` <= 3, `sources.rs` <= 8, `runtime/live_tests.rs` <= 130, `tests/feeder.rs` <= 35, `tests/feeder_slots.rs` <= 60, `tests/feeder_support/mod.rs` <= 10, `tests/feeder_transport.rs` <= 15, `deploy/feeder/policy.toml` <= 3, `docs/writer-feeder.md` <= 20. New: `feeder/attempts.rs` <= 220 (with tests), `changelog.d/<ID>.md` <= 6.

### Implementation

- `mod.rs`: `Fleet` (Default, Clone, Debug), the two policy fields and their validation inside the invalid_policy check, `refusals(f, p, quota_stale)`, `admit` via `refusals(f, p, None)`, and `pub const TICK_WORK_SECS` and `TICK_DEADLINE_SECS`.
- `attempts.rs`: `read(keeper: &impl Transport, host: &str, policy: &Policy) -> Fleet` and the pure `reduce(rows: &Value, host: &str, policy: &Policy, now: i64) -> Fleet`. Unparseable rows are skipped. A non-array body is an error.
- `runtime.rs`: the `--readback` option and usage line, `KeeperFacts`, a `decide` parameter, `refusals` on every row, `kill_switch_ignored`, the readback live probe, and the `Deadlines` guard at entry. `capacity::sample` keeps `dry` (no cache write in readback).
- `workflow.rs`: the `time_left` check before `claim_batch` in the live branch, and `refusals` on the refusal row.
- `adapters.rs`: `Deadlines` (thread-local plus guard), `request_limit`, `output_until`. `post()` picks `hard` for the `release` verb and `work` otherwise. `output()` and `run_spawner` use `output_until` with `work` and `hard` respectively.
- `capacity.rs` and `sources.rs`: add `fleet: Fleet::default()` to the `HostFacts` literals. `sources::facts` passes the stale reason to `refusals` through `FactsOutcome`.
- `policy.toml`: `barren_window_runs = 6` and `barren_pause_runs = 4` with a comment naming the experiment.
- `docs/writer-feeder.md`: refusal lists, readback in the deploy steps, canary, barren pause and tick deadline.

## Open points for design approval

None.
