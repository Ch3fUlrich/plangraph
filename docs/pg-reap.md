# pg-reap: report-only worktree policy (slice 1)
Run `cd rust && cargo run -p pg-reap -- report < repositories.json`.
Input: JSON array of `{repo, porcelain, facts}`; one complete `git worktree list --porcelain` per repo, facts keyed by decoded path.
Required facts: `idle_hours` (nonnegative integer), booleans `clean`, `merged_into_main`, `has_claim`, `live_run`, `open_pr`, `in_use_by_process`, `in_use_by_unit`, `is_clone_path`.
Flags cover branch and path; callers provide facts, not live lookups. Unknown/incomplete fields fail.
Pure `classify`: clone/process/unit/claim/run/PR protections, then another worktree on the branch (both protected), then bare/locked/prunable, all beat age.
Merged clean is eligible at 24 h; unmerged clean at 72 h; dirty at 168 h regardless of merge status, always with `needs_backup=true`.
TSV columns: `repo path branch status reason needs_backup`, sorted by raw repo/path; detached/bare branch is `-` (detached HEADs do not share a branch).
Backslashes/tabs/LF/CR are escaped. Missing facts protect; malformed porcelain or duplicate repos fail without partial TSV.
Only stdin/stdout/stderr I/O: no git invocation, worktree access, moves, removals or locks. Eligibility is not approval.
Later slices: quarantine/removal, shared RUN-WORKTREE host locking, detached archive refs, verified patch/untracked backup, live facts, seven-day deletion and routing D-id approval.
Test: `cargo test -p pg-reap` (hermetic boundary/protection/parser tests and stable report in a read-only temp directory).
