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
