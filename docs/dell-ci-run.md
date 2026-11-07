# Dell CI execution

`python3 tools/dell_ci_run.py --sha <40-character-sha>` selects every job
by default. The SSH login must start in the repository and have the CI
prerequisites preinstalled (including Docker, cargo, the Windows GNU Rust
target, protoc and Temporal 1.9.1). No Actions runner is installed.

Postgres uses a private password and an ephemeral localhost Docker port.
Rust build targets and Temporal logs live in disposable job directories.
Rust refuses execution with fewer than 2 GiB available on `/tmp`.
Records contain status and timing, not captured command output or credentials.

The local gate evaluates suite results; it does not reproduce GitHub's
base-copy commit-trailer step or hosted setup actions. A setup failure can
prevent all jobs from starting; its record has a top-level reason instead.
