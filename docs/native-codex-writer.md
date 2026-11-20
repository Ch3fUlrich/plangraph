# Native Codex writer runs

Native Codex writer cards use `run_kind: "native-codex"`, `repo` (an explicit absolute local
repository path; missing or relative paths are refused before admission), and `base_ref` (a pushed `refs/heads/...` or `refs/tags/...`).
Submit through `python3 -m plangraph.spawner.cli spawn --card @card.json
--prompt-file brief.txt`. The usual admission/overflow policy selects the host;
remote forwarding rewrites `repo` to that host's configured repository. Public
repository allowlisting, fresh readiness and RAM caps still apply. Native runs
require at least 5 GiB free; the target checks again before starting and stops
Codex under 2 GiB. Targets need the updated Python spawner and `pg-spawnd` binary
on PATH (or built at `rust/target/release/pg-spawnd`), Cargo, Git and native Codex auth.
Before a run record is created, target-host admission resolves `cargo`, `git` and
`codex` on the caller's effective PATH. Missing or non-executable tools refuse
with `tool_missing: <tool>`. Configure the host's launch environment (the overflow
host's `env_file` for remote launches) to include the Cargo and nvm Node bin
paths where needed; admission does not source a login profile or guess paths.
Records retain `tool_path` and `resolved_tools`. Relative PATH entries are made
absolute against the caller's directory so they retain their meaning in the
worktree. The writer, its resumed sessions and final gates all use that recorded
PATH, including tools launched by gate scripts.

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

## WSL executable selection

The native writer resolves `codex` from PATH before starting the Codex process.
It refuses a selected executable under `/mnt/`, including a Linux-side symlink
into that tree, with `tool_wrong_platform`. A Windows npm shim can otherwise
pass a presence check and then fail looking for `@openai/codex-linux-x64`.
Missing executables report `tool_missing`; the launcher does not fall through
from a rejected Windows executable to another PATH entry.

For a WSL writer started through `ssh ws-build wsl -d Ubuntu -- bash script`,
the invoked script must export the Linux install directory before launching:

```bash
export PATH="$HOME/.local/bin:$PATH"
# Then invoke the writer launcher (or pg-spawnd native-writer request.json).
```

Non-login `bash` does not load the login-shell PATH setup. Install Linux Codex
in `$HOME/.local/bin`; the export also lets npm's Linux shim find Linux tools.
The external `codex-writer.sh` is not tracked in this repository; the guard here
covers the native writer path.

## Health check

After a spawner move, each target must answer `pg-spawnd health --json` with exit 0;
`pg-spawnd` has no `--version` verb (advertised verbs: `probe`, `serve`, `shim`, `health`, `agents-view`).
Route listings never show `native-codex`: it is a card `run_kind` that spawn sends past routing
(`src/plangraph/spawner/cli.py`); native dry runs are refused (`src/plangraph/spawner/overflow.py`).
Check routing with a review dry run from a fresh `python3 -m plangraph.spawner.cli` process.
Make the first native-codex run after a move a small canary; check its run record for the exit code and review path before real writers use the spawner.
