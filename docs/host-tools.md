# Rust host tools

`pg-disk-hygiene prod-paths [--json]` is a read-only production path check.
It inventories `~/.config/systemd/user` (override with `--unit-dir DIR`), unit
drop-ins, ExecStart/ExecStartPre, WorkingDirectory, Environment/PYTHONPATH,
EnvironmentFile paths and contents, global Git core.hooksPath, each main
checkout's local core.hooksPath, and `crontab -l`. Empty unit assignments reset
earlier values; drop-ins apply in filename order. `%h`, `$HOME` and `~` expand
without running shell code. Git context/config injection variables are cleared.
Main checkout defaults are `~/code/plangraph` and `~/code/plangraph-data`;
repeat `--repo PATH` to select other main checkouts. Allowed roots default to
those two checkouts plus `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`,
`/run`, `/var` and `/dev`. Repeat `--allow-root PATH` to add an allowed root.
Paths under `worktrees`, `*-worktrees`, `.claude/worktrees`, `plangraph-hooks`,
`code/routing` or `AutoOS` always fail, including resolved symlink targets.
Each issue prints `path-kind file: offending-path` and exits 1; clean checks
exit 0. JSON includes `ok`, `issues` and `allowed_roots`. Inventory errors also
exit 1. The check never edits units, Git config, or crontabs.

Build host binaries with `cargo build -p pg-worker-core --bins --release` in
`rust/`. Install binaries in `~/.local/bin` on each worker host.

`pg-fleet-series sample --host ID` appends one minute sample under
`~/.local/state/plangraph/fleet/ID.ndjson`. `probe` emits without persisting;
`--ssh DEST` collects a remote worker host using the same installed binary.
Failures persist `reachable=false`; unavailable measurements are null.
The state root can be overridden with `--state DIR`.
Running/queued records are read from `DIR/runs/*/record.json`; session IDs
come from `session_id` or `session`. This measures recorded agent sessions.
Only adjacent reachable samples with an increased OOM counter mark sessions lost.
`pg-fleet-series summary --hours 24` prints mean/p90 runs, idle-with-ready hours,
OOM increments, lost sessions and unreachable minutes, including missing minutes.
Rust run records gain `oom_kill_delta` at terminal transition, using their stored
host and sampled interval; missing/recently unavailable samples produce null.
The Rust command replaces the proposed Python fleet-series entry point.

Install the fleet service and timer in `~/.config/systemd/user/`, and create
`~/.config/plangraph/fleet-worker.env` with `PLANGRAPH_HOST_NAME` set to the
registry ID. Enable the timer's worker instance with `systemctl --user enable --now`.
Deployment and multi-host evidence require operator access and are not local tests.

Before each package start, call `pg-package-host` with the existing
`~/.config/plangraph/hosts.json` registry (`--registry` overrides it).
It uses samples at most 120 seconds old and probes stale/missing hosts over SSH.
It reserves 2048 MB above a 3000 MB RAM floor, gates at load/core >= 1,
then orders by load/core, swap, runs, recorded sessions, free RAM and registry order.
Windows workstation placement requires `--allow-workstation`.
JSON includes the selected host, measurements and every admission refusal.
Exit 5 means queue: no host admits. `--reservation-mb` sets package memory.
This admission module is also the Rust overflow spawner's policy source.
The [phase-batch skill](../skills/phase-batch-orchestration/SKILL.md) calls the
readiness and placement commands before each package start. Repeat
`--exclude-host ID` for failed readiness checks; stricter registry RAM floors
and per-role reservations also apply to both selection and overflow.

