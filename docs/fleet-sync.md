# Fleet code sync

## Production checkout guard and audit

`python3 -m plangraph.fleet_sync check-checkout --repo CHECKOUT --state-dir STATE`
checks HEAD against `origin/HEAD`'s default branch and examines staged/unstaged
tracked changes. Use `--default-branch main` if the symbolic default is absent.
Detached HEAD, the wrong branch, unavailable refs, and local default-branch
commits not merged into `origin/<default>` alarm. Behind-only main is allowed.
Skip-worktree and assume-unchanged flags alarm as hidden tracked files: a
production checkout must not hide changes from inspection. Untracked files do
not alarm. This check never fetches, resets, switches or cleans the checkout.

Delivery uses the existing live-follow keeper-alert interface:
`PLANGRAPH_ALERT_CMD` is parsed as argv and receives the message as its last
argument. Configure the deployed keeper alert handler, with no hardcoded fleet
session address. A missing/unexecutable handler or failed delivery exits nonzero;
the unit failure remains visible. State is private, per checkout and locked:
one alarm on healthy-to-bad, one clear on recovery, and at most one successful
reminder per hour while bad. A failed delivery is not checkpointed and retries.
Unreadable or invalid state resets to a healthy/unsent baseline so bad checkouts
still alarm; an acknowledged send atomically replaces that state and resumes dedupe.
Healthy checks require an available alert command too, so broken alarm
configuration is not silent.

`deploy/systemd/plangraph-prod-checkout.{service,timer}` are uninstalled templates
with a five-minute interval. Adapt the checkout, Python environment and default
branch, and supply the required `prod-checkout.env` with `PLANGRAPH_ALERT_CMD`
before operator installation. This package does not install or start units.

`python3 -m plangraph.fleet_sync install-audit --repo CHECKOUT --state-dir STATE`
prints the planned hook paths and writes nothing. `--apply` copies a self-contained
stdlib runtime to private `STATE/head-audit.py`, and installs `post-checkout` and
`reference-transaction` shell stubs. Other hooks are refused before any write;
exact prior plangraph audit stubs can be upgraded. Git's hooks path is honored,
so ensure it is dedicated to this production repository before applying.
The stubs use the absolute interpreter and copied script with `-I -S`; they do
not import the changing checkout and do not require Python on PATH/PYTHONPATH.

Prepared/aborted transactions and file-only checkouts return immediately in sh.
Only committed HEAD moves start the runtime, at most once per move: per-worktree
markers suppress the second observer. Linked worktrees use their own HEAD and
worktree path. Every stub exits zero, including runtime/import/missing-script
failures; the runtime arms a one-second alarm before dependency imports and I/O.
Git may wait up to about one second per HEAD move for lock contention or blocked
I/O; interpreter startup precedes that timer. Audit remains best-effort: deadline
expiry or errors may omit observations. The marker prevents a second attempt
from delaying the same move.

The private JSONL log records UTC time, old/new SHA, worktree path, its branch or
`detached`, and /proc pid/ppid/command-name chains. Process arguments/environment
are never read. Appends serialize with rotation under an exclusive lock and use
one O_APPEND write per complete line; they never scan the whole log.
At 1 MiB the log rotates to `head-moves.jsonl.1`, retaining one previous file.
Auditing observes moves; it never authorizes or prevents them. No hooks are
installed by this package.

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
