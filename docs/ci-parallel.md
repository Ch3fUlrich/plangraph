# Parallel CI isolation

On Windows, parallel test processes set `PG_SPAWND_PIPE_NAMESPACE` to a unique
per-test temporary directory name. The optional namespace accepts up to 80 ASCII
letters, digits, or hyphens and keeps the user name and user-only pipe DACL.
Normal invocations continue to use the per-user pipe.

The `ci-temporal` Rust helper runs a command with `PG_TEMPORAL_ADDR`, `SPIKE_ADDR`,
`TEMPORAL_TEST_ADDRESS`, and `TEMPORAL_ADDRESS` pointing to its own dev server.
It takes an OS-selected port, disables the UI, leaves HTTP and metrics on random
ports, and keeps logs in a private directory under `RUNNER_TEMP` (or the system
temp directory). Startup retries cover a lost port-allocation race. The helper
kills and reaps its server and prints the log tail when the command finishes.
It uses the same process and temporary-directory handling on Linux and Windows.

## Merge tiers and environment evidence

Draft PRs and PRs into a phase branch run policy gates and affected suites;
ready PRs into main and all `batch/*` PRs select every suite. Full-tier Python
jobs use the complete suite rather than affected-test selection. The same
runner variables and fork guards apply to PRs and main; this change does not
provision additional runners or change their labels.

`ci-gate` depends on every workflow job, including browser and rust-windows.
In the full tier, skipped jobs fail the gate. `merge_batch land` additionally
reads the latest tests workflow run on the exact saved batch head, requires
all jobs and shards green with none skipped, and refuses missing environments.
`--checks` cannot bypass that proof. Rerun attempts and their job names are
recorded in an idempotent batch PR comment before landing; explain them with the flake fix or
quarantine Plan-Item, rather than silently rerunning a known failure.

## Red main and runner switches

One function decides main's state: `plangraph main-state` (rust/plangraph-cli/src/land). It reads the tip of
main, the `tests` runs on exactly that tip (push, schedule, workflow_dispatch) with every attempt and its job
list, and the ref again; a ref or run listing that moved during the read starts the pass over, and three
failed passes answer `state_unknown`. It answers `green`, `pending`, `red` (with the blocking runs and their
failure sets) or `ref_not_forward_only` (main's protection must forbid force push and deletion). A run blocks
until one of its own attempts is complete-green, so a green scheduled run never hides a failed push run; an
attempt older than the bound T (twice the median of the last 20 green push runs, at least 30 minutes) counts
as stalled.

A tip that reached main by a tested fast-forward runs only the fast jobs on main (matrix tier `fast`); its full CI
ran before it became the tip. Its state is the latest applicable generation: the latest concluded full attempt on the
commit (any ref, so the evidence run on the train's branch and every later full rerun count) together with the latest fast
run. Green when both are green, red when either is red, pending while the latest started attempt of either is not
concluded; green, red, green on the same commit reads green again without a new commit.

`tools/merge_batch_land.py land`, the keeper's phase finish and the router's `merge-part` call it before
they merge and refuse an ordinary merge with `main_red`, `main_pending`, `state_unknown` or
`ref_not_forward_only`. The one exception is a GO row for the exact head with `EXCEPTION:main-red-fix`;
the function honours it only while it answers red at that read (`plangraph main-state --admit --exception
EXCEPTION:main-red-fix`). It does not waive the batch's own full CI or review requirements. The train builder
freezes no manifest while main is red or pending.

`land` also refuses a head whose green run was tested on an older main: the head must be
0 commits behind main's current tip (`stale_base`, from the compare of `main...HEAD`), so the
tested tree is the tree that merges and two pull requests that are green alone but red together
are caught at the second. The merge itself stays a pull request merge bound to the tested head.

Runner or environment trials run on a branch, never on main. Procedure to change `CI_SELF_HOSTED*` or any
`*_RUNNER_LABELS` variable:

1. Dispatch the full `tests` workflow (`workflow_dispatch`) on a non-main branch that uses the proposed runners.
2. Require every job green, none skipped.
3. Record the run URL, head SHA and runner labels in the authorizing D-row.
4. Only then change the repository runner variables, then rerun main's failed run if one is open.

A red trial is repaired on the branch. `deploy/ci-watch/matrix.toml` lists the expected jobs of each tier;
change it with the workflow.

## Bounded Python CI steps (D-6054)

Every step in the pytest and pytest-postgres jobs has a deadline below its job
limit. Python-job commands and browser tests use `bounded-step`: GNU `timeout -k`
ends the command before the step deadline, with stdout/stderr redirected directly
to a unique `RUNNER_TEMP/ci-step.*.log`. The action prints that path, the saved
output, and the original exit status, then fails on timeout or command failure.
Escaped descendants inherit a regular file rather than the runner's output pipe.
Logs remain on the runner for diagnosis; routine timeouts finish as failed steps,
so the workflow job log remains available. External cancellation can still lose
GitHub logs. The remaining non-Python CI jobs need the same treatment separately.
