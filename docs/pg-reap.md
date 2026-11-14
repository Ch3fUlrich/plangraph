# pg-reap: report-only worktree policy (slice 1)
Run `cd rust && cargo run -p pg-reap -- report < repositories.json`.
Input: JSON array of `{repo, porcelain, facts}`; one complete `git worktree list --porcelain` per repo, facts keyed by decoded path.
Report tests buffer requests before writing stdin, avoiding a broken-pipe race when invalid facts are rejected early.
Required facts: `idle_hours` (nonnegative integer), booleans `clean`, `merged_into_main`, `pushed`, `failed`, `has_claim`, `live_run`, `open_pr`, `in_use_by_process`, `in_use_by_unit`, `is_clone_path`.
Flags cover branch and path; callers provide facts, not live lookups. `merged_into_main` means **all** commits reachable from HEAD are merged into main; `pushed` means **all** are present on a remote, including detached HEADs. `failed` records a failed attempt. Unknown/incomplete fields fail, including missing push state.
Pure `classify` reason precedence: clone/process/unit/claim/run/PR protections, another worktree on the branch (both protected), bare/locked/prunable, main worktree, unmerged commits, unpushed commits, then age. Bare beats locked, locked beats prunable regardless of field order. The first porcelain entry intrinsically identifies the main worktree; sorting never loses this protection.
**Unmerged or unpushed commits are never eligible, at any age, clean or dirty.** Only merged-and-pushed worktrees can be eligible: clean at 24 h; dirty at 168 h with `needs_backup=true`. This supersedes the earlier unmerged-clean 72 h and dirty-regardless-of-merge eligibility rules; backup is not a waiver for commit safety.
Pure `state` labels clone/active/duplicate-branch/bare/locked/prunable/main paths `live`; otherwise merged paths `merged`, failed attempts `failed`, unmerged idle paths at 72 h `abandoned`, and younger paths `live`, in that order. These lifecycle labels do not grant eligibility: failed/abandoned paths with unmerged commits stay protected.
TSV columns: `repo path branch state status reason needs_backup`, sorted by raw repo/path; detached/bare branch is `-` (detached HEADs do not share a branch).
Backslashes/tabs/LF/CR are escaped. Missing facts label `live` and protect. Empty/blank or malformed porcelain, duplicate repos, duplicate decoded paths (within or across repos), and facts keys absent from their own repo inventory all fail without partial TSV. Foreign facts cannot stand in for missing facts.
Only stdin/stdout/stderr I/O: no git invocation, worktree access, moves, removals or locks. Eligibility is not approval.
The live adapter below implements clean-worktree removal. Future slices: quarantine, shared RUN-WORKTREE host locking, detached archive refs, verified dirty/untracked backup and seven-day deletion approval.
Test: `cargo test -p pg-reap` (hermetic boundary/protection/parser tests and stable report in a read-only temp directory).

## Live merged-worktree sweep

`pg-worktree-sweep --repo REPO --root WORKTREES --pins PINS.json` defaults to dry
run. `--apply` removes only candidates approved by `pg_reap::classify` with the
`merged-clean-24h` rule; the dirty backup rule is never applied. No second merge,
commit-safety or age policy is implemented in the adapter. It refreshes origin,
collects `git worktree list --porcelain`, checks all commits against origin/main
(including nested batch merge parents), verifies remote containment and clean
status including ignored files, and measures idle time from the newest worktree
Git HEAD/index mtime. A missing index is permitted; missing HEAD fails closed.

Each candidate refreshes the inventory and reads pinned paths, active run records,
process cwds from `/proc`, user service WorkingDirectory (loaded and installed
units), GitHub PR state via `gh`, and keeper claims via a reader. Set
`PG_SWEEP_CLAIMS_READER` or `--claims-reader` to an executable which performs a
fresh authoritative keeper read and prints exactly
`{"paths":["/absolute/claimed/tree"],"branches":["refs/heads/topic"]}`. It must exit
nonzero on unavailable/incomplete authority. Branches may also be short names.
The reader receives no arguments and must cover every keeper project using the
host. Missing readers, failed readers and malformed protection data protect the
candidate. This contract supports existing host-specific keeper readers without
assuming a keeper schema or treating a stale snapshot as current authority.
`gh` must be authenticated for the repository. Truncated PR inventories protect.

Main and clone paths, locked trees, active claims/runs, open PRs, process/unit
working directories, duplicate branch checkouts, `plangraph-md-sync`, and keeper
`live-*` paths survive. Symlinked trees are protected. Unmerged or unpushed commits
survive regardless of age. Detached trees are conservatively protected.

Before any apply deletion, a private, synced
`~/.local/state/plangraph/sweep/<timestamp>.tsv` records repo, path, branch, SHA and
PR. TSV escapes backslash/tab/CR/LF. Removal uses `git worktree remove` without
force, followed by SHA-conditional local-ref deletion. The restore record remains
if a later command fails. Per-tree errors protect/skip that tree and are printed.
Restore with `git branch SHORT_BRANCH SHA` (strip `refs/heads/` from the stored
branch), then `git worktree add PATH SHORT_BRANCH`.

`--delete-remote` additionally requires `--apply`, a MERGED PR matching the exact
recorded SHA, and a remote SHA lease. The timer never passes it. A missing PR does
not relax ancestry or push safety. `--apply --dry-run` is a refusal.

The disk timer's oneshot service invokes this binary with `--daily --apply` after
the disk check. A private completion marker limits removal attempts to once per
24 hours; the other 15-minute invocations perform disk checks only. Install both
binaries. Unavailable authorities produce protected rows; configure and inspect a
dry run before enabling. Repository validation does not install timers or clean
live host trees. Keep build/start scheduling quiescent during host maintenance;
the separate spawner admission gate owns concurrent start prevention.
