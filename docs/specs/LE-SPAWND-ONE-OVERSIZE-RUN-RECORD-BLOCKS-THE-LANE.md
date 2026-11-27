# LE-SPAWND-ONE-OVERSIZE-RUN-RECORD-BLOCKS-THE-LANE

Outcome. A run record above the 8 MiB read bound is judged from a bounded field projection. A terminal one holds no key, a live or undecidable one holds only the keys it names, and the stall watchdog alerts routing once per oversized record.

- plan_source. Keeper item plangraph:LE-SPAWND-ONE-OVERSIZE-RUN-RECORD-BLOCKS-THE-LANE, 2026-11-27 10:34Z, priority 10, D-7041 and D-7042. D-7047 (rule 3.112) names the item but leaves the premise unchanged.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 50d3b136d010e528cf5a8721d5a3ccf067e397510a2dc502db59d7ccdfe2e4e3
- tree SHA-256. 2da9b3e766730369da4ec908ac51eea741b708be900b2b4fc16f7bba25dae2af
- evidence_after_deploy. After the binary is installed on dell, copy one quarantined record back under `~/.local/state/plangraph/runs/` as an operator-approved step, or wait for the next naturally oversized record. Then record a dell feeder pass that spawns with it present, and the one `oversize-record` alert in `watchdog/outbox`.

## Checked against base

- `scan_runs` reads each `runs/run-*/record.json` with `read_json`. `read_json` returns `Invalid` for any file above `MAX_RECORD_BYTES`. An `Invalid` record is pushed to `unreadable`, and `Observed::lanes` turns every unreadable name into a lane that holds every requested key. `refuse_held` then refuses the whole launch as `lane_unreadable`. [rust/pg-spawnd/src/lane_guard.rs:38–50,214–250,308–324; rust/pg-spawnd/src/record_file.rs:5,46–67]
- That is the 2026-11-27 10:25Z dell refusal: `lane_unreadable: unreadable run record run-2aa53941; run-3888c619; run-b1edfc87`. The three records were terminal native writer failures of 88, 81 and 15.7 MB. All three were `status: failed`, so all three would be judged `Free`.
- A prefix read cannot judge these records. `pg-spawnd` builds serde_json with `preserve_order`, and the native writer adds `reason`, `ended` and any `survivors` to a record that the Python launch created. Liveness needs `status` and the presence of `survivors` (`run_finished`), and `survivors` can follow a 44 MB `reason`. So the field order gives no bound before the end of the file. The native cancellation path adds `survivors` after the run's other fields. [rust/pg-spawnd/Cargo.toml:16; rust/worker-core/src/sweep_runs.rs:17–32; src/plangraph/spawner/overflow.py:679–685]
- `pg-spawnd` depends on `serde = "1"` (the `Deserializer`, `Visitor` and `DeserializeSeed` traits, without `derive`) and on `serde_json` with `preserve_order`. The projection below needs no new dependency or feature. [rust/pg-spawnd/Cargo.toml:16–17]
- The stall watchdog sweep reads every record on its timer. Its `deliver_event` already queues alerts for routing, and it records one-time rules by event id, so a rule there fires once per run across ticks and restarts. [src/plangraph/spawner/stall_watchdog.py:146–200,254–306]
- The cause of the size is LE-WRITER-COMPLETION-REASON-UNBOUNDED. This item makes readers tolerate records that are already large and records that grow large in other ways.

## Decisions

