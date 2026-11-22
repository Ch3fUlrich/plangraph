# Disk admission and Cargo cache limits

Disk admission uses the production Python CLI/MCP path (`seat_spawn.admit` and
`overflow`), alongside RAM admission. `PLANGRAPH_DISK_FLOOR_GB` defaults to 5
(GiB). With `work_drives` configured, an unnamed local run launches from the
first configured path that is a Git checkout with enough free disk. Immediate
and queued admission both pass this checkout to the launcher. If no configured
path is a launchable Git checkout, admission reports that configuration error.
A run with `--cwd`, card `cwd`, `diff_ref.repo`, or `repo` is pinned, and its
checkout uses its configured floor or falls back to the first matching
filesystem floor. Both paths also check the Cargo target, run state, supervisor
tree, and worktree root filesystems. Before refusing `disk_low`, admission cleans idle rebuildable caches
(`shared_cargo_target`, its pool slots `<target>-pool/slot-N`, `~/.cache/pg-target-po`,
`~/.cache/pg-target-warm`, and idle worktree `rust/target` directories) once and re-measures free space; a
persistent breach records a `disk-hygiene.json` floor alarm (`alarm: true`).
A refusal reports `disk_low` and measured GB and uses RAM's overflow
or queue mechanism. Remote readiness uses `disk_floor_gb` (default 5) and
optional `work_drives`; the receiving spawner also checks live disk before launching.

Every harness child, including writers and fixers, receives `CARGO_TARGET_DIR`
from `PLANGRAPH_CARGO_TARGET_DIR` (default `~/.cache/plangraph/cargo-target`).
Caller and per-worktree target overrides are replaced; children also receive
`CARGO_INCREMENTAL=0`. The cache cap is
`PLANGRAPH_CARGO_TARGET_CAP_GB` (default 12 GiB, per slot; one value shared with the Rust pool). Over-cap busy hosts refuse a building role with
`cargo_target_cap`, which names the measured cache path; a `review` builds nothing and is exempt from
the cap at admission and during the run (the free-space floor still binds it); idle hosts clear the cache before checking free space. A run keeps the
host busy only while a recorded child or supervisor pid is alive (or it has survivors): ended,
`timed_out`, `stalled` and `queued` records, and `running` records whose recorded pids are all gone, do
not (ps marks the latter `stale`). Cleanup takes the exclusive cargo lease and waits up to
`PLANGRAPH_CARGO_CLEANUP_WAIT_S` (default 30) for live builds before it refuses with `busy or cleanup
unavailable`; it never deletes under a held lease.
The same reclaim (`host_ready.reclaim_cargo_target`) also prunes `cli-tests/<checkout hash>` builds to
the newest `PLANGRAPH_CLI_TESTS_KEEP` (default 3), none untouched for 24 h; `tools/preflight.py` runs it
before the pre-push hook builds (lease wait 5 s, then skipped), so the push cache stays under the cap.
An independent one-second runtime sample reaps runs at the shared cap or below
2 GiB free (rule 7.1). Sampling permits transient overshoot between measurements;
it is not a filesystem quota. Building artifacts are never deleted; idle admission
reclaims the cache after all run leases close. A disk stop is terminal for the
whole dispatch, leaves provider health unchanged, and waits for process-tree
verification and lease release before collection returns.
Shared leases cover spawned processes and exclude cleanup, even before
the run record is saved (POSIX flock; Windows shared read handles).
Use a dedicated cache directory, never a source directory.
The shared lock contract is a sibling file named `<target>.plangraph-lock`,
where `<target>` is the expanded, absolute shared target path. Both
`host_ready.py:cargo_lease` and `pg-disk-hygiene` use this exact name and POSIX
flock: runs hold shared leases; reclaim holds a nonblocking exclusive lease
through scanning and deletion. Hygiene reports and skips a root when its lease
is held. The lock file stays in place after release.

Hygiene resolves `PLANGRAPH_CARGO_TARGET_DIR` with the same home expansion and
absolute-path rules as `host_ready.shared_cargo_target`, defaulting to
`~/.cache/plangraph/cargo-target`; it also checks `~/.cache/pg-target-po`.
Its alarm and cleanup defaults are 5 GiB and 10 GB (decimal) respectively.
`pg-disk-hygiene` reclaims ignored `target`, `rust/target`, `node_modules`, and
nested `__pycache__` / `.pytest_cache` directories in eligible idle worktrees.
Other ignored data, tracked cache files, dirty trees and protection records
prevent reclaim. Worktree removal remains a separate `pg-worktree-sweep` step.

When every Cargo pool slot is leased, `plangraph fast-checks` waits up to 30 seconds
for a slot. It reports exhaustion if every slot stays busy.

Pre-push preflight also pins the shared target, disables incremental builds and
holds its shared lease while Cargo runs. Cap checks run before and after each
Cargo command. Native Codex launches pin the same target; briefs forbid private
targets. Continuous cap sampling in these two paths and exact-head fast-check
stamps remain open.

Hygiene additionally reclaims Cargo-marked private targets under /tmp (two levels)
and ignored, untracked rust/target directories in registered worktrees under
--root after 30 minutes without writes. This cleanup runs without disk pressure;
shared targets, pinned/live paths, active builds, process cwds, foreign ownership,
leases and symlinks prevent deletion. Worktree source dirtiness alone does not
protect ignored build artifacts in this narrower cleanup.
