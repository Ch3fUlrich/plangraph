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

## Workstation work drives

NOTE Workstation host session. Record root usage and inspect dry-run output from
the merged-worktree sweep and disk hygiene before running either in apply mode.
Record reclaimed bytes and the resulting WSL root usage.

NOTE Workstation host session. Mount a D: ext4 virtual disk for builds and
caches and an F: ext4 virtual disk for worktrees. Create a writable
`plangraph/worktrees` directory on each filesystem offered for worktrees. Set
`PLANGRAPH_WORKTREE_MOUNTS` in the spawner and feeder environment to the
colon-separated worktree mount paths. Admission chooses the eligible filesystem
with the most free bytes. The default floor is 5 GiB and 10% free. Set
`PLANGRAPH_DISK_MIN_FREE_PERCENT` to adjust the percentage.

NOTE Workstation host session. Point `PLANGRAPH_CARGO_TARGET_DIR` and package
cache environment variables at dedicated directories on the build mount.
Configure both mounts in registry `env.PLANGRAPH_WORK_MOUNTS` and point the
hygiene and sweep units at the configured worktree roots. Include
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
