# Fleet command ports

`rust/pg-fleet` provides native fleet commands with the legacy command names.
Build from `rust/` using `cargo build -p pg-fleet --release`. Install with
`cargo install --path pg-fleet` after reviewing the local cutover checklist.
This package does not change live PATHs or remove the source tools.

`handoff-check CARD [--log]` validates the eight-section, forty-line handoff
format and appends new Done entries to `FLEET_WORKLOG`, defaulting to
`~/.local/state/fleet/worklog.jsonl`. Appends take an exclusive file lock.
Secret-like filenames and configuration outside `liveness/` are refused.

`host-sync` implements the fleet PATH check for host setup. It checks
`handoff-check`, `resume-digest`, and `fleet-name`. Set `FLEET_BIN_DIR` to
the directory containing the selected installed binaries (or the existing
data checkout's router/bin during transition), and `FLEET_PATH_DIR` to an
existing PATH entry to repair missing commands. POSIX repairs create links;
Windows repairs copy executable files. A correct PATH makes no changes.
Every unresolved command prints `FAIL fleet PATH: COMMAND: REASON` and
causes exit 1. Invoke this fleet check from the host's synchronization flow;
it does not perform host-specific repository synchronization.

Parity fixtures record Python behavior before implementation; integration
tests use temporary homes, worklogs and repositories. No tests use live keeper
writes or live fleet sessions.
