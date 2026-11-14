# Parallel CI isolation

On Windows, parallel test processes set `PG_SPAWND_PIPE_NAMESPACE` to a unique
per-test temporary directory name. The optional namespace accepts up to 80 ASCII
letters, digits, or hyphens and keeps the user name and user-only pipe DACL.
Normal invocations continue to use the per-user pipe.

The `ci-temporal` Rust helper runs a command with `PG_TEMPORAL_ADDR`, `SPIKE_ADDR`,
`TEMPORAL_TEST_ADDRESS`, and `TEMPORAL_ADDRESS` pointing to its own dev server.
It takes an OS-selected port, disables the UI, leaves HTTP and metrics on random
ports, and keeps logs in a private directory under `RUNNER_TEMP` (or the system
temp directory). Startup retries cover a lost port-allocation race. The helper
kills and reaps its server and prints the log tail when the command finishes.
It uses the same process and temporary-directory handling on Linux and Windows.
