# Rust hook shadow harness (RUST-HOOKS slice 1)

Run `pg-hook shadow ENTRY LOG_PATH PYTHON_HOOK_PATH` with the original payload on stdin.
Python's exit code, stdout and stderr remain authoritative; Rust errors, unwind panics
and log-write failures cannot change that result. Logs append JSONL `{entry, same, py, rust}`;
successful outputs use the existing parity fields `exit`, `stdout`, `stderr`.
Both implementations receive identical raw stdin bytes, even for invalid UTF-8.
CLI usage mistakes and internal failures (including stdin reads or Python spawn)
fail open with exit 0; an executed Python hook's status still wins, including signals
encoded as 128 + signal in both the returned status and the parity comparison.
Rust failures use `error` or `panic`; pending hooks use `status: NotImplemented`, not `error`.
All five registry entries are pending: `bash_guard`, `fleet_inject_pre`, `fleet_inject_post`,
`fleet_report`, `ctx_guard_session`. Supply the matching Python script explicitly
(`fleet_inject.py` serves both inject entries). No settings or switch wiring is installed.
Harness faults add `harness: {error}` and `same: null` rather than a false parity
comparison; unavailable Python/Rust observations are `null`. Spawn, stdin read/write,
wait and harness unwind failures are accounted for when the log is writable.
An early Python stdin close still preserves its completed output and status.
Logs contain hook outputs: on Unix the harness creates them with mode 0600 and
tightens existing log permissions before appending. Log paths are explicit arguments.
`cd rust && cargo test -p pg-hook` runs isolated replay tests and loads the real
`tests/hooks/parity_payloads.json`: recorded Python outputs are the differential oracle.
The driver keeps `{TOK}` symbolic (no secret-shaped strings are constructed),
substitutes `{TMP}`, seeds temporary files/HOME/state,
maps historical `fleet_inject`/`context_guard` names, and skips NotImplemented entries.
The driver explicitly checks that the pending registry produces no comparisons,
then injects no-op test hooks for the 11 recorded no-op report/session cases and a
different-output hook for 12 negative comparisons. Both inject aliases also run
through independent no-op test hooks: all six inject cases produce comparisons,
including two intentional seeded-inbox mismatches and four matches. Together these
exercise every one of the 29 corpus cases and the comparator
without claiming that pending production hooks have passed parity. Replay tests
resolve Python before clearing the environment; CLI tests use a private PATH with
that interpreter, so non-system Python installations work without inherited state.
Only `rust/pg-hook/**` maps to this doc; the shared workspace manifest and lock
file remain unmapped, as required by `.docs-map`'s shared-file policy.
Hook ports, property tests, 14-day zero-diff observation, rollback canary and settings
integration remain deferred; the production switch belongs to **RUST-HOOKS-SWITCH**.
