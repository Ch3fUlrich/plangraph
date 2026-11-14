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
