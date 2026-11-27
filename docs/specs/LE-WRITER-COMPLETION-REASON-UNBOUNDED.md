# LE-WRITER-COMPLETION-REASON-UNBOUNDED

Outcome. A terminal run record keeps `reason` and `completion.reason` within 4096 bytes plus a marker that names `reason.log`, which keeps at most the first and last 512 KiB of the failure text in the run directory.

- plan_source. Keeper item plangraph:LE-WRITER-COMPLETION-REASON-UNBOUNDED, 2026-11-27 10:34Z, priority 10, D-7042. D-7047 (rule 3.112) names the item but leaves the premise unchanged.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 7a9a507529d565db53fc5fdabdf4de26d81fe5326e113dd626246f23a9254ab2
- tree SHA-256. 8a38108b5ae50552713b6c03cb6237fd8384f14f8fc98d85f406026800f1e82a
- evidence_after_deploy. After the merge is installed on every host, run `find ~/.local/state/plangraph/runs -name record.json -size +1M -newer <installed binary>` on coding.vm, dell and the workstation guest once 24 hours have passed. The expected result is no output. Also record one new failed native run whose `reason` ends in the marker and whose `reason.log` is at most 1 MiB plus its marker and holds the head and tail of the push output.

## Checked against base

- The three quarantined dell records (dell `runs-oversize-quarantine` directory: `run-2aa53941…`, `run-3888c619…` and `run-b1edfc87…`) are all `run_kind: native-codex`, `role: implement`, `status: failed`. Read on 2026-11-27, `reason` takes 44,046,737, 40,688,154 and 7,822,264 JSON bytes, and `completion.reason` holds the same text. Every one starts `command failed (exit status: 1):\nstdout: pre-push: hook checkout head=…` and ends `error: failed to push some refs`. That is the push hook's full output. `error` is 14 bytes in each record.
- The text comes from `output()` in the native writer host. It formats both streams of a failed command in full. `finish` then stores the error string unchanged as `record["reason"]`, and `Host::save` publishes the record through the lifecycle journal with no bound. [rust/pg-spawnd/src/native_writer_host.rs:94–105,482–497; rust/pg-spawnd/src/native_writer.rs:106–155; rust/worker-core/src/run/lifecycle.rs:187–233]
- The Python spawner copies `record['reason']` into `completion.reason` when it first saves a terminal record. `SpawnService._save` limits `error` to 2000 characters, `stderr_tail` to 4096 bytes and `stdout_tail` through `keep_output`. It applies no bound to `reason` or `completion`. `terminal_reason` can fill `reason` from an unbounded `last_reason`, `error` or stderr line. [src/plangraph/spawner/notify.py:419–445; src/plangraph/spawner/runner.py:856–878; src/plangraph/spawner/service.py:139–181; src/plangraph/spawner/exit_outcome.py:449–463]
- Readers reject a record above `MAX_RECORD_BYTES` (8 MiB). The lane guard needs that limit to stay, so this item bounds the writers and leaves the reader limit unchanged. [rust/pg-spawnd/src/record_file.rs:5,46–67]
- `stderr_capture.py` already keeps per-run diagnostics in a private file beside the record (`stderr.log`). [src/plangraph/spawner/stderr_capture.py:1–23]

## Decisions

