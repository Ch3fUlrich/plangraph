# Dell CI execution

`python3 tools/dell_ci_run.py --sha <40-character-sha>` selects every job
by default. The SSH login must start in the repository and have the CI
prerequisites preinstalled (including Docker, cargo, the Windows GNU Rust
target, protoc and Temporal 1.9.1). No Actions runner is installed.

Postgres uses a run-specific name, a private password and an ephemeral localhost
Docker port; cleanup removes only the container ID created by that job.
Temporal selects three ephemeral localhost ports (gRPC, HTTP and metrics).
If a port is taken before startup, the job fails rather than reusing a service.
Worktrees, scratch, Rust build targets and Temporal logs live under remote
`~/.cache/dell-ci/<id>` on disk, with cleanup attempted on every exit path.
Rust refuses execution with fewer than 20 GiB available on that filesystem,
reporting `insufficient ~/.cache/dell-ci space: requires at least 20 GB free`.
Records contain status, timing, exit codes and bounded 40-line, `$HOME`-scrubbed
stdout/stderr tails per job, with sensitive values redacted, even on failure or timeout.

The local gate evaluates suite results; it does not reproduce GitHub's
base-copy commit-trailer step or hosted setup actions. A setup failure can
prevent all jobs from starting; its record has a top-level reason instead.

## Isolation
Every PR-controlled command (including dependency installation) runs in bwrap.
A trusted network-enabled fetch stage reads requirements and Cargo lock files as
data only. It rejects requirement options, URLs and non-crates.io Cargo sources;
pip downloads wheels only, and cargo fetch uses synthetic manifests outside the
checkout, never PR manifests, configuration, setup.py, build.rs or scripts.
Each job gets private wheels and CARGO_HOME. Inside bwrap, pip installs with
`--no-index --find-links /scratch/wheels` and Cargo has `CARGO_NET_OFFLINE=true`.
User, PID and IPC namespaces, a fresh procfs and a cleared, allowlisted environment
hide host credentials, concurrent process environments and host home directories.
System directories and Rust toolchains are read-only; only a fresh per-job checkout
and scratch are writable binds. HOME is ephemeral; cargo, targets, venvs and caches
are private to each job. PATH never includes the host's `~/.local/bin`.
The trusted outer driver owns Docker creation/removal and passes PG_DSN and its
password as positional arguments;
no Docker socket is mounted. Temporal runs inside the sandbox.
Networking is isolated with `--unshare-net`: host and other-job listeners are
unreachable. Postgres uses a private Unix socket and SCRAM authentication, not a
published port; Temporal runs on the job's private loopback network.
Missing bwrap/user-namespace support fails closed; there is no unsandboxed fallback.
