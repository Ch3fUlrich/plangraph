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
The live adapter below shares the commit and protection policy. The approved reaper adds quarantine, a seven-day retention period and a separate deletion plan.
Test: `cargo test -p pg-reap` (hermetic boundary/protection/parser tests and stable report in a read-only temp directory).

## Live merged-worktree sweep

`pg-worktree-sweep --report-claude-jobs "$HOME/.claude/jobs"` lists stale Claude
job directories and their sizes in JSON. A candidate needs a valid `done`
record with an offset-aware `updatedAt` and no newer filesystem writes in
the last seven days. The command only reports candidates.

User units protect the worktrees they run from. The sweep reads every file and drop-in under
`$XDG_CONFIG_HOME/systemd/user` (or `~/.config/systemd/user`) and every link target, splits each
word of each value on whitespace, quotes and `; & | ( ) < > = , :` and refuses a worktree that is,
or contains, any absolute path those pieces (or a whole word with spaces) name, printing
`named by user unit <unit>`. Protection over-approximates: shell scripts, comments and redirections
are not parsed, so their paths protect too. `pg-disk-hygiene` keeps those worktrees' caches.
`pg-worktree-sweep --check-units` lists each unit whose worktree is missing
(`unit-worktree-missing <unit>: <path>`, exit 1), but only for whole words that start with `/` and
hold no whitespace or `; & | ( ) < >`, and for the literal value of a path-valued field
(`WorkingDirectory`, `*Path`, `*Directory`, ...); a worktree root is `*worktrees/<name>` or
`runs/<run>/worktree`. Shell text never reaches the report. An unreadable unit directory protects
every worktree.

A pin is not a run: `live-run` counts only trees owned by an unfinished run record (its checkout is the
tree or inside it; a run in an ancestor directory owns nothing below it), and pinned trees (and the
reserved `plangraph-md-sync` / `live-*` names) report `pinned`. Both protect.

`pg-worktree-sweep --repo REPO --root WORKTREES --pins PINS.json` defaults to dry
run. Repeat `--root` for each worktree root, including the state `runs` directory.
`--apply` removes only clean merged candidates using the shared pg-reap protection
and commit policy. `--idle-minutes` defaults to 60 and replaces the pure report's
24-hour clean threshold; the dirty backup rule is never applied. It refreshes origin,
collects `git worktree list --porcelain`, checks all commits against origin/main
(including nested batch merge parents), verifies remote containment and clean
status including ignored files, and measures idle time from the newest worktree
Git HEAD/index mtime. A missing index is permitted; missing HEAD fails closed.

Each candidate refreshes the inventory and reads pinned paths, active run records,
process cwds from `/proc`, user service WorkingDirectory (loaded and installed
units), GitHub PR state via `gh`, and keeper claims via a reader. Set
`PG_SWEEP_CLAIMS_READER` or `--claims-reader` to a trusted shell command which performs a
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
survive regardless of age. Detached trees use the same containment and protection checks. Run trees additionally
require a readable terminal record (native statuses and verified legacy aliases
are listed below); nonterminal and unknown statuses protect.

Before any apply deletion, a private, synced
`~/.local/state/plangraph/sweep/<timestamp>.tsv` records repo, path, branch, SHA and
PR. TSV escapes backslash/tab/CR/LF. Removal uses `git worktree remove` without
force, followed by SHA-conditional local-ref deletion. The restore record remains
if a later command fails. Per-tree errors protect/skip that tree and are printed.
Restore with `git branch SHORT_BRANCH SHA` (strip `refs/heads/` from the stored
branch), then `git worktree add PATH SHORT_BRANCH`. For detached rows (branch
`-`), restore with `git worktree add --detach PATH SHA`.

`--delete-remote` additionally requires `--apply`, a MERGED PR matching the exact
recorded SHA, and a remote SHA lease. The timer never passes it. A missing PR does
not relax ancestry or push safety. `--apply --dry-run` is a refusal.

The `pg-worktree-sweep.timer` invokes the report-only reaper daily. Disk checks stay
on their 30-minute timer. Install both sweep units and both binaries. See
`deploy/systemd/disk-hygiene.env.example` for all four roots, pins, and the
claims-reader settings (`PG_SWEEP_CLAIMS_READER`, `PG_URL`, `PG_TOKEN_FILE`).
Per host, the operator mints an unscoped read-only `sweep` principal
(`pg admin add-principal <name> --role sweep`, reads claims, runs and PR links
only; all writes return `403`) into a mode-0600 `~/.config/plangraph/sweep.token`
file. When that token file exists, `deploy/bootstrap.sh` creates
`~/.config/plangraph/disk-hygiene.env` from `deploy/systemd/disk-hygiene.env.example`
and an empty `~/.config/plangraph/pins.json` (`[]`) if missing. Set
`PG_SWEEP_CLAIMS_READER="PYTHONPATH=/path/to/plangraph/src /path/to/plangraph/.venv/bin/python3 -m plangraph.keeper.cli sweep-claims"` with `PG_URL` and `PG_TOKEN_FILE`
configured for that unscoped `sweep` (or `router`) token. This verb reads every project directly
from keeper, refuses unknown/truncated open lanes and changing snapshots, and
never falls back to a local copy. The reader setting is a trusted shell invocation.

