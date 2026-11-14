# Fleet code sync

Run `python3 -m plangraph.fleet_sync release record` after fetching origin/main.
The newest of the last 50 locally known first-parent main commits with successful required branch checks is
recorded privately and atomically in `~/.local/state/plangraph/release.json`.
The CLI queries `gh api`; missing protection or required checks fails closed.
`release show` prints the record. No checkout is changed.
The Python `record(max_commits=50)` API can configure the scan window; scanning
stops at the first green commit and fails closed if none is found in the window.
The `git rev-list --first-parent` scan excludes merged PR heads; merge commits
on main are eligible, not their second-parent commits.
Check results are selected by completed/started (or status updated/created)
timestamp, not API response order; equal-time conflicting results fail closed.

`genconfig write --registry PATH` derives ps-hosts.json and overflow-hosts.json
under `$XDG_CONFIG_HOME/plangraph` (default `~/.config/plangraph`). Each file is
atomically replaced; the pair is not a transaction. `genconfig check` exits 1
on semantic drift, including hand edits. Both print a one-line summary.
The default registry is that directory's hosts.json. SSH destinations must resolve
through SSH configuration or `ssh_target`. Set optional `repos.plangraph.live_dir` to the pinned live
checkout; no fallback to a developer checkout is allowed. Local entries are
excluded from remote configs; keeper entries fail closed. Windows SSH hosts
are ps-only. Overflow retains a deny-by-default empty public_repos allowlist.

Generated Linux overflow host entries include `floor_mb`, copied from the
registry's `caps.mem_floor_mb`, for `host_ready.check_host`. Overflow admission
reads the floor and role reservation directly from the registry, overwrites the
generated floor, and fails closed when registry caps are missing or malformed.
Regenerate the configs after changing that registry value; generated ps-host
entries do not include RAM admission fields.

`drift --registry PATH` prints host, live SHA, release SHA, commits behind.
`drift --json` emits an array. Missing objects or unreachable transports produce
`unknown`, never a fabricated zero; drift always exits 0. Counts use local Git
objects, so fetch the release and host commits first.
Routerctl calls `python3 -m plangraph.fleet_sync drift`; its integration lives in
the private data repository.

## Advance and rollback (slice 2)

`advance --sha SHA --hosts host-a host-b --go D-NNNN [--dry-run]`
uses the same optional `--registry PATH`. The first host is the canary; hosts
are processed serially and the first failure stops the rollout (exit 1).
SHA must exactly match release.json. D-825 requires a router GO reference for
each SHA; retain that requirement until 10 clean live runs. This command records
the supplied decision reference, not an independent verification of router approval.

Set `repos.plangraph.live_dir` to an absolute `<live_root>/current`, outside
an absolute `worktrees_root`; local paths are resolved to reject symlink overlaps.
Generated consumer configs continue to use that path. Releases
are detached clones at `<live_root>/releases/<sha>`, independent of worktree cleanup.
Configure `health_checks` with three argv arrays: `doctor` (spawner doctor),
`whoami` (keeper whoami), and `spawn` (one dry-run spawn). These commands run from
the candidate checkout before switching and from current after switching; spawn
runs only once, after switching. Commands
are injected configuration: advance refuses spawn argv without `--dry-run`.
No shell expansion occurs for health commands; output is suppressed.

POSIX current is atomically replaced as a symlink. Windows uses an atomically
replaced `current.pointer` file containing the absolute release path; Windows
launchers must resolve that pointer (ordinary path consumers need an adapter).
Any prepare/health/switch/prune failure restores the previous target and marks the
host FAILED. Earlier successful hosts are not reverted. Transport loss is FAILED;
inspect the host before retrying because acknowledgement may have been lost.
For an intentional rollback, record the previously approved release and obtain
a fresh GO, then advance to it. Do not hand-edit generated host configs.

Pruning retains three releases, always including current and previous.
`--dry-run` validates inputs and prints each host plan without contacting hosts,
writing logs, or changing files. Actual runs append JSON step/status records to
`~/.local/state/plangraph/fleet-sync.log`, excluding commands, env and error text.
An exclusive controller lock prevents concurrent advances from the same controller;
an exclusive live-root lock also prevents different controllers interleaving on a host.
After a crash, inspect the rollout before manually removing a stale lock
(`~/.local/state/plangraph/fleet-sync.lock` or `<live_root>/.advance.lock`).
Selected ps-only hosts, missing health checks, and unknown host ids are refused
before contacting any host (exit 2, one diagnostic line per problem).

