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

Landing freezes unless main's latest tests run is completed green on main's
current tip. A missing, queued, cancelled or failed run refuses with `main_red`.
The only exception is a recorded GO row naming the exact batch SHA, `GO`, and
`EXCEPTION:main-red-fix` as separate tokens; pass its file with `land --go-row`.
It does not waive the batch's own full CI or review requirements.

Trial runner changes on a non-main branch. Before changing `CI_SELF_HOSTED*`
or `*_RUNNER_LABELS`, dispatch the full tests workflow on that branch using the
proposed runners, require every job green, and record the run URL, head SHA and
runner labels in the authorizing D-row. Only then switch the repository runner
variables. A red trial is repaired on the branch; main is never the trial host.
The router-owned `merge-part` command must perform the same main-tip check
before using any single-PR exception.

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
