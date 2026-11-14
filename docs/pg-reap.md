# pg-reap: report-only worktree policy (slice 1)
Run `cd rust && cargo run -p pg-reap -- report < repositories.json`.
Input: JSON array of `{repo, porcelain, facts}`; one complete `git worktree list --porcelain` per repo, facts keyed by decoded path.
Required facts: `idle_hours` (nonnegative integer), booleans `clean`, `merged_into_main`, `pushed`, `failed`, `has_claim`, `live_run`, `open_pr`, `in_use_by_process`, `in_use_by_unit`, `is_clone_path`.
Flags cover branch and path; callers provide facts, not live lookups. `merged_into_main` means **all** commits reachable from HEAD are merged into main; `pushed` means **all** are present on a remote, including detached HEADs. `failed` records a failed attempt. Unknown/incomplete fields fail, including missing push state.
Pure `classify` reason precedence: clone/process/unit/claim/run/PR protections, another worktree on the branch (both protected), bare/locked/prunable, main worktree, unmerged commits, unpushed commits, then age. Bare beats locked, locked beats prunable regardless of field order. The first porcelain entry intrinsically identifies the main worktree; sorting never loses this protection.
**Unmerged or unpushed commits are never eligible, at any age, clean or dirty.** Only merged-and-pushed worktrees can be eligible: clean at 24 h; dirty at 168 h with `needs_backup=true`. This supersedes the earlier unmerged-clean 72 h and dirty-regardless-of-merge eligibility rules; backup is not a waiver for commit safety.
Pure `state` labels clone/active/duplicate-branch/bare/locked/prunable/main paths `live`; otherwise merged paths `merged`, failed attempts `failed`, unmerged idle paths at 72 h `abandoned`, and younger paths `live`, in that order. These lifecycle labels do not grant eligibility: failed/abandoned paths with unmerged commits stay protected.
TSV columns: `repo path branch state status reason needs_backup`, sorted by raw repo/path; detached/bare branch is `-` (detached HEADs do not share a branch).
Backslashes/tabs/LF/CR are escaped. Missing facts label `live` and protect. Empty/blank or malformed porcelain, duplicate repos, duplicate decoded paths (within or across repos), and facts keys absent from their own repo inventory all fail without partial TSV. Foreign facts cannot stand in for missing facts.
Only stdin/stdout/stderr I/O: no git invocation, worktree access, moves, removals or locks. Eligibility is not approval.
Later slices: quarantine/removal, shared RUN-WORKTREE host locking, detached archive refs, verified patch/untracked backup, live facts, seven-day deletion and routing D-id approval.
Test: `cargo test -p pg-reap` (hermetic boundary/protection/parser tests and stable report in a read-only temp directory).

## Recorded orphan reaping

On Linux, `pg-reap orphans EVIDENCE.json` accepts an array of
`{pid,start_ticks,cwd}` captured while the descendant was owned by a run.
Only a non-zombie orphan with parent 1, the exact recorded start token, and
exact absolute cwd is eligible. A pidfd is opened before verification so PID
reuse cannot redirect SIGKILL. Each reap is logged; unreadable ownership or a
five-second exit deadline fails the sweep as `kill_incomplete`. Unverified
entries are logged and untouched. This command never deletes directories.

The evidence producer must persist descendant snapshots before their root exits;
this command cannot recover missing historical identities from cwd alone. The
existing Python sampler still has a short-process observation gap. Integrating
persistent ownership evidence and stale probe-directory leases remains required
before unattended deployment. Do not infer evidence from a PID or directory name.
For an already verified inventory, an operator can run
`systemd-run --user --on-calendar='*-*-* *:*:00' --unit=pg-orphan-reap pg-reap orphans "$XDG_STATE_HOME/plangraph/orphan-evidence.json"`.
Retained probe directories with no ownership record need operator reconciliation.

## Attempt retention decisions

`pg_reap::retention::decide` records the RUN-WORKTREE outcome policy separately
from the report-only fleet reaper's stricter commit-safety policy: running attempts stay. Terminal worktrees are removed immediately; successful
branches are pushed and retained, merged branches may be deleted, and failed or
cancelled branches are retained for 14 days. An open PR or live claim prevents
branch deletion. Dirty removal candidates require verified patch and
untracked backup first. This pure function does not push or remove anything.
Unknown outcomes fail deserialization. A caller must collect current PR/claim
facts, verify merge ancestry and backups, and take the creation root's `.lock`
with POSIX flock (or Windows byte lock) before acting. The existing spawner
creation already takes that exact lock through `run_worktree_lock(root)`.

On Linux, `pg-reap finish-attempt < request.json` executes this attempt policy.
Required fields: `repo`, `root`, `path`, `branch`, `outcome`, `age_seconds`,
`open_pr`, `live_claim`. Paths must resolve to a registered non-main attempt
immediately below the host root, on a unique `lane/` branch; locked/prunable
registrations refuse. It takes that root's `.lock` nonblockingly, verifies merged
ancestry, pushes successful branches, removes terminal trees while retaining
protected/young branches, and compare-deletes eligible branch tips. A later
request can delete an expired retained branch after its worktree is gone; any
new registration on that branch refuses cleanup.
Dirty removal first writes and reads back a binary HEAD patch and makes a full
worktree tar backup (including ignored/untracked files and symlinks, excluding
`.git`), verifies it with `tar --compare` and syncs backup files. Backup errors
retain the worktree. Backups are under `root/backups/<attempt>` and never reused.
The caller must quiesce the run and collect fresh PR/claim/age facts; the command
trusts those explicit facts. Automatic run-end invocation and fleet facts
collection remain unwired. No deployment has been performed.

Before deleting any expired branch, the executor requires its exact tip to be
reachable from a remote-tracking ref. An unpushed tip refuses cleanup and keeps
the branch, including when its worktree was already removed. Backup archives
are not substitutes for this commit-history witness. Remote-tracking refs are
read as last fetched; the retention run does not refresh them. The caller must
fetch the repository separately when a fresh remote view is required.
Terminal outcomes do not prove idleness: immediately before backup and again
before removal, Linux procfs scans compare process start tokens around cwd reads
and protect processes anywhere inside the tree. An unreadable process inventory
refuses cleanup; the worker must have permission to inspect all live processes. The CLI always scans real `/proc`;
isolated namespace/procfs fixtures are available to library tests.

The orphan executor has a real-child pidfd regression: a living parent and a
wrong start token both refuse signals; the matching orphan is killed and its
pidfd confirms exit. Test cleanup pins the child identity too.
