# Native Codex writer runs

Native Codex writer cards use `run_kind: "native-codex"`, `repo` (an explicit absolute local
repository path; missing or relative paths are refused before admission), and `base_ref` (a `refs/heads/...` or `refs/tags/...` ref that exists on `origin` or, for a lane head never pushed, in the host repository; a ref found in neither is refused by name before any worktree is made).
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
run state directory under `runs/<run-id>/worktree`, outside the repository. It runs gpt-6.2-sol (lead writer leg, operator D-6397) at medium
reasoning with the brief on stdin. A card may name `effort` `high` or `xhigh` only
together with `effort_decision: "D-<n>"`; any other effort refuses with
`effort_invalid` or `effort_needs_decision` before Codex starts, and the manifest
and ledger row record the effort used. The worker drives the batch writer loop itself (R-batch-01..04, 06;
`rust/pg-spawnd/src/native_writer.rs`). After each writer turn it runs the
repository's fast gates present in the checkout (file size, new-code language,
changelog/docs, `ci_commit_gate.py check` against the pinned base, and the private
scrub when `~/.config/plangraph/scrub-words.txt` exists) and writes the stamp
`<git-dir>/fast-checks/<head>`. A failure resumes the same Codex session with the
output (stdout and stderr in full), at most twice per head. A turn that leaves HEAD at
the base is resumed with the items of its brief and runs no gate; after two such
resumes the run ends exit 7 `no_commit`. `codex-host-readiness` reports a placeholder
git identity (`example.invalid`, `localhost`, unset) as not ready. A review is refused without the stamp of exactly
that head. The worker then starts one review run through the spawner CLI (role
`review`, one `--writer-family` per family in the Writer trailers of base..head plus the
served writer model's family, read by `pg_worker_core::writer_families`, the reader
`plangraph review` shares). Admission selects a registered worker with enough
disk and RAM. The diff card transfers the exact commits to that worker, which
owns the checkout and its Cargo target under one disposable run directory.
The writer keeps its inherited target for preflight. The worker releases the
directory at review teardown. `PLANGRAPH_NATIVE_REVIEW_CLI`, a JSON argv array, replaces
`python3 -m plangraph.spawner.cli`. The review prompt uses the versioned review
brief and includes the writer's done criteria and invariants, both SHAs, changed
public symbols and their first callers, and the exact-head fast-check log
(`<git-dir>/fast-checks/<head>.log`) and writer session log. The completed run's
receipt binds the worker host and head. The last `VERDICT:` line must be
canonical. ACCEPT requires repository access and evidence in a `FILES READ:`
line. The review record sets `tested` from the reviewer's standalone `TESTS:`
report. A successful run exposes `review_test_status` as `reported` or `untested`
in its terminal record. An absent report or `TESTS: none:` is untested.
The Sonnet seat runs its scoped Bash commands in a read-only review checkout
with disposable writable build and test scratch space. A different `HEAD:` or weak ACCEPT is `no_verdict`, retried twice, then
the run ends BLOCKED with
exit 4. The review text is kept in `review_dir` as `<head8>-<n>-<run-id>.txt`.
REQUEST_CHANGES resumes the same session with a pointer to a private brief
in the run directory. The file preserves the original brief byte for byte
before the follow-up instructions and review text. Later rounds review
the delta from the previous head. The HIGH+MED count of each round goes to
`round_counts`; `native_writer::round_stop` applies the round progress rule of
`round_progress.rs` to the round just finished and caps the run at that file's
`MAX_ROUNDS` (exit 3 with `round_stop` `round_cap`, `no_progress` or
`slow_progress`; `no_session` when Codex
reported no session ID to resume). ACCEPT runs the same
gates on the clean final head, then pushes to the card's optional
`push_ref` (`refs/heads/...`, no force). A refused push records the head and
gate output in `push_failures` and gives the same session up to two fix turns
within the overall review cap. A fix must commit a new head and pass fast
checks and review before the next push. An unchanged head ends the run with
the original gate output. A successful push records
`status_line: DONE`. Exhaustion keeps the gate output in the failed run record.
The review seat follows the current spawner route. A card can pin a seat with
`review_leg` and an audited `review_override_reason` of at least ten characters.
The review record includes the selected leg and its harness. Before each
automatic review attempt, the worker checks the host's route for repository-capable seats.
An empty route records `review_status: deferred_to_batch_seat` and the per-leg
`review_causes`, exits successfully after final gates, and leaves the head for
the batch review. Once a seat is admitted, normal verdict retries apply.
The card's `review_dir` selects a target-host directory; the default is
`<worktree>/reviews`.

When a native batch brief provides no status-file path, put one
`SKIPPED <key> <reason>` line per skipped item in a commit body above its trailer
paragraph. The review prompt includes these lines for items in the brief and
the exact committed range. A later `Plan-Item` trailer clears an earlier skip.

