# Native Codex writer runs

Native Codex writer cards use `run_kind: "native-codex"`, `repo` (the local
repository path), and `base_ref` (a pushed `refs/heads/...` or `refs/tags/...`).
Submit through `python3 -m plangraph.spawner.cli spawn --card @card.json
--prompt-file brief.txt`. The usual admission/overflow policy selects the host;
remote forwarding rewrites `repo` to that host's configured repository. Public
repository allowlisting, fresh readiness and RAM caps still apply. Native runs
require at least 5 GiB free; the target checks again before starting and stops
Codex under 2 GiB. Targets need the updated Python spawner and `pg-spawnd` binary
on PATH (or built at `rust/target/release/pg-spawnd`), Git and native Codex auth.

The Rust worker fetches the pushed base into a per-run ref, refuses ancestry
behind fetched origin/main, and creates a detached worktree inside the
repository under `.plangraph-worktrees/<run-id>`. It runs gpt-6.1-sol at medium
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
