# Disk admission and Cargo cache limits

Disk admission uses the production Python CLI/MCP path (`seat_spawn.admit` and
`overflow`), alongside RAM admission. `PLANGRAPH_DISK_FLOOR_GB` defaults to 5
(GiB); the checkout, configured `worktree_root`, and state tree filesystems must
all meet it. A refusal reports `disk_low` and measured GB and uses RAM's overflow
or queue mechanism. Remote readiness uses `disk_floor_gb` (default 5); the
receiving spawner also checks live disk before launching.

Every harness child, including writers and fixers, receives `CARGO_TARGET_DIR`
from `PLANGRAPH_CARGO_TARGET_DIR` (default `~/.cache/plangraph/cargo-target`).
Caller and per-worktree target overrides are replaced. The cache cap is
`PLANGRAPH_CARGO_TARGET_CAP_GB` (default 12 GiB). Over-cap busy hosts refuse with
`cargo_target_cap`; idle hosts clear the cache before checking free space.
Shared leases cover spawned processes and exclude cleanup, even before
the run record is saved (POSIX flock; Windows shared read handles).
Use a dedicated cache directory, never a source directory.
The Rust worktree sweep also removes legacy `rust/target` before removal.