- D1. Bound: `REASON_KEEP_BYTES = 4096` in both writers. A reason whose UTF-8 encoding is at most 4096 bytes is stored unchanged and writes no file. A longer reason is stored as the first 1024 bytes, then `\n[... <N> bytes omitted; see reason.log ...]\n`, then the last 3072 bytes. N is the decimal byte count left out. Both cuts move back to the nearest UTF-8 character boundary. The head shows the failing command and the tail shows the final error.
- D2. Log file, bounded (coordinator decision 2026-11-27): `reason.log` goes in the run directory (the parent directory of `record.json`), written with the existing atomic private writer and mode 0600. `REASON_LOG_KEEP_BYTES = 512 * 1024`. A failure text of at most 1 MiB (2 × 512 KiB) is written whole. A longer one is written as its first 512 KiB, then the one-line marker `\n[... <N> bytes elided ...]\n`, then its last 512 KiB, where N is the decimal count of dropped bytes. Both cuts move back to the nearest UTF-8 character boundary. The record gets `"reason_log": "reason.log"`, a relative file name only, so redaction and moving the run directory keep it valid.
- D3. Idempotent: a bounded reason is at most 4096 bytes plus the marker, so a re-save finds nothing to bound and does not touch `reason.log`. A record that already carries `reason_log` keeps it.
- D4. One implementation per writer. The native writer (Rust) writes its record without passing through Python, and Python spawner runs never pass through Rust. Rust gets one pure function in a new lib module, `pg_spawnd::reason_bound`. Python gets one function, `keep_reason`, in `spawner/stderr_capture.py`. Both use the constant, marker text and file name from D1 and D2. The Python and Rust tests assert the same marker text.
- D5. Where each bound runs. Rust: `Host::save`, before `lifecycle::publish`. Every native writer terminal path goes through it, `finish` and `execute_request`'s configure refusal included. Python: `SpawnService._save`, directly after `record.update(clean(record))`, so `reason.log` holds redacted text. It bounds `record['reason']` and, when `record['completion']` is a dict, `completion['reason']`. If both carry the same long text, they share one `reason.log`. A `completion.reason` that differs from `record['reason']` is set to the bounded `record['reason']`. The two refusal writes in `native_writer_commands.rs` (work-disk error and card sandbox refusal) store short fixed messages and stay unchanged.

## Entry points

- E1. Native writer record save: `Host::save` in `rust/pg-spawnd/src/native_writer_host.rs`, reached from `native_writer::finish`.
- E2. Python spawner record save: `SpawnService._save` in `src/plangraph/spawner/service.py`. The terminal completion save in `runner.py:873–878` reaches it through `super()._save`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A terminal native record whose reason is a 10 MB command failure is written under 64 KiB. Its reason starts with the first bytes of the failure, carries the marker with the exact omitted byte count and `reason.log`, and ends with the failure's last line. `reason.log` holds the first and last 512 KiB of the failure around the D2 marker, and `reason_log` is `"reason.log"`. | X1 X2 |
| B2 | E2 | A Python-saved failed record with a 10 MB `reason` and the same `completion.reason` is written under 64 KiB. Both fields are bounded the same way, and one `reason.log` holds the redacted text bounded per D2. | X3 |
| B3 | E1 E2 | A reason of at most 4096 bytes is stored unchanged with no `reason.log` and no `reason_log`. Saving a bounded record again leaves the record and `reason.log` byte-identical. | X1 X3 |
| S1 | E1 E2 | No new record over 1 MiB on any host for 24 hours after deployment. | X4 |

## Verification