- D1. Projection read. When `read_json` returns `Invalid` for a regular file larger than `MAX_RECORD_BYTES` and at most `MAX_PROJECTION_BYTES = 512 MiB`, `record_file::read_projection(path)` streams the file once through `serde_json::Deserializer::from_reader`. The reader is a private `CappedReader` wrapping a `BufReader` of `open_record(path)?`. It counts bytes consumed, and while a cap is armed it returns an I/O error once the count passes the armed limit. The top level is read with `deserialize_map` and a hand-written `Visitor`, so a non-object top level fails. Keys longer than 64 bytes fail. For a kept key, the visitor arms the cap (consumed + cap) and deserializes the value as `serde_json::Value`, then disarms. Any other value is skipped with `IgnoredAny`, which serde_json scans without buffering. So a kept field can never allocate more than its cap. Caps: `manifest.brief` 1 MiB; `status`, `survivors`, `role`, `overflow_remote`, `origin_receipt`, `item_key`, `item_keys`, `lane_admitted`, `fleet_session_name`, `run_id` and `review_dir` 64 KiB each. `review_dir` is kept for the native-admission check in LE-NATIVE-LAUNCH-ADMITS-NO-ITEM-AND-REFUSES-THE-WRITERS-COMMITS-LATER. `manifest` is read by a nested visitor. An absent `manifest` stays absent and `null` stays `null`. For an object, only its `brief` is kept (capped), and other members are skipped; an object without `brief` becomes `{}`. Any other JSON type is skipped and stored as `null`, because `record_keys` reads only `manifest.brief` and treats a non-object manifest exactly as `null`. After the top-level object, `Deserializer::end()` must succeed, so trailing data fails the projection. A duplicate kept key fails the projection. The projection returns a `Value` object that carries exactly the kept keys present in the file. A present `null` stays `null` and an absent key stays absent, so `run_finished` and `record_keys` judge it as they would judge the full record.
- D2. Judgment. The projected value goes through the existing `liveness` and `record_keys`. `Free` holds nothing. `Holds` is a normal run lane with its keys. `Unreadable` with at least one named key becomes a lane named `unreadable run record <dir>` with `source: "unreadable"` and only those keys. `Unreadable` with no named key, a failed projection, or a file above 512 MiB keeps today's behaviour and holds every requested key. Records within 8 MiB keep their current judgment unchanged, including "undecidable holds every key".
- D3. Scan result. `RunScan` gains `pub held_unreadable: Vec<(String, Vec<String>)>` for D2's keyed undecidable records and `pub oversize: Vec<String>` for the directory name of every record that went through the projection. `unreadable` keeps its meaning. `Observed::lanes` maps `held_unreadable` to keyed unreadable lanes. The refusal text stays `lane_unreadable: unreadable run record <dir>; …`, so `lane_admit::REASONS` and `run_refusal.py` match it without change.
- D4. Alarm. The stall watchdog raises the one alarm, not the admission guard. Admission runs on every launch and has no durable outbox; the watchdog has both a timer and one-time delivery. In `_read_runs`, when `sweep` is set and `dry_run` is not, a record whose `stat().st_size` exceeds `RECORD_LIMIT = 8 * 1024 * 1024` (mirroring `MAX_RECORD_BYTES` in `rust/pg-spawnd/src/record_file.rs`, named in a comment) is delivered before the record is read as `dict(rule='oversize-record', kind='alert', recipient='routing', run_id=<dir>, bytes=<size>, limit=RECORD_LIMIT)`. `'oversize-record'` joins the one-time rules in `deliver_event`. The record is then read as it is today.
- D5. No completion-marker shortcut. The item offers `*.delivered` markers as an alternative proof of termination. They are written by the Python service only and say nothing about survivors, so the projection is the single judgment.

## Entry points

- E1. Lane admission: `pg-spawnd lane-admit` and the MCP native spawn through `lane_guard::admit_launch` → `Observed::lanes` → `scan_runs`.
- E2. Stall watchdog sweep: `stall_watchdog.sweep_runs` → `_read_runs`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A state dir with one 9 MiB terminal record (its `status` after a 9 MiB `reason`) and one live record holding `demo:BETA` admits a launch for `demo:ALPHA`. `scan_runs(...).oversize` names the terminal record. | X1 |
| B2 | E1 | A 9 MiB live record naming `demo:ALPHA` refuses `demo:ALPHA` as `lane_collision`, naming its run, and admits `demo:BETA`. A 9 MiB terminal record (`status: failed`) with `survivors: [4242]` after the `reason` holds its keys. The same record with `survivors: []` holds none. | X1 |
| B3 | E1 | A 9 MiB record with an unknown status and `item_key: demo:ALPHA` refuses `demo:ALPHA` as `lane_unreadable` and admits `demo:BETA`. A 9 MiB file that is not JSON, that has a non-object top level, a kept field above its cap, or trailing data after the object, refuses every key as today. The projection of a 9 MiB live record keeps its `review_dir`. Absent, `null`, non-object and object manifests project as D1 states. | X1 |
| B4 | E2 | Two sweeps over a state with one 9 MiB record and one small record leave exactly one `oversize-record` alert for routing, naming the run and its byte size. A dry run leaves none. | X2 |
| S1 | E1 | A dell feeder pass spawns with an oversized terminal record present. | X3 |

