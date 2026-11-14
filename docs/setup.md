# Rust user setup

Build with `cd rust && cargo build -p plangraph-cli`, then put the resulting
`plangraph` binary on your PATH. This core provides `--version`, `env check`,
`setup apply [--dry-run]`, and `setup check`. Other commands are refused.
MCP transport, hooks, services, cost gates, namespace moves and package installation
belong to later slices. Setup performs no live service activation.

`setup apply --dry-run` previews the embedded `setup/manifest.toml`.
`setup apply` installs missing environment defaults; `setup check` reports drift.
The selected user home is HOME, with USERPROFILE as a fallback. The env-file path
is PLANGRAPH_ENV_FILE or `<home>/.config/plangraph/plangraph.env`.
Environment loading uses process values, then env-file values, then built-in
PG_URL, PG_LEASE_S and PG_HEARTBEAT_S defaults. Unknown keys are preserved.
Loaded values are returned as data and are never exported into the process environment.
These user-selected paths are configuration conveniences, not trusted security
inputs: a future admission gate must independently establish trusted identity,
config and state paths and check ownership/privacy before using them.

The env file accepts uppercase keys, optional matching quotes, comments on their
own lines and literal values (no expansion). Duplicate keys, invalid syntax and
inline keys containing TOKEN, SECRET, PASSWORD, KEY or PAT are refused.
Credential file paths ending in `_FILE` are permitted. Process credentials are
not subject to the env-file prohibition. No env values appear in diagnostics.
Setup preserves existing env values and comments and adds only missing defaults.

The setup contract supports `env` and `text` entries with home-relative targets;
JSON/hook merging is deferred. Unknown fields/formats, invalid env content,
unsafe paths, symlinks at or below home and non-file targets are refused. Optional `platforms`
limits an entry to the named Rust operating systems. Each target produces an
action line. Exit codes: 0 success, 1 check drift, 2 usage/refusal, 3 partial failure.

Changed targets use a same-directory persistent lock, a synced private backup
(last three kept), and a synced temporary file atomically renamed over the target.
Unchanged apply creates no backup. Check and dry-run create no directories or locks.
Unix files use 0600 permissions; reused Unix locks require the current effective
owner and one link, refuse symlink swaps, and are restricted to 0600. Held locks
refuse a competing apply. Windows uses the user's directory ACL for privacy;
lock handles refuse reparse following and deletion. Setup assumes the selected
home and its directories are controlled by the user, without concurrent hostile
ancestor replacement.

Run `cargo test -p plangraph-cli` and
`cargo clippy -p plangraph-cli --all-targets -- -D warnings` from `rust/`.
The contract has portable Rust tests plus Unix-specific filesystem tests.
CI builds and tests this crate on Linux only; Windows CI does not compile or run it.
