# Disk admission and Cargo cache limits

Disk admission uses the production Python CLI/MCP path (`seat_spawn.admit` and
`overflow`), alongside RAM admission. `PLANGRAPH_DISK_FLOOR_GB` defaults to 5
(GiB); the checkout, configured `worktree_root`, and state tree filesystems must
all meet it. A refusal reports `disk_low` and measured GB and uses RAM's overflow
or queue mechanism. Remote readiness uses `disk_floor_gb` (default 5); the
receiving spawner also checks live disk before launching.

Every harness child, including writers and fixers, receives `CARGO_TARGET_DIR`
from `PLANGRAPH_CARGO_TARGET_DIR` (default `~/.cache/plangraph/cargo-target`).
Caller and per-worktree target overrides are replaced; children also receive
`CARGO_INCREMENTAL=0`. The cache cap is
`PLANGRAPH_CARGO_TARGET_CAP_GB` (default 12 GiB). Over-cap busy hosts refuse with
`cargo_target_cap`; idle hosts clear the cache before checking free space.
An independent one-second runtime sample reaps runs at the shared cap or below
2 GiB free (rule 7.1). Sampling permits transient overshoot between measurements;
it is not a filesystem quota. Building artifacts are never deleted; idle admission
reclaims the cache after all run leases close. A disk stop is terminal for the
whole dispatch, leaves provider health unchanged, and waits for process-tree
verification and lease release before collection returns.
Shared leases cover spawned processes and exclude cleanup, even before
the run record is saved (POSIX flock; Windows shared read handles).
Use a dedicated cache directory, never a source directory.
The Rust worktree sweep also removes legacy `rust/target` before removal.
