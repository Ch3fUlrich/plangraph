# Worker disk rollout

## Dell data drive

NOTE Dell host session. Verify that `/mnt/data` is the ext4 data mount and is writable
by the worker account. Set `env.PLANGRAPH_WORK_MOUNTS` to `/mnt/data` in the host
registry so host readiness checks the mount and its free-space floor. Preserve the
checkout on its current filesystem and point `state_dir` and the repository's
`worktrees_root` to dedicated directories on the mounted disk.

NOTE Dell host session. Quiesce writers and seats. Save the existing registry and
`dell_writer.remote.sh` with timestamped backups. Move existing state, scratch,
caches and archive directories aside as rollback copies before copying data to the
new locations. Keep Git worktree registrations valid by moving registered worktrees
with Git or keeping their original paths through a symlink. Point the spawner's
`PLANGRAPH_SPAWN_STATE`, `PLANGRAPH_CARGO_TARGET_DIR`, `TMPDIR`, `CARGO_HOME`,
`PIP_CACHE_DIR`, `npm_config_cache`, and `UV_CACHE_DIR` at their respective
directories on the data mount. Apply the same environment to the Dell wrapper
for writers and seats, and point the archive at its data-drive directory.

NOTE Dell host session. Record the backup paths and run one lightweight probe.
Record the actual worktree, cargo target, scratch, package caches, archive and
run record paths. Measure root and temporary filesystem usage before and after
ten probe runs and include the readiness mount output with the results.

NOTE Dell and workstation host sessions. Put the data-drive target in the host environment
as `PLANGRAPH_CARGO_TARGET_DIR` (a real directory, never a symlink: the fast-check pool refuses
a symlinked pool), in the file read by the spawner and the plangraph units and in the
environment `fleet-launch` runs under. The spawner, `plangraph fast-checks`, preflight and
`fleet-launch` resolve that one setting; `fleet-launch` hands it to the session as
`CARGO_TARGET_DIR` unless the caller set one, and no launcher creates
`~/.cache/plangraph/cargo-target` while it is set. A worktree under `.claude/worktrees` holds no
`rust/target`; `pg-disk-hygiene --apply` removes one only when it is eligible under the cleanup rules in
`docs/operations/spawner-disk-admission.md` (ignored and untracked, not pinned or live, no running build,
idle long enough). Cargo targets under
`~/.cache/cargo-target` and `~/.cache/pg-target-warm` untouched for 24 hours are removed by
`pg-disk-hygiene --apply`, and the disk watch alert at 85% used names the three largest
directories of the host.

## Workstation work drives

NOTE Workstation host session. Record root usage and inspect dry-run output from
the merged-worktree sweep and disk hygiene before running either in apply mode.
Record reclaimed bytes and the resulting WSL root usage.

NOTE Workstation host session. D:, F:, G:, and H: are all approved for WSL work
data. Provision ext4 virtual disks on the drives in use. Start with builds and
caches on D: and worktrees on F:. Give each virtual disk a fixed WSL mount path,
such as `/mnt/work-d`, `/mnt/work-f`, `/mnt/work-g`, or `/mnt/work-h`.
Create a writable `plangraph/worktrees` directory on each mount offered for
worktrees and put those colon-separated paths in `PLANGRAPH_WORKTREE_MOUNTS`.
Keep the spawner environment and the feeder's `plangraph.env` (or the file
named by `PLANGRAPH_ENV_FILE`) in sync. Admission chooses the eligible
filesystem with the most free bytes. The default floor is 5 GiB and 10% free. Set
`PLANGRAPH_DISK_MIN_FREE_PERCENT` to adjust the percentage. Size each virtual
disk to leave headroom on its backing Windows drive and check usage on both
layers so neither reaches 100%.