## Verification

- X1. E1, changed. In `rust/pg-spawnd/src/lane_guard_tests.rs`: `an_oversize_terminal_record_holds_no_key_and_is_listed_oversize`, `an_oversize_live_record_refuses_only_its_own_keys`, `an_oversize_terminal_record_with_survivors_after_the_reason_holds_and_an_empty_list_does_not`, `an_oversize_undecidable_record_refuses_only_the_keys_it_names`, `an_oversize_record_that_does_not_project_refuses_every_key` (not JSON; a top-level array; an `item_key` of 65 KiB; `{...}` followed by `x`), `an_oversize_live_record_projects_its_review_dir` and `manifest_projection_keeps_absent_null_non_object_and_object_apart`. Write each record with a `reason` of `MAX_RECORD_BYTES + 1 MiB` bytes placed before `status` (the fixture writes the JSON text by hand to fix the order). Check: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p pg-spawnd --lib lane_guard::tests`.
- X2. E2, changed. `tests/test_stall_watchdog_oversize.py::test_an_oversize_record_alerts_routing_once` and `test_a_dry_run_sweep_alerts_nothing`. Use `stall_watchdog.sweep_runs(state)` and read `watchdog/outbox/*.json` as `tests/test_stall_watchdog.py::test_real_readers_and_durable_delivery` does. Check: `python3 -m pytest -q tests/test_stall_watchdog_oversize.py`.
- X3. E1, live only. The evidence_after_deploy observation on dell.
- X4. Held out. `cargo test --manifest-path rust/Cargo.toml --offline --locked -p pg-spawnd --test record_limits --test lane_admit && python3 -m pytest -q tests/test_stall_watchdog.py`, unchanged and green. `record_limits` keeps proving that `result` and `ps` reject hostile records: the projection is used only by the lane guard.

Write X1 and X2 first and see B1, B3 and B4 fail on the base.

## Scope and reuse

- R1. One projection reader in `record_file.rs`, next to `read_json` and `open_record`, and one judgment (`liveness`, `record_keys`). `records::read_record_file` (used by `result`, `status` and steer) keeps the 8 MiB bound and is not changed.
- N1. No change to how records are written (LE-WRITER-COMPLETION-REASON-UNBOUNDED), to the quarantine directory on dell, or to the feeder.

### File budgets

`lane_guard.rs` 372 → at most 410 lines. `record_file.rs` 69 → at most 189 lines. `lane_guard_tests.rs` 518 → at most 668 lines. `stall_watchdog.py` 548 → at most 557 lines. No `Cargo.toml` or `Cargo.lock` change. The Python watchdog change is documented in the fragment `docs/spawner/LE-SPAWND-ONE-OVERSIZE-RUN-RECORD-BLOCKS-THE-LANE.md`.

### Implementation

- `record_file.rs`: `pub const MAX_PROJECTION_BYTES: u64 = 512 * 1024 * 1024;` and `pub fn read_projection(path: &Path) -> Result<Value, ReadError>`. Re-check that the path is a regular file within the limit with `symlink_metadata`, then read through `CappedReader` (D1), with an overall `take(MAX_PROJECTION_BYTES + 1)`. Use a private top-level `Visitor` (`visit_map`) and a `Capped(cap)` `DeserializeSeed` that arms and disarms the reader cap around `Value::deserialize`. The armed limit is held in a `Cell` shared by the reader and the seed. A cap overrun, a parse error or trailing data maps to `ReadError::Invalid`.
- `lane_guard.rs`: in `scan_runs`, on `Err(ReadError::Invalid)`, check `symlink_metadata(&path)` for a regular file above `MAX_RECORD_BYTES`. If it is one, push the name to `oversize` and judge `read_projection` per D2. Otherwise push to `unreadable` as today. In `Observed::lanes`, add the `held_unreadable` lanes. Update the module doc comment to state the oversize rule.
- `stall_watchdog.py`: D4. Add `RECORD_LIMIT` at module level and `'oversize-record'` to the `once` tuple.

## Open points for design approval

None.