`merge_batch.py land` invokes one `--apply --delete-remote --include-merged` sweep for exact
`--target PATH:refs/heads/BRANCH:SHA` tuples taken from batch state and the matching
local inventory. The same pass checks other idle worktrees under the scanned roots
with the usual live protections and restore records. Only exact landed targets
can trigger remote deletion. Explicit
`--remote-target BRANCH:SHA` entries also clean branches without local trees and
batch-closed PRs, after fresh claims/PR checks and origin/main containment. Each
remote deletion writes and syncs a restore row and uses
`--force-with-lease=refs/heads/BRANCH:SHA`; an advanced remote branch survives.
Restore remote rows (path `-`) with `git push origin SHA:refs/heads/BRANCH`.
Cleanup errors are printed without failing land. The daily timer only reports. Missing authorities protect. Configure and inspect a dry run
before enabling. Repository validation does not install timers or clean live host
trees. Keep build/start scheduling quiescent during host maintenance; the separate
spawner admission gate owns concurrent start prevention.

Run status authority includes the operator's native statuses: completed,
failed, timed_out, cancelled, refused, stalled. Known legacy terminal aliases
(timeout, dry_run, killed, succeeded, skipped, terminated_after_answer) use the
same guards. Native `state/runs/<run>/worktree` and legacy
`state/worktrees/attempt-*` roots are added automatically. A legacy tree must
match checkout/cwd/run_worktree/worktree_attempts metadata in a terminal record;
unmatched trees, unknown statuses and surviving processes protect.
Missing optional roots are reported and skipped; other root errors abort safely.
Detached PR protection enumerates every open PR (truncation protects), then matches
its exact head SHA. Protected branch names survive both remote deletion modes.
Every remote deletion also has a branch-only TSV row (path `-`); restore any
remote with `git push origin SHA:refs/heads/BRANCH`.

The reader's local metadata binding uses keeper `show.run_ids` and the real local
spawner records, not an assumed extra keeper response field. Its `--state DIR`
can select another local state root. A completed writer's paths remain claimed
while its keeper item is in review. Unresolved active claims refuse the reader.
Keeper `show` bypasses the ref-list cache for fresh open-lane reads.

Empty path arguments (including unset systemd root variables) refuse the whole
sweep rather than interpreting them as the current directory. The sweep exports
its canonical `--state` as `PG_SWEEP_STATE` to the claims reader, so local run
metadata follows the same state root. A reader's explicit `--state` overrides it.

## Approved host reaping

Run `pg-worktree-sweep --reap --repo REPO --root WORKTREES --pins PINS.json`
with the same roots, state and keeper reader as the service. It prints reasons
for protected paths and one JSON row for each worktree or run directory eligible
for quarantine or deletion. Worktrees need over seven days of idle time, clean
status including ignored files, and commits present on origin/main. Run
directories need a terminal record owning the native checkout, a final head
present on origin/main, seven days of idle time, and no remaining checkout.
Directories with nested checkouts, symlinks or mounts stay protected. Main and
push clones stay protected. Cargo target roots are listed for disk hygiene.

Send the desired JSON rows to routing for approval. Save the approved rows,
one per line, and repeat the command with `--approval D-NUMBER --plan FILE`.
The approval ID is recorded as supplied by routing. Every row must still
match the live report. Reports use one snapshot of keeper claims, run pins,
processes and user units, one open PR inventory for all detached heads, and
one PR query per named branch. Before an approved mutation the reaper reloads
that evidence and rechecks run ownership. Worktree mutations and run directory
moves hold the creation lock. Run directory deletion uses the applicable
writer locks without blocking new worktree creation. The reaper also takes the
legacy attempt root lock for legacy paths.
A missing authority protects the path. Process discovery checks cwd,
executable, root and open descriptors. A host that needs the privileged process
reader must install the version supporting `pg-process-cwds --references`.

The approved quarantine step records the move in a private receipt under
`STATE/reap-quarantine` and preserves local and remote refs. The source and
quarantine must share a filesystem. After seven days, request approval for
the new deletion rows and apply that list. `STATE/reap.ndjson` records the
approval, original path, quarantine path and logical bytes for each deletion.
A move interrupted before the retention timestamp is recorded stays in
quarantine for manual inspection. The daily timer remains report-only.