Each host may set `ssh_target` for advance, drift, and generated ps/overflow
SSH destinations (for example a user-qualified SSH alias); when omitted, they
use the host id. Logs and selection still use the id. Regenerate consumer
configs after changing the override.
For fresh release health checks, the local registry entry's optional `env.PATH`
is the complete executable search path for doctor, including installed `pg-spawnd`
and harnesses. Doctor selects `PLANGRAPH_HOST_NAME` or registry `self`, resolving
ids/aliases case-insensitively. No build artifacts in a release tree are needed.
Windows registry `env.Path` is accepted case-insensitively. A present but unusable
registry reports `host_path: fail`; an unmatched identity is named explicitly.
Doctor restores the inherited PATH after its checks, including on exceptions.

## Keeper lazy-import review disposition

The admin import LOW does not apply: `src/plangraph/keeper/cli.py:308`
already imports `cli_admin` only inside `main`'s admin branch. The oversight
import LOW does not apply: `src/plangraph/keeper/cli_oversight.py:166-167`
already imports `db` and `oversight` inside `cmd_oversight_check`. The review's
`cla_oversight.py` refers to `cli_oversight.py`; no `cla_oversight.py` exists.
The fresh-interpreter guard covers help/whoami without `fcntl`, then real
admin init/verify and oversight dispatch. No keeper source change is needed.

Fleet-sync schema/example tests resolve fixtures relative to their test file
and exercise reads after changing to an unrelated directory. The imports at
`tests/test_fleet_sync.py:4` (`Path`) and `tests/test_fleet_sync.py:9` (`drift`)
predate this PR.

## Keeper-aware production main fast-forward

`python3 -m plangraph.fleet_sync main-ff --repo CHECKOUT --keeper-state STATE`
fetches origin/main and fast-forwards a clean attached main only. Before a move,
it compares the keeper's loaded SHA with the target using static import closure
from the existing keeper package at both revisions. Existing changed/deleted
keeper files and imported repository modules hold the move, including lazy,
relative and literal import-module dependencies. Unimported additions and
unrelated spawner changes pass; no loaded-state file means no running keeper.
Stale PID/start-token state is ignored; unreadable state or failed analysis holds.
`--no-fetch` checks locally known refs for offline fixture/manual diagnosis.

A hold exits 3 and prints one line:
`HELD keeper code changed: <files> loaded <sha> target <sha>`.
Branch/dirt/non-fast-forward refusals exit 2; success/current exits 0. The last
line is stored privately as `plangraph-main-ff.status` in the checkout's Git dir.
Existing `drift` text output includes that line for local hosts; `drift --json`
adds `main_ff` when a status is present. No status file preserves earlier output.

`deploy/systemd/plangraph-main-ff.{service,timer}` are uninstalled hourly templates.
Adapt their paths and supply `main-ff.env` with PLANGRAPH_MAIN_TREE and
PLANGRAPH_KEEPER_STATE (the DB filename plus `.loaded.json`; see the
[keeper state contract](keeper/main-fast-forward.md)). Restart the keeper from
known code to publish its state before activating the guarded job. Stop/replace
any prior unguarded updater rather than running both. No live config or unit is
installed by this package. The named earlier main-ff script/routerctl sources
were absent from this checkout; this command and the existing fleet drift
surface provide the in-repository implementation and status integration.

The main-ff guard now rechecks keeper state immediately before merging. Missing
state permits a move when conclusive probes show no keeper unit/process. Both user and system
unit scopes and standalone keeper processes are checked; a running keeper without
state holds with a restart/path diagnostic. Bus/timeouts and inconclusive probes
hold with a named cause, while missing systemctl/not-found units alone do not. `--keeper-unit` defaults to the repository's existing
`plangraph-keeper.service`. See the keeper state contract for bootstrap, dirty or
non-Git provenance, exact runtime data dependencies, and manual-update limits.

Process detection now recognizes interpreter module and keeper-file launch
targets on POSIX/Windows, excludes editors/probes and uses `ps` instead of
requiring `pgrep`. Positive keeper detection takes precedence over a prior unit
error in the HELD diagnostic. Headless user-bus failures and unavailable process
tools remain inconclusive and HELD; no-process alone does not prove an unreadable
user unit absent. See the state contract for the verified launch inventory.
