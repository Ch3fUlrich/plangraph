# Native Codex writer runs

Native Codex writer cards use `run_kind: "native-codex"`, `repo` (an explicit absolute local
repository path; missing or relative paths are refused before admission), and `base_ref` (a pushed `refs/heads/...` or `refs/tags/...`).
Submit through `python3 -m plangraph.spawner.cli spawn --card @card.json
--prompt-file brief.txt`. The usual admission/overflow policy selects the host;
remote forwarding rewrites `repo` to that host's configured repository. Public
repository allowlisting, fresh readiness and RAM caps still apply. Native runs
require at least 5 GiB free; the target checks again before starting and stops
Codex under 2 GiB. Targets need the updated Python spawner and `pg-spawnd` binary
on PATH (or built at `rust/target/release/pg-spawnd`), Git and native Codex auth.

The Rust worker fetches the pushed base into a per-run ref, refuses ancestry
behind fetched origin/main, and creates a detached worktree in the
run state directory under `runs/<run-id>/worktree`, outside the repository. It runs gpt-6.1-sol at medium
reasoning with the brief on stdin. A review on the final HEAD is mandatory:
missing reviews resume the same Codex thread at most three times. Review routes
must use role `review`, writer_families `["openai"]`. The optional `review_dir`
card field selects a target-host directory; the default is `<worktree>/reviews`.
Review `.txt` filenames must contain head8, or their first five lines must name
the full head. The newest matching file with a canonical verdict counts; ACCEPT
then runs the available file-size, language and changelog/docs gates. Repositories
without these scripts rely on the brief's own tests, as the plan-order driver did.

Run records retain host, pushed base ref and resolved SHA, worktree, session ID,
final head, resume count, review path/text/verdict and exit code. Exits are 0
accepted, 3 review changes, 4 missing review, 5 base refusal, 6 writer failure,
7 final gate failure, 8 start disk refusal. Worktrees and logs remain for recovery.
This slice uses the existing POSIX SSH overflow transport; Windows readiness,
worktree paths, disk probing and native process supervision need a later port.
The plan-order launcher scripts remain until the migration slice.

Overflow receipts keep target process identity under `remote_process`; origin
watchdogs never sample that PID locally. Cancel is forwarded to the target by run
ID. A failed SSH cancel leaves the last known run status intact with a query error.

The Rust MCP bridge delegates native launch, result refresh and cancellation to
the same Python owner. Remote receipts refresh before returning a result; cancel
reaches the Python supervisor locally or the target host over SSH.

## Health check

After a spawner move, each target must answer `pg-spawnd health --json` with exit 0;
`pg-spawnd` has no `--version` verb (advertised verbs: `probe`, `serve`, `shim`, `health`, `agents-view`, `ledger-report`).
Route listings never show `native-codex`: it is a card `run_kind` that spawn sends past routing
(`src/plangraph/spawner/cli.py`); native dry runs are refused (`src/plangraph/spawner/overflow.py`).
Check routing with a review dry run from a fresh `python3 -m plangraph.spawner.cli` process.
Make the first native-codex run after a move a small canary; check its run record for the exit code and review path before real writers use the spawner.

## Writer statistics ledger

Each terminal native run writes one immutable row in `runs/<run-id>/ledger.jsonl`.
Repeated saves of the same row are idempotent; differing rows are refused. Atomic
private writes and the existing nonblocking lock prevent partial or duplicate rows.
A failure is printed and retained as `ledger_error` in `record.json`.
`pg-spawnd ledger-report <state-directory>/runs` prints JSONL totals by UTC end day.
Each metric also has an `<metric>_unknown` count; unknown data never means zero.
Malformed rows refuse the report. Interrupted runs without an end record have no row.

Rows include the host, requested model/effort, exit code, elapsed wall minutes,
committed additions plus deletions from the admitted base to final head (binary
files contribute no text lines), and tokens. Text `tokens used` summaries accept
comma separators; native `turn.completed` usage sums input plus output across
resumes. Cached input is already included in input. If both representations are
present they must agree, and are counted once. Missing usage is null.

This slice counts the single `item_key` supplied to the native wrapper as given;
it counts done only after success with that key in a new commit's Plan-Item trailer.
Missing item identity is unknown. `review_rounds` counts canonical reviews observed
by the wrapper, excluding probes with no review; HIGH/MED held are zero on ACCEPT
and unknown otherwise. These are wrapper measurements, not inferred counts of the
writer's internal self-review rounds. Native launch failures before the Rust wrapper
starts have no ledger row. Batch landing statistics and legacy wrappers remain open: this slice does not
close plangraph:LEDGER-AUTO-FILL, whose land-row part needs its own keeper item.