Before launching Codex, run `pg-host-readiness --project PATH --sandbox workspace-write`.
It checks version, login, native sandbox startup, and a commit in an independent
temporary repository's worktree, with a 30 second command timeout and no model call.
`--scratch-root` chooses a filesystem matching the intended workspace.
`--codex` selects the executable. It reads `CODEX_HOME/config.toml` (or `HOME/.codex`),
requires folder trust, disabled startup updates and at least 3000 MB free RAM.
It prints `READY` only with no missing checks, otherwise `MISSING` lines and exit 1.
Configured hooks/plugins currently report unverified trust: persisted hook hashes
are CLI-owned and this tool does not assume their storage schema. This is a known
limitation for hosts using hooks; it never accepts an unproven hook trust state.
The phase-batch skill must call the check on the chosen host before a package.
See the official [configuration reference](https://developers.openai.com/codex/config-reference)
and [sandbox command reference](https://developers.openai.com/codex/cli/reference).

`pg-repo-sync --repo PATH --check-config --host ID --allow-name NAME --allow-email EMAIL`
checks a main checkout's local `core.bare` and identity overrides without writing.
Repeat allow-list flags for accepted host identities. Absent local overrides inherit
the host identity; explicit overrides must match. Drift prints one `ALERT` line with
host, path and escaped values and exits 1. Clean checkouts are silent.
Git context variables, including `GIT_DIR` and injected config, are removed from
child processes so fixtures and host checks cannot address unrelated metadata.

With no arguments, `pg-repo-sync` discovers every direct child of `$HOME/code`
containing `.git`, including symlinked directories. Missing HOME or root is silent
success. `--root DIR` and `--repo PATH` are repeatable; explicit labels stay as passed.
It fetches origin and silently fast-forwards only a clean default branch.
Alerts use exactly `detached`, `other-branch:NAME`, `tracked-changes`, `diverged`, or
`fetch-failed`; failed probes map to those codes, never a fabricated behind count.
Add `--sync` to `--check-config` to perform both checks on a worker host.

`pg-disk-hygiene --repo PATH --root WORKTREES --pins PINS.json --dry-run`
reports disk usage, alarming at 75%, with cleanup candidates at 80%.
The mandatory pins file is a JSON array of absolute paths exported from md-sync;
active run records add live checkout protection. Unreadable protection data fails
closed. Only registered, clean, unlocked worktrees under the explicit root idle
for over 24 hours qualify. The main checkout and symlinked caches are preserved.
`--apply` refreshes origin with prune, deletes rebuildable `target/` and
`node_modules/`, and removes worktrees only for merged or previously tracked,
now absent origin branches. It never forces removal or blindly prunes worktrees.
Reports and freed file bytes are logged to `disk-hygiene.ndjson`; the latest
`disk-hygiene.json` contains `alarm` for the orchestrator heartbeat to read.
Review a dry run before installing the 30 minute user timer and setting `PG_REPO`,
`PG_WORKTREES` and `PG_PINS` in `~/.config/plangraph/disk-hygiene.env`.
Live timer installation and real host cleanup are separate deployment evidence.

`pg-disk-hygiene sweep-merged --repo MAIN --root WORKTREES` prints a dry-run
branch/worktree removal plan. `--json` emits actions, skipped reasons and the
GitHub call count. `--max N` caps branch action groups per run (default 20).
The main checkout, invoking checkout, `main`, `master`, remote default branch,
locked/prunable trees and trees outside the explicit root are protected.
The command batches `gh pr list --state merged --limit 1000` once, then checks
all PR states per branch, stopping at 64 total GitHub calls. Open PRs (including
`phase/*`) and truncated per-branch histories are protected. A branch needs a
MERGED PR with a matching tip SHA, or a CLOSED PR whose matching head is
contained in `origin/main`; newer local commits never inherit an older PR's
eligibility. Missing PR evidence preserves the branch.

Worktrees qualify only for an eligible branch or an absent origin branch, and
only when clean, with every commit reachable from origin. Detached trees use
the same origin containment guard. Ignored files outside `target/` and
`node_modules/`, tracked caches, user-unit references (using the `prod-paths`
scanner, including quoted paths and drop-ins), or any process cwd preserve the
tree. Every worktree of a branch must qualify before that branch is removed.
On Linux the process check scans `/proc`; unreadable cwd links fail closed.
Full removal coverage therefore requires visibility of all process cwds.
On other operating systems worktree removal is conservatively skipped.

`--apply` fetches/prunes origin, writes and syncs a private restore TSV before
deletion, rechecks worktree guards and branch SHAs, removes worktrees first,
then deletes eligible local branches. An absent-origin branch without PR
evidence keeps its local branch even if its worktree is removed. Dry-run
does not fetch, write state, or delete anything. `--remote` additionally opts
into remote branch deletion with apply; remote and local tips must match, and
a SHA lease prevents deleting a remote branch changed since planning.
Without `--remote`, remote branches remain intact.

Restore files live at `~/.local/state/plangraph/sweep/<UTC>.tsv` (override the
state root with `--state DIR`), with header `branch\tsha\tpr\tworktree`.
Keep the TSV after a partial failure; mutations stop at the first error.
Recreate a recorded branch with `git branch BRANCH SHA`, then restore its tree
with `git worktree add PATH BRANCH`. For a deleted remote, push the recreated
branch to origin. Detached rows have an empty branch and can be restored with
`git worktree add --detach PATH SHA`. These commands require the recorded
objects to remain available; restore before Git garbage collection.
`--unit-dir DIR` chooses a fixture or alternate user-unit directory.

Affected fixture tests run in WSL from `rust/` with
`PATH="$HOME/.cargo/bin:$PATH" cargo test -p pg-worker-core --test prod_paths
--test sweep_merged --test disk_hygiene`. The sweep fixtures use a PATH-stubbed
`gh`, temporary main/bare-origin repos and no real Git config writes. Run with
full `/proc` visibility for the apply/removal assertions; unprivileged runs
also verify that inaccessible processes preserve worktrees.

`pg-package-host --spawn REQUEST.json --order ID1,ID2,ID3 --allow-workstation`
starts a package through the existing `pg-spawnd shim` on the chosen host (SSH
for remote hosts). Each target must already run its configured spawner daemon.
The request is the existing spawn tool's JSON arguments (`card`, `prompt`, role,
and optional seat arguments); checkout paths must be valid on the target host.
Admission uses the same policy as package selection. Overflow tries registry
order, or the complete explicit `--order`, immediately after each refusal.
Receipts and all refusals are saved privately under `STATE/overflow/`.
Exit 5 asks the orchestrator to queue only after all hosts refused. Exit 1 means
uncertain launch state: inspect the target before retrying to avoid duplicate work.
An accepted queued daemon receipt is retained as an acknowledged launch.
The phase-batch caller should use this spawn mode after the readiness check.