The writer child runs Codex with `sandbox_mode="workspace-write"`, never `danger-full-access`
(`native_writer_sandbox.rs`; AGENT-JAIL step 1, rule 6.7). Network access stays on because the
writer fetches and builds. Beyond the worktree the writable roots are named: the shared cargo
target, the review directory, the run directory and the repository's and worktree's git
directories (Codex keeps a worktree's git directory read-only unless it is named, so a commit
would fail). The child's environment is an allowlist (`ENV_ALLOW`: home, locale, harness auth,
git identity, ssh agent, proxy and TLS settings, cargo and rustup homes); every other variable of
the supervisor, including host secrets, is dropped. A canary run on a worker host comes first
after a change to either list.
Package placement requires a fresh `workspace-write` record from `pg-host-readiness`.
The probe starts Codex's sandbox and commits in a scratch linked worktree using the
same sandbox overrides as the writer. On Linux, the writer account's AppArmor policy
must allow Codex's Bubblewrap helper to create its user namespace. A denied sandbox
startup marks the host unready.

Run the live write-boundary and Git fixtures on a candidate host:

```sh
cargo test -p pg-spawnd --manifest-path rust/Cargo.toml --bin pg-spawnd native_writer_sandbox::tests -- --ignored
```

Run records retain host, pushed base ref and resolved SHA, worktree, session ID,
final head, resume count, fast-check result, every review round, the last review
path/text/verdict/run ID, pushed ref and exit code. Exits are 0 accepted, 3 review
changes at the round stop, 4 no verdict, 5 base or card refusal (stale or unpushed base, `effort_invalid`,
`effort_needs_decision`, a `push_ref` outside `refs/heads/`), 6 writer failure,
7 final gate failure, 8 start disk refusal. Worktrees and logs remain for recovery.
Exit 9 reports `result: BLOCKED` when writer relaunches are exhausted or their
disk/base preparation fails.

Run `pg-spawnd resume RUN_ID --prompt-file PATH` on the writer's host for an
external fix round. Use `--state PATH` when the run has a custom state root.
The run must have ended and its worktree must still match its recorded head.
The command reuses the recorded Codex session, tool PATH, worktree and review
directory. It runs the same gates and exact-head review before publishing the
new result. The record keeps the previous outcome in `resume_from` and the
ledger appends a row for the fix round's usage.

Queue a scope change with `plangraph_agent_steer` using `run_id` and
`message`, or pipe the message into
`python3 -m plangraph.spawner.cli steer RUN_ID`. Both routes can take an
explicit configured host when the parent lacks a local receipt. The worker
prepends queued messages to its next Codex resume and records their delivery
times in `steer_messages`. The receipt reports queue acceptance immediately.

Native records include each Codex turn's start and end in `writer_turns` and
the review's `accepted_at` timestamp for the throughput collector.

The live Rust supervisor persists a `manifest` in the run record before launching
Codex: name (run ID), host, worktree, original brief, effort and `try_count`.
A killed/crashed CLI, failed CLI execution (including exit 127), or exit without a
`turn.completed` result event starts a new Codex session with the same brief.
Before relaunch it checks the existing disk floor, fetches and merges origin/main
into the same worktree, preserving writer commits. Dirty-tree or merge failures
stop with BLOCKED; no reset or cleanup discards work. There are at most three
writer attempts (two relaunches), separate from missing-review session resumes.
A relaunched session receives the open findings of the last REQUEST_CHANGES review;
round counts carry over, so the cap holds across relaunches.
The last writer failure remains in the record for the orchestrator.

This first relaunch slice covers failure of the writer child while the native
supervisor survives. Recovery after supervisor death or a host restart, including
startup manifest discovery and process-identity fencing, remains open. Persisted
manifests retain the inputs needed for that recovery; they are not yet replayed
by a replacement supervisor.
This slice uses the existing POSIX SSH overflow transport; Windows readiness,
worktree paths, disk probing and native process supervision need a later port.
The plan-order launcher scripts remain until the migration slice.

Overflow receipts keep target process identity under `remote_process`; origin
watchdogs never sample that PID locally. Cancel is forwarded to the target by run
ID. A failed SSH cancel leaves the last known run status intact with a query error.
Set `parent_session` on the card to receive terminal results in that session's
inbox on the supervising host. A refused launch also raises a Keeper finding
when the run is linked to an item. Native admission rechecks transient disk and
cache limits during its admission window (120 seconds by default). A refusal
after that window ends the attempt. The caller must submit a new launch.

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
`pg-spawnd` has no `--version` verb (advertised verbs include `probe`, `serve`, `shim`, `health`, `agents-view`, `ledger-report`, and `resume`).
Route listings never show `native-codex`: it is a card `run_kind` that spawn sends past routing
(`src/plangraph/spawner/cli.py`); native dry runs are refused (`src/plangraph/spawner/overflow.py`).
Check routing with a review dry run from a fresh `python3 -m plangraph.spawner.cli` process.
Make the first native-codex run after a move a small canary; check its run record for the exit code and review path before real writers use the spawner.

## Writer statistics ledger

Each terminal native writer attempt writes an immutable row in
`runs/<run-id>/ledger.jsonl`. The first row describes the initial run.
Explicit fix rounds append rows tagged with their `resume_number`.
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