NOTE Workstation host session. Point `PLANGRAPH_CARGO_TARGET_DIR` and package
cache environment variables at dedicated directories on the build mount.
Configure every provisioned work mount in registry `env.PLANGRAPH_WORK_MOUNTS`
and point the hygiene and sweep units at the configured worktree roots. Include
`PLANGRAPH_WORKTREE_MOUNTS` in their environment so the sweep can match work
trees to the corresponding run records. Verify
`pg-spawnd host work-disk`, the recorded paths and disk usage, refusals at the
floor, and admission of 15 parallel writers.

## Reclamation and reporting

NOTE Dell and workstation host sessions. Install and enable the 30-minute
disk-hygiene user timer and the merged-worktree sweep timer. Configure their pin,
claims, run state and root paths. Inspect the dry run and record a real cleanup
with freed bytes. Give the heartbeat collector the newest `disk-hygiene.json`
report for that host and surface its `alarm` or failure.

The hygiene report's `free_bytes` is the lowest final free-space sample. The
`devices` rows show start and end samples and reclaimed or projected bytes for
each filesystem. The `kept` rows give the paths and reasons for retained caches.
In apply mode, zero bytes reclaimed below a device's cleanup floor queues a
Keeper routing alert and makes the run fail. Positive reclamation that leaves
a device below its alarm floor queues an alert and allows the run to succeed
if no other failure occurred. Each alert lists up to five kept paths with their
reasons. The stall watchdog retries delivery from the private outbox until
Keeper acknowledges the event ID. Dry runs publish the report without queuing
pressure alerts.

The timer records `started_at`, `time_to_first_removal`, and
`first_removal_deadline_missed` alongside the device report. Use the matching
invocation's NDJSON row to assess the 60-second first-removal deadline during
an observed overlapping build. Keep the live evidence gate open until a
pressured overlapping invocation is measured.

The timer emits an alert when a monitored filesystem is used strictly above 85%. The alert
lists up to five configured directories by allocated bytes.
`disk-watermarks.json` keeps one row per crossed device, keyed by device, with the time it was
measured. Diagnostics start after reclamation and share the pass deadline.
A completed measurement can be reused for the same root when it has no nested
diagnostic roots. If the deadline interrupts the scan, the previous rows stay
available. A sample at or below 85% drops its row on a completed pass. The flow
watchdog reads these rows locally (see `docs/flow-watchdog.md`).

## Build caches

NOTE Worker host sessions. Set `RUSTC_WRAPPER=sccache` and an explicit
`SCCACHE_CACHE_SIZE` in the host environment read by the spawner and
fleet launcher. Set `SCCACHE_DIR` on the build disk. Size the cache so that it
and active Cargo targets leave the configured free-space floor. Check the
effective wrapper, cache directory and cap with a small spawned Cargo build.

NOTE Watchdog host session. Give each host's `disks` array one `{device, used_percent,
consumers}` sample per configured work mount, root and temp filesystem: `device` is the `st_dev`
of the sampled path, `consumers` the five largest directories on that filesystem. The legacy
`work_used_percent` array names no filesystem and only raises a bare alert; a collector that still sends it must send it on every row (`[]` when there are no work mounts), because an omitted list does not end an alert. On the single-disk worker, inspect the
worktree and cache reports, reclaim eligible data and measure usage below 85%.

## WSL cache guard

NOTE Host-sync session. Install a root-owned `pg-spawnd` in `/usr/local/bin`.
Run `host-sync --install-wsl-guard POLICY HOST` as root on the selected host
using the fleet launch policy and host name. The command installs the system
unit files only for entries with `memory_source = "windows"` and writes
`PG_WSL_FREE_MB` in `/etc/plangraph/wsl-cache-guard.env` from the optional
`cache_guard_min_free_mb` placement field, which defaults to 20480.

NOTE Workstation host session. Check Windows interop from the system service.
Enable the timer and run the 30-minute Cargo and pytest probe. Confirm Windows
free physical memory stays above 12 GiB and the launcher admits the workstation
on every probe. Inspect the guard's before and after samples, then retire the
temporary timer on the orchestration host once the replacement is verified.
