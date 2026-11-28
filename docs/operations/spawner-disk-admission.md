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

Writers, fixers, and reviews without a worker lease receive `CARGO_TARGET_DIR`
from `PLANGRAPH_CARGO_TARGET_DIR` (default `~/.cache/plangraph/cargo-target`).
Spawned builds also use host-configured cache variables. See the sccache
settings in [Worker disk rollout](worker-disks.md).
Caller and per-worktree target overrides are replaced; children also receive
`CARGO_INCREMENTAL=0`. The cap uses the first valid nonnegative GiB value from
`PLANGRAPH_CARGO_TARGET_CAP_GB` or the host file `cargo-target-cap-gb` under
`$XDG_CONFIG_HOME/plangraph` (default `~/.config/plangraph`). It defaults to
32 GiB per target or pool slot, above the measured 20.9 to 25.9 GB of a cold
full-workspace debug test build. Post-run trimming targets 75% of the cap by
evicting eligible [idle variants](../host-tools.md#rust-host-tools-disk-hygiene), oldest first.
If the target is still above the cap, trimming can remove Cargo-marked debug
profiles. Over-cap busy hosts refuse a building role with
`cargo_target_cap`, which names the measured cache path. Native writer reviews use
a disposable Cargo target alongside the exact-head checkout on a worker. The
free-space floor applies to reviews, which are exempt from the shared cap. Idle hosts clear the shared cache before
checking free space. A run keeps the
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
flock. Runs hold shared leases. Timer ranking takes an exclusive lease for
each slice, and the selected candidate keeps its lease through the final proof
and removal. Hygiene reports a held lease and keeps the candidate. The lock
file stays in place after release.

Hygiene resolves `PLANGRAPH_CARGO_TARGET_DIR` with the same home expansion and
absolute-path rules as `host_ready.shared_cargo_target`, defaulting to
`~/.cache/plangraph/cargo-target`; it also checks `~/.cache/pg-target-po`.
Its alarm triggers at 75% used or under 5 GiB free. Cleanup starts at 80% used
or under 10 GB (decimal) free.
`pg-disk-hygiene` checks ignored `target` and `node_modules` roots and nested
`__pycache__` and `.pytest_cache` directories in eligible idle worktrees.
Tracked cache files and protection records
prevent reclaim. Hygiene also removes detached `*-fast` worktrees after
two hours without writes, including on disks above the cleanup threshold.
It checks process references again before removing a tree and requires its
commit to remain reachable from a Git ref.

When every Cargo pool slot is leased, `plangraph fast-checks` waits up to 30 seconds
for a slot. It reports exhaustion if every slot stays busy.

### Fast-checks wall time and the slot 0 lease (measured 2026-11-27/28)

Pool slot 0 is the primary target, which is also the shared writer target
(`pool_slots`, `cargo_pool.rs`). `acquire_slot` takes the first slot whose
`<slot>.plangraph-lock` it can lock `LOCK_EX`, so every `fast-checks` run on an idle pool
holds slot 0 exclusively from its first to its last cargo step
(`fast_check_target.rs::resolve_cargo_target`, lease json `run_id: fast-checks`,
`slot 0`). `writer_lease` (`cargo_cache.rs`) takes `LOCK_SH` on the same file, so a writer
launch or resume fails with `CARGO_TARGET_DIR: shared cache is being reclaimed` for the
whole run. The exclusive hold is needed for the target's artifacts only while a cargo
command runs; it is not needed between steps, and a pool slot above 0 would not collide
with writers. A `fast-checks` that is stopped keeps the lock: on the workstation a run
with a stopped (`T`) cargo child held slot 0 for 2 h 55 min.
The 30 s slot wait is never the cost: `cargo-pool-lock-waits.jsonl` shows 0 s of
Cargo lock wait on every build, and no run log shows a slot wait.

Wall time per run (workstation, 32 cores, no sccache; selector output decides the size):

| Selector | Wall | Clippy | Test compile | Test run | Other |
| --- | --- | --- | --- | --- | --- |
| no Rust (`rust=false`) | 33 to 96 s | 0 | 0 | 0 | scrub 15 to 26 s, affected tests 23 to 51 s |
| `rust=true full=false`, few crates | 583 s | 152 s (26%) | 367 s test step (63%, compile and run not split) | | 54 s |
| `rust=true full=false`, `pg-spawnd` | above 2400 s (timeout) | 336 s | 939 s | 965 s | 64 s |
| `rust=true full=true` | 1915 to 2131 s | 10 to 15% | 44 to 45% | 37 to 42% | 4% |

On dell (12 cores) a full run is 1265 to 1665 s and a `pg-spawnd` run 660 to 696 s,
with 18 crates compiled and 250 to 310 s of test runtime. "Test compile" is the test
step wall minus the sum of the `finished in` times of its test binaries. Four
`pg-worker` test binaries use 124 s, 137 s, 63 s and 59 s of runtime (`admission_policy_guard`,
`admission_policy_guard_probes`, `e2e`, `process_crash`). A full run prints 471 to 962
`Compiling` lines, with each local crate compiled up to 9 times (clippy, test and build
profiles). The warm shared target of the workstation holds 1.4 GB, so every run starts
nearly cold. sccache is not installed on the workstation or on dell. The 300 s wall budget is
now a receipt line, not a failure (`budget overrun wall_ns=... limit_s=300`), so the
overrun does not refuse a green head; it only measures the host.

Pre-push preflight also pins the shared target, disables incremental builds and
holds its shared lease while Cargo runs. Cap checks run before and after each
Cargo command. They count allocated blocks once per file inode. A non-Rust
lane push with a current cached policy executable stages it under a read
lease without invoking Cargo. Native Codex launches pin the same target; briefs forbid private
targets. Continuous cap sampling in these two paths and exact-head fast-check
stamps remain open.

Worktree cache cleanup requires a clean worktree idle for over 24 hours.
Under pressure the timer can reclaim Cargo-marked private targets in approved
cache and scratch locations and ignored, untracked `rust/target` directories
in registered worktrees after 30 minutes without writes. Pins, live run records,
process references, foreign ownership, leases and mount boundaries protect
individual candidates. Admission and post-run trimming retain their existing
host-wide build checks.

Pool trimming can keep a shared debug profile and evict a stale build unit
within it. The timer rechecks the newest unit before removal. A slot total
cut short by the deadline is treated as incomplete unless the bytes already
observed exceed the cap.

## Dell data-drive handoff (D-5122)

`SKIPPED plangraph:DELL-DATA-DRIVE` in batch84-disk. The fleet host policy and
`dell_writer.remote.sh` live outside the Plangraph checkout used for this batch.
The batch review directory holds the skip reason in `STATUS` and the
repository-only follow-up `DELL-DATA-DRIVE-POLICY` in `slices.json`.

The follow-up covers registry and launcher paths for worktrees, writer and seat
scratch, Cargo, pip, npm and uv caches, run records, and the OpenCode archive.
It also covers the `/mnt/data` entry in the D-5118 disk check and a per-path
backup and rollback mapping. Deployment evidence includes the backup inventory,
one probe that shows the resolved paths, and ten runs with disk usage readings
for `/` and `/tmp` before and after.
