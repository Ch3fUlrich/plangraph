# Rust host tools: disk hygiene

Build `pg-disk-hygiene` with `cd rust && cargo build -p pg-worker-core --bin
pg-disk-hygiene --bin pg-process-cwds --release`, using the environment's shared `CARGO_TARGET_DIR`.

`pg-disk-hygiene --repo PATH --root WORKTREES --pins PINS.json --dry-run`
reports an alarm below 5 GiB available. Below 10 GB available, `--apply`
reclaims ignored `target/`, `rust/target/`, `node_modules/`, and nested
`__pycache__/` / `.pytest_cache/` in registered clean worktrees
idle over six hours. It never removes worktrees or tracked source. Git probe
failures skip that tree. Freed bytes count only removed rebuildable files.
The main checkout, locked trees, pinned paths, `plangraph-md-sync`, keeper
`live-*` paths, active runs and process working directories are protected.
The mandatory pins file is a JSON array of absolute paths exported by md-sync;
missing or malformed protection data fails closed. Unknown run state fails closed.

The shared cache roots are `PLANGRAPH_CARGO_TARGET_DIR` (default
`$HOME/.cache/plangraph/cargo-target`, with home expansion and absolute-path
resolution) and `$HOME/.cache/pg-target-po`. They can be reclaimed only with no
build process running and a nonblocking exclusive lease on the sibling
`<target>.plangraph-lock` file. Spawner runs hold shared leases on that same
file; cleanup skips a leased root and holds its exclusive lease through scanning
and deletion. The lock file stays in place after release.
The shared target cap defaults to 10 GB per root. Under disk pressure or above
that cap, cleanup removes oldest idle incremental sessions and stale
`build/<crate>-<hash>` variants, keeping the newest compile unit and run unit per crate and profile.
Candidates must be idle over six hours; recent, unknown and sole variants survive.
The cap is a reclamation trigger, not a hard size limit: protected/newest data may
exceed it. Build dependencies and other cached artifacts remain rebuildable but
are retained. Each removal rechecks processes and eligibility; a live Cargo
process anywhere defers cleanup, including when its cwd is outside the target.
Process discovery fails closed; symlinked cache roots and children are refused.
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
Threshold options are `--alarm-free-bytes` (default `5368709120`, 5 GiB),
`--cleanup-free-bytes` (default `10000000000`) and `--shared-target-cap-bytes`
(default `10000000000`); options take byte counts (10 GB is decimal). Alarm must be positive and no
larger than cleanup. JSON reports `free_bytes`, never a usage percentage.
Rule 7.1 requires at least 5 GiB free to start any writer/run on every host and
stopping at 2 GiB free. The spawner owns admission and runtime stopping;
this timer reports pressure and reclaims idle rebuildable state.
Live deployment is separate from repository validation.

The same timer also invokes `pg-worktree-sweep --daily --apply`. Install that
binary and configure `PG_SWEEP_CLAIMS_READER`; see [pg-reap](pg-reap.md#live-merged-worktree-sweep)
for live protection authority, restore records, and remote-deletion opt-in.

## pg-repo-sync

`pg-repo-sync` discovers direct children of `$HOME/code` with a `.git` entry,
including symlinks. Missing HOME or code directory is silent success. Repeat
`--root DIR` or `--repo PATH` to select checkouts; explicit labels stay as passed.
It fetches origin and silently fast-forwards a clean default branch. Alerts exit 1
and use only `detached`, `other-branch:NAME`, `tracked-changes`, `diverged`, or
`fetch-failed`, including defensive probe failures. No timer is installed here.

`--check-config --host ID --allow-name NAME --allow-email EMAIL` checks local
`core.bare` and identity overrides without writes; repeat allow flags as needed.
Absent overrides inherit the host identity. Drift reports host, path and escaped
values. Add `--sync` to also sync. All inherited `GIT_*` variables are removed.
Explicit paths require their own `.git` and matching Git top level, so a nested
path cannot fetch, merge or inspect config in its parent repository.
Every Git and systemd query is bounded to 60 seconds (lower with
`PG_REPO_SYNC_TIMEOUT_SECS`, minimum 1). Sync fetches origin with prune on every
run, regardless of disk usage; there is no `--apply` mode in this tool.
Running system and user service WorkingDirectory paths (including descendants
of a checkout) protect it from sync. Missing or unreadable unit information
fails closed with a `SKIP` report. Config-only inspection remains read-only.
Repeat `--exclude PATH` to skip a checkout (canonical paths match symlink aliases),
or create `.repo-sync-pin` in it. Both report `SKIP` before any Git operation.
No sync timer is installed or enabled by this change; deployment remains separate.
Systemd's optional-directory `!` marker is decoded; a missing optional directory
cannot protect a checkout. User-manager `~` resolves to HOME; unresolved system
manager homes fail closed. Unreadable unit protection also returns exit 1.

Hook discovery follows each selected checkout's effective `core.hooksPath`
(including inherited and relative paths) and symlinked hook files into their
owning checkout. Targets outside `$HOME/code`, such as a dedicated
`plangraph-hooks` checkout, are included recursively and printed as `HOOK` lines.
Running service WorkingDirectory checkouts are also listed as `UNIT` lines;
existing live-unit protection still prevents their automatic fast-forward.
Pinned and excluded checkouts are not inspected for hook targets.

Pre-push prints its own checkout HEAD before evaluating pushed refs. If its
locally fetched `origin/main` contains a missing commit older than 24 hours,
it refuses with a stale-checkout diagnostic and the `pg-repo-sync --repo PATH`
repair command. The hourly sync owns fetching; an old HEAD with no missing
upstream commits is current. Gate changes in a topic branch take effect in the
shared hook checkout after they merge into main and that checkout syncs.