- X1. E1, changed. `rust/pg-spawnd/src/reason_bound.rs` tests `short_reason_is_unchanged`, `long_reason_keeps_head_tail_and_omitted_count` (asserts the exact marker `\n[... 9995904 bytes omitted; see reason.log ...]\n` for a 10,000,000-byte input), `log_text_keeps_head_and_tail_of_a_five_megabyte_reason` (a 5,000,000-byte input gives a log text of at most 1 MiB plus the marker `\n[... 3951424 bytes elided ...]\n`, starting with the input's first 512 KiB and ending with its last 512 KiB; a 1 MiB input is kept whole) and `cuts_fall_on_char_boundaries` (multi-byte characters on both cut points). Check: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p pg-spawnd --lib reason_bound`.
- X2. E1, changed. `rust/pg-spawnd/src/native_writer_host_reason_tests.rs::terminal_native_record_with_a_ten_megabyte_reason_stays_under_64_kib`. Build a `Host` the way `native_writer_host_tests.rs::terminal_writer_appends_ledger_with_log_tokens_once` does, save a terminal record whose reason is `command failed (exit status: 1):\nstdout: ` + 10 MB + `\nerror: failed to push some refs`, then assert B1 on `record.json` and `reason.log`. Save again and assert B3. Check: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p pg-spawnd --bin pg-spawnd native_writer_host::reason_tests`.
- X3. E2, changed. `tests/test_spawner_reason_bound.py::test_ten_megabyte_reason_and_completion_reason_are_bounded_and_logged`, `test_five_megabyte_reason_log_keeps_head_and_tail_within_one_mebibyte` (asserts `reason.log` is at most 1 MiB plus the D2 marker, names 3,951,424 elided bytes, and keeps the first and last 512 KiB) and `test_short_reason_is_unchanged_and_writes_no_log`. Use `SpawnService(state=tmp_path)._save(record)` as `tests/test_spawner_exit_stderr_capture.py` does. Assert B2 and B3, including the identical record and log marker texts from X1. Check: `python3 -m pytest -q tests/test_spawner_reason_bound.py`.
- X4. E1 E2, live only. The `find` in evidence_after_deploy.
- X5. Held out. `python3 -m pytest -q tests/test_spawner_exit_stderr_capture.py && cargo test --manifest-path rust/Cargo.toml --offline --locked -p pg-spawnd --bin pg-spawnd native_writer_host::tests`, unchanged and green.

Write X1–X3 first and see them fail on the base, where the record keeps the whole text and no `reason.log` exists.

## Scope and reuse

- R1. Reuse `pg_worker_core::run::record_io::atomic_write_private` (Rust) and `routing_support.private_io.atomic_write_private` (Python). Add no new record field besides `reason_log`. Leave the 8 MiB reader limit, `error`, `stderr_tail`, `stdout_tail` and `stderr.log` unchanged.
- N1. Readers that meet an oversized record are LE-SPAWND-ONE-OVERSIZE-RUN-RECORD-BLOCKS-THE-LANE. The quarantined dell records stay where they are.

### File budgets

`runner.py` is above the 800-line limit on the baseline and stays read-only. The Python change goes in `stderr_capture.py` (33 lines, at most 75 after the change) and one call in `service.py`. The Rust tests go in a new sibling file, because `native_writer_host_tests.rs` (781 lines) has no room for a 40-line test. `native_writer_host.rs` gets the call in `save` plus the three-line `#[cfg(test)] #[path = "native_writer_host_reason_tests.rs"] mod reason_tests;`. The changes to `src/plangraph/spawner/**` are documented in the fragment `docs/spawner/LE-WRITER-COMPLETION-REASON-UNBOUNDED.md` (the docs map rule for `docs/SPAWNER.md`).

### Implementation

- Rust `reason_bound.rs`: `pub const REASON_KEEP_BYTES: usize = 4096; pub const REASON_LOG_KEEP_BYTES: usize = 512 * 1024; pub const REASON_LOG: &str = "reason.log";` `pub fn bound(text: &str) -> Option<String>` returns `None` when no bound applies. `pub fn log_text(text: &str) -> Cow<str>` applies D2. `pub fn bound_record(record: &mut Value, run_dir: &Path) -> io::Result<()>`: when `record["reason"]` is a string and `bound` returns `Some`, it writes `log_text(text)` to `run_dir/reason.log` with `atomic_write_private`, then sets `reason` and `reason_log`. Any other value is left unchanged. Register it with `pub mod reason_bound;` in `lib.rs`.
- `Host::save`: after cloning the record and before `lifecycle::publish`, call `pg_spawnd::reason_bound::bound_record(&mut record, self.record_path.parent().ok_or("record directory missing")?)` and map an I/O error to `String`. A failed log write fails the save. The writer then reports `native writer record save failed`, the same as for any other save error.
- Python `stderr_capture.py`: `REASON_KEEP = 4096`, `REASON_LOG_KEEP = 512 * 1024`, `REASON_LOG = 'reason.log'`, `keep_reason(text) -> str | None`, `reason_log_text(text) -> str` (D2), and `bound_reasons(record, run_dir)`. `bound_reasons` applies D1–D3 to `record['reason']` and `record['completion']['reason']` (D5) and writes the log with `atomic_write_private`. In `SpawnService._save`, call `bound_reasons(record, self._path(record['run_id']).parent)` on the line after `record.update(clean(record))`.
- Docs: the fragment states the bound, the marker, `reason.log` and `reason_log`. The changelog fragment is one line.

## Open points for design approval

None.
