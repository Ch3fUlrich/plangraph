# Rust host tools: disk hygiene

Build `pg-disk-hygiene` with `cd rust && cargo build -p pg-worker-core --bin
pg-disk-hygiene --bin pg-process-cwds --release`, using the environment's shared `CARGO_TARGET_DIR`.

`pg-disk-hygiene --repo PATH --root WORKTREES --pins PINS.json --dry-run`
reports an alarm at 85% used or below 30 GB available. At 88% used, `--apply`
reclaims ignored `target/` and `node_modules/` in registered clean worktrees
idle over six hours. It never removes worktrees or tracked source. Git probe
failures skip that tree. Freed bytes count only removed rebuildable files.
The main checkout, locked trees, pinned paths, `plangraph-md-sync`, keeper
`live-*` paths, active runs and process working directories are protected.
The mandatory pins file is a JSON array of absolute paths exported by md-sync;
missing or malformed protection data fails closed. Unknown run state fails closed.

The two shared cache roots `$HOME/.cache/plangraph/cargo-target` and
`$HOME/.cache/pg-target-po` can be reclaimed only with no build process running.
Process discovery fails closed; symlinked cache roots are refused.
Linux may hide cwds of non-dumpable user services even from their owner. Install
`pg-process-cwds` root-owned at `/usr/local/libexec/pg-process-cwds` with only
`cap_sys_ptrace=ep` (operator installation: `install -o root -g root -m 0755`
then `setcap cap_sys_ptrace=ep`). This read-only helper accepts no arguments,
reads process metadata, prints JSON and never starts children or mutates state.
`PG_PROCESS_CWD_READER` can select an absolute path to an equivalent authoritative
reader; its output is `[absolute_cwd_paths, build_running_boolean]`. The helper is
used only when direct discovery is denied. An absent/failed reader still blocks
cleanup; a denied cwd is never silently ignored, including a foreign or root-owned
proc entry. Inode ownership is not used to waive process protection. Inspect a dry run after install. Cache cleanup
is deferred while Cargo, rustc or other recognized build tools run. Build
processes must not be launched concurrently with a host maintenance apply run.

Every invocation with a usable state destination replaces
`~/.local/state/plangraph/disk-hygiene.json` and appends `disk-hygiene.ndjson`.
Collection failures write `alarm:true` and `error` and exit nonzero, replacing
stale healthy reports. `--state DIR` overrides the destination. An unwritable
state directory is a hard error. `--apply --dry-run` is refused.

Install the service and 15-minute timer under `~/.config/systemd/user/` and the
binary under `~/.local/bin`. Set `PG_REPO`, `PG_WORKTREES`, `PG_PINS` in
`~/.config/plangraph/disk-hygiene.env`. Review dry-run output before enabling.
Live deployment is separate from repository validation. The start admission
gate belongs to the spawner and is maintained separately.
