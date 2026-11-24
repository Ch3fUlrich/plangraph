# Rust host tools: disk hygiene

`host-sync --check-protoc` compiles a probe that imports
`google/protobuf/field_mask.proto` and prints its include status. It uses
`PROTOC` and `PROTOC_INCLUDE` when configured.

`host-sync --install-wsl-guard POLICY HOST` installs the WSL guard system units
and writes the configured floor for hosts with Windows memory placement.
See [the WSL rollout notes](operations/worker-disks.md#wsl-cache-guard).

NOTE Host-sync sessions. Add `host-sync --check-protoc` to the host setup check
and record its output on all three workers. On the affected worker, install the
well-known include files that match its system protoc and repeat the check.
Temporal builds use the SDK's vendored descriptor compiler. Verify the worker
build during the Dell push-gate probe with its normal shared Cargo target.

Build `pg-disk-hygiene` with `cd rust && cargo build -p pg-worker-core --bin
pg-disk-hygiene --bin pg-process-cwds --release`, using the environment's shared `CARGO_TARGET_DIR`.

`pg-disk-hygiene --repo PATH --root WORKTREES --pins PINS.json --dry-run`
reports an alarm at 75% used or below 5 GiB available. At 80% used or below
10 GB available, `--apply`
reclaims ignored `target/`, `rust/target/`, `node_modules/`, and nested
`__pycache__/` / `.pytest_cache/` in registered clean worktrees
idle over 24 hours. It never removes worktrees or tracked source. Git probe
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

On approved x86-64 Linux and WSL hosts, install the pinned compiler cache with
`plangraph setup apply --component sccache`. The installer checks both the
release archive and the executable before replacing `~/.local/bin/sccache`.
Inspect the installed binary with `plangraph setup check --component sccache`.

Spawner builds, pre-push preflight, Git hook installation and CI use
`~/.cache/plangraph/sccache` for compiled objects. Set
`PLANGRAPH_SCCACHE_CACHE_SIZE` in the host's build environment to change the
default `20G` cap. Start a fresh sccache server after changing the cap.
Self-hosted CI uses the host's installed binary through the shared Rust setup
action. Each runner without sccache uses the same missing-wrapper fallback.
`SCCACHE_BASEDIRS` can list the absolute roots of equivalent checkouts before
server startup to normalize paths in cache keys.
`sccache --show-stats` shows hits, misses and cache usage. With sccache absent,
the build environment warns once and Cargo continues without a wrapper.

The shared target cap defaults to 32 GiB per root. A valid
`PLANGRAPH_CARGO_TARGET_CAP_GB` value takes precedence over the host file
`cargo-target-cap-gb` in `$XDG_CONFIG_HOME/plangraph` (default
`~/.config/plangraph`). Both take a nonnegative GiB value. Under disk pressure
or above the cap, cleanup removes oldest idle incremental sessions and stale
`build/<crate>-<hash>` variants, keeping the newest compile unit and run unit per crate and profile.
Candidates must be idle over six hours; recent, unknown and sole variants survive.
The cap is a reclamation trigger, not a hard size limit: protected/newest data may
exceed it. Build dependencies and other cached artifacts remain rebuildable but
are retained. Each removal rechecks processes and eligibility; a live Cargo
process keeps only the target it uses: its cwd, an open file, its executable or `CARGO_TARGET_DIR` inside the target or its worktree. A build elsewhere on the host defers nothing.
Process discovery fails closed; symlinked cache roots and children are refused.
Linux may hide cwds of non-dumpable user services even from their owner. Install
`pg-process-cwds` root-owned at `/usr/local/libexec/pg-process-cwds` with only
`cap_sys_ptrace=ep` (operator installation: `install -o root -g root -m 0755`
then `setcap cap_sys_ptrace=ep`). This read-only helper accepts no arguments for cwd discovery and `--references` for reaper
process paths. It reads process metadata, prints JSON and never starts children.
`PG_PROCESS_CWD_READER` can select an absolute path to an equivalent authoritative
reader; its output is `[absolute_cwd_paths, build_running_boolean]`. The helper is
used only when direct discovery is denied. An absent/failed reader still blocks
cleanup; a denied cwd is never silently ignored, including a foreign or root-owned
proc entry. Inode ownership is not used to waive process protection. Inspect a dry run after install. Cache cleanup
is decided per path, never by a host-wide build flag. Below the cleanup floor the run
removes idle Cargo targets (worktree `rust/target`, scratch, `fc-*`) largest first until free space
is back above it; a target untouched for 15 minutes with no process, lease, pin or unfinished run
counts as idle. Each kept path is reported as `<path>: kept: <reason>`. An `--apply` run that ends
below the floor with `freed_bytes` 0 exits non-zero with an `ALERT` naming the five largest kept paths.

Every invocation with a usable state destination replaces
`~/.local/state/plangraph/disk-hygiene.json` and appends `disk-hygiene.ndjson`.
Collection failures write `alarm:true` and `error` and exit nonzero, replacing
stale healthy reports. `--state DIR` overrides the destination. An unwritable
state directory is a hard error. `--apply --dry-run` is refused.

Install the service and 30-minute timer under `~/.config/systemd/user/` and the
binary under `~/.local/bin`. Set `PG_REPO`, `PG_WORKTREES`, `PG_PINS` in
`~/.config/plangraph/disk-hygiene.env`. Review dry-run output before enabling.
Repeat `--root` for additional worktree roots. Configured worktree mounts and
existing state worktree roots are included automatically. Each filesystem is
compared with its own thresholds. An unavailable root raises the alarm while
the other filesystems are still swept. Set `PLANGRAPH_SPAWN_STATE` when records live
outside the default state directory.
Threshold options are `--alarm-free-bytes` (default `5368709120`, 5 GiB) and
`--cleanup-free-bytes` (default `10000000000`, 10 GB). Use
`--shared-target-cap-bytes` to override the configured cap for one invocation.
These options take byte counts. Alarm must be positive and no larger than
cleanup. JSON reports `free_bytes`, never a usage percentage.
Rule 7.1 requires at least 5 GiB free to start any writer/run on every host and
stopping at 2 GiB free. The spawner owns admission and runtime stopping;
this timer reports pressure and reclaims idle rebuildable state: stale `fc-*` fast-check
targets beside the shared target (Cargo markers still required, leased or pinned ones kept) and
registered job worktrees outside every `--root` whose head is in `origin/main`, tree clean and idle a
day (a pushed but unmerged head is reported and kept). A busy host over the cargo cap first drops
build variants idle for an hour; `--supervise` spawns end with `ALERT SPAWN <run> REFUSED: <reason>`
and a non-zero exit when a refusal lands after admission.
Live deployment is separate from repository validation.

The separate daily `pg-worktree-sweep.timer` invokes the report-only
`pg-worktree-sweep --reap`.
Install its service and timer, the binary, mint a mode-0600 unscoped `sweep`
token at `~/.config/plangraph/sweep.token`, run `deploy/bootstrap.sh` to create
`~/.config/plangraph/disk-hygiene.env` (with `PG_SWEEP_CLAIMS_READER`, `PG_URL`,
`PG_TOKEN_FILE`) and `~/.config/plangraph/pins.json` when the token file exists,
and inspect a dry run before enabling; see [pg-reap](pg-reap.md#live-merged-worktree-sweep)
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

For a dedicated orchestration checkout, add `--refresh-prs OWNER/REPO --notify
/absolute/path/to/notifier` to a single explicit `--repo PATH` sync invocation.
After each successful sync it sweeps all open `batch-ready` PRs, including drafts,
so a main update refreshes their heads. Config-only inspection requires `--sync`.
The existing sync protections still apply. Deployment must arrange invocation
on main updates; this change does not install an event listener or timer.

The same sweep is available as `pg-repo-sync refresh-prs --repo PATH --github
OWNER/REPO --notify /absolute/path/to/notifier`. It fetches main once, validates
listed PR heads against origin, and merges that main snapshot in disposable,
detached worktrees. Origin and OWNER/REPO must identify the same repository.
Forks are reported for operator handling. Git's checked-in merge attributes
resolve union paths; other conflicts add `needs-update`, report their paths to
the notifier's stdin, and never push. Each failed PR reports `ALERT #N ...` and
makes the command exit 1 while other PRs are still processed. Successful changed
heads use ordinary pushes with repository hooks active, triggering existing PR
CI with the host's existing credentials. No-change sweeps do not push. The
notifier is one executable, without shell evaluation; delivery failure exits 1.
Git writes retain normal hook/runtime behavior, without the inspection helper's
hook suppression or its query timeout. No credentials or Git config are changed.

Hook discovery follows each selected checkout's effective `core.hooksPath`
(including inherited and relative paths) and symlinked hook files into their
owning checkout. Targets outside `$HOME/code`, such as a dedicated
`plangraph-hooks` checkout, are included recursively and printed as `HOOK` lines.
Running service WorkingDirectory, ExecStart executable and FragmentPath
checkouts are also listed as `UNIT` lines; existing live-unit protection still prevents their automatic fast-forward.
Pinned and excluded checkouts are not inspected for hook targets.

Pre-push prints its own checkout HEAD before evaluating pushed refs. If its
locally fetched `origin/main` contains a missing commit older than 24 hours,
it refuses with a stale-checkout diagnostic and the `pg-repo-sync --repo PATH`
repair command. A cached upstream tip older than a day is fetched before
judging freshness;
an unsuccessful refresh refuses the push. Missing commit ages follow main’s
first-parent history, so recent merges of old topics do not cause false alarms.
An old HEAD with no missing upstream commits is current. Gate changes in a topic
branch take effect in the shared hook checkout after they merge into main and that checkout syncs.

## Fleet series history (Rust core)

`pg_worker_core::fleet_series` stores minute samples in
`<state>/fleet/<host>.ndjson` and attributes lost sessions only across adjacent,
reachable samples with an increasing OOM counter. Terminal run records receive a
host-specific `oom_kill_delta`, or null when coverage is unavailable.

Per host, append retains the newest 1,440 samples (24 hours at minute cadence),
limited further to 4 MiB. Reads seek directly to that byte tail and parse only the
newest complete retained rows, so legacy oversized histories do not cause an
unbounded scan. Older rows are discarded; summaries cover only retained data and
run attribution becomes unknown when its baseline has expired. An individual
sample exceeding 4 MiB is rejected without replacing history. Appends take the
existing nonblocking host lock and atomically replace the private history file.

This first slice supplies storage, summaries and run attribution. The reference
branch's collection module, sampler CLI and systemd service/timer remain open;
no timer or live-host action is installed by this slice.


### Package host readiness

Run `pg-host-readiness --project PATH --registry FILE --state DIR` on each
candidate. The registry's `self` entry sets the recorded host. The default
sandbox is `workspace-write`, matching the native Codex writer. The tool
checks configuration trust, login, CLI version, and sandbox startup in a
temporary directory. After startup succeeds, it checks commit access in a
temporary linked Git worktree with both Git directories named as writable
roots. The probes make no model call. Use `--scratch-root` for a
filesystem matching the intended worktree. Hooks and plugins whose trust cannot
be established cause a failed check.

In text mode the tool prints `READY` on success and `MISSING` with the check failure on exit
1. It writes both results to
`DIR/package-readiness/HOST/workspace-write.json`. Make each host's latest
record available under the placement command's state directory. `pg-package-host`
placement refuses missing, failed, future-dated, and older-than-120-second
package-readiness records and records for a different host or sandbox. Remote capacity samples come from the
same state directory's `fleet/HOST.ndjson` history.

The structured `--json` report and the saved record include `sandbox_ok` and
`sandbox_error`. Native placement excludes hosts with a failed probe and verifies
startup again on the selected host before creating a worktree. A startup
failure reports `sandbox_unavailable` with the probe diagnostic. The operator
can then review the bwrap AppArmor profile or the user namespace sysctl.

### Three-host writer and review rollout

Use `hosts.json` as the source for placement. Give each target its own host
entry, current main checkout in `repos.plangraph.root`, role-specific
reservations, and memory floor. List the repository IDs approved for SSH
transport in the registry's `public_repos` field. A workstation WSL entry uses
`os: linux`, the Windows SSH endpoint in `ssh_target`, and the distribution in
`wsl_distro`. Placement, readiness and status probes reach the guest
through `wsl.exe --exec pg-spawnd steps` with a JSON step plan on stdin, never a shell; install
`pg-spawnd` on the guest's `PATH`. Its paths use the distribution's Linux filesystem. Set
`placement_fallback: true` on the orchestration host to rank it after ready
workers. Among ready worker hosts, placement uses free memory above each
host's floor. The run record includes the selected host and placement reason.

After merge, install the matching spawner and `pg-host-readiness` binaries on
the three hosts and ensure noninteractive SSH can find them. Configure the
registry on each host so `self` names that host. Regenerate the status and
session projections with `python3 -m plangraph.fleet_sync genconfig write`,
then restart the affected spawner units. Run
`python3 -m plangraph.spawner.host_ready --json` on each placement origin.
Confirm each host reports a trusted checkout, matching main SHA, working
tools and login, a pre-push test interpreter (the checkout's `.venv`, else
`PREFLIGHT_PYTHON`, else `python3`, resolved as the hook does) with `pytest` and `xdist`, the probe process's umask and the `UMask` of the host's
session `.service` unit (when it has one) keeping the group and other write bits clear, and no failures. Refresh the `host_ready` readiness record before its
ten-minute freshness window expires, and the `package-readiness` record before
its 120-second window expires.

Submit a writer and a review from a placement origin and verify both run types
land on the remote Linux worker and on the workstation WSL. Record the run IDs
and selected hosts, then attach a 24-hour sample showing work on all three
hosts. Review requests send their exact base and head as a `diff_ref`.
Unpublished commits in approved public repositories are transferred in a
temporary Git bundle, so testing this path requires no push.

### Fleet worker liveness and maintenance

Run `pg-spawnd worker-liveness --deliver` from the
placement coordinator once per minute. It probes SSH and Tailscale for the
worker and workstation SSH entries in the fleet registry. Set
`PG_TAILSCALE_PEER` in a host's registry `env` if its Tailscale address
differs from the SSH hostname. The SSH result governs placement because
launch uses SSH. The recorded Tailscale result helps diagnose a link failure.

After two misses the worker is refused and `pg alerts` lists an open
finding with the affected run IDs. The probe combines the coordinator's
placed run records with the last available remote inventory. Recovery clears
the finding and records which runs to check. Placement continues to apply its
regular readiness checks after recovery.

An operator or router with coordinator state access can use
`pg-spawnd worker-maintenance HOST END_UNIX_SECONDS` to block placement
immediately. Misses during that window are recorded for host health without
paging routing. A successful probe at or after the end clears the placement
hold. A host that still fails remains unavailable. An inventory-only host
needs an active worker configuration before it can receive placement.

The versioned `pg-worker-liveness` service and timer use a coordinator
`worker-liveness.env` with
`PLANGRAPH_SPAWN_STATE`, `PG_URL`, `PG_TOKEN_FILE` and
`PG_HOST_LINK_SENDER`. Use a Keeper system or router principal and place
both updated binaries on the service PATH. The liveness command delivers
through the host-link watchdog outbox and retries pending events. Events
with a permanent payload refusal move to `watchdog/rejected`. Use one
coordinator timer for the fleet to avoid redundant SSH monitors.
