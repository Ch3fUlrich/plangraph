# Rust hook shadow harness (RUST-HOOKS slice 1)

Run `pg-hook shadow ENTRY LOG_PATH PYTHON_HOOK_PATH` with the original payload on stdin.
Python's exit code, stdout and stderr remain authoritative; Rust errors, unwind panics
and log-write failures cannot change that result. Logs append JSONL `{entry, same, py, rust}`;
successful outputs use the existing parity fields `exit`, `stdout`, `stderr`.
Rust failures use `error` or `panic`; pending hooks use `status: NotImplemented`, not `error`.
All five registry entries are pending: `bash_guard`, `fleet_inject_pre`, `fleet_inject_post`,
`fleet_report`, `ctx_guard_session`. Supply the matching Python script explicitly
(`fleet_inject.py` serves both inject entries). No settings or switch wiring is installed.
Keep logs private: they contain hook outputs. Log paths are explicit CLI arguments.
`cd rust && cargo test -p pg-hook` runs isolated replay tests and loads the real
`tests/hooks/parity_payloads.json`: recorded Python outputs are the differential oracle.
The driver restores `{TOK}`, substitutes `{TMP}`, seeds temporary files/HOME/state,
maps historical `fleet_inject`/`context_guard` names, and skips NotImplemented entries.
Hook ports, property tests, 14-day zero-diff observation, rollback canary and settings
integration remain deferred; the production switch belongs to **RUST-HOOKS-SWITCH**.
