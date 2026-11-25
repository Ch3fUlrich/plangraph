# CI gate

`tests.yml` runs on every pull request to `main`, every push to `main`, and manual dispatch.

The three Temporal CLI consumers share a SHA-pinned cache action keyed by OS, architecture and CLI version; cache misses retain the archive SHA-256 check and retry downloads across a six-minute window.

The `select` job (`tools/ci_select.py`) maps changed paths to suites: `scrub`, `pytest`, `pytest-postgres`, `rust`, `rust-worker`, `rust-spike`. Unknown paths, or a change under `.github/`, `requirements-ci.txt`, or `tools/ci_select.py`, run every suite; a docs-only change runs `scrub` only.

On self-hosted Linux runners (`CI_SELF_HOSTED=1` with `CI_LINUX_RUNNER_LABELS`, same condition as `runs-on`), the `rust`, `rust-worker` and `rust-spike` jobs set `CARGO_BUILD_JOBS` to the runner CPU count, build into a `CARGO_TARGET_DIR` and skip the `actions/cache` restore and save. Only a `push` to the default branch uses the persistent dir `$HOME/cargo-target/<repository>/<job>/<hash of the job's Cargo.lock and Cargo.toml files>` (keyed by the slot's OS user, since runners are single-use and the runner name changes every job); every other run, including same-repo pull requests, builds into an empty per-run dir under `RUNNER_TEMP`, so a pull request cannot plant artifacts that later main builds execute. The job's `CARGO_BUILD_JOBS` and `CARGO_TARGET_DIR` come only from `$GITHUB_ENV` steps (self-hosted or hosted), never from job-level `env`. The fork exclusion in the gate is expression-only and a fork pull request runs the workflow from its own merge commit, so it relies on the repository setting "Require approval for all outside collaborators' workflow runs" being on. `RUST_TEST_THREADS` is set to 8 there to avoid launch-test flakes on many-CPU runners. Directories of older hashes are deleted first, then the job's directory is deleted when it exceeds `vars.CI_CARGO_TARGET_CAP_GB` (default 40 GB; a non-numeric value counts as 40). `PG_JAIL_BIN` and the admission-guard target follow `CARGO_TARGET_DIR`. Hosted runners keep `CARGO_BUILD_JOBS=4` and the cache unchanged.

The `ci-gate` job fails if any selected suite failed, was skipped, or was cancelled.

A non-doc change satisfies the changelog check with a new or edited direct-child `changelog.d/<ITEM-KEY>.md` fragment that adds an entry line against the merge-base (excluding README.md). Ordinary PRs must not edit `CHANGELOG.md`, even for docs-only changes or with a skip trailer. `tools/changelog_fold.py` folds fragments in a release commit recognized by a nonempty `Release: <version or description>` trailer in its final trailer paragraph. CI and the Rust writer gate check each commit, including merge-resolution edits.

Branch protection on `main` requires only the `ci-gate` check — no review requirement, admins not enforced. Rollback: delete the protection.

A `Commit trailers` step (`tools/ci_commit_gate.py`) checks each PR commit for a non-Claude `Writer:` trailer — the Writer may name a Claude model only as `claude-<model> <run|session|uuid id> [(note)]` (shape-only check; `claude-<model>` and the run-hex id are lowercase only, a uuid may be any case; the optional note is one parenthesised span that may not contain `)` (so no nested parens), a newline, or a second `Writer:` marker in any case) — and a `Test-Removal:` trailer when tests are removed. Exempt shas are listed in `tools/ci_commit_exempt.txt`.

## Known limits of the commit gate

- Gate-Edit and Test-Removal ids are checked for shape (D-<digits>) only, not against the decision log.
- The first push of a new ref (all-zero before sha) is not gated, a pull request into main still is.
- Merge commits are skipped for the Writer and test rules, their parent commits are checked. A merge that edits a gate file itself (a conflict resolved in `.github/workflows/*`, seen in its combined diff or in the conflict list of its two parents, so keeping one side whole counts too) needs its own `Gate-Edit: D-<digits>` trailer; a clean main-into-lane merge does not. The pre-push hook and the CI step run this, and `merge_batch build` runs the workflow Gate-Edit check on every PR head before it builds.
- The pre-push open-PR check identifies the pushed branch's own PR by the same-repository head branch and the remote tip. For landing trains and batch branches it reads the part table in that PR's body and accepts matching heads that are already in the pushed history. A batch can also use the PR numbers and heads recorded by `merge_batch`, including on its first push. Foreign open PRs still participate in the merge check.
- The Writer may name a Claude model only as `claude-<model> <run|session|uuid id> [(note)]` (shape-only check; `claude-<model>` and the run-hex id are lowercase only, a uuid may be any case; the optional note is one parenthesised span that may not contain `)` (so no nested parens), a newline, or a second `Writer:` marker in any case); the Claude marker list otherwise matches the Writer value by substring and relies on the writer naming itself.
- Copies of gate files under other names are not path-matched (only suffix variants such as .bak are).
- Test removal is detected by name in files whose path looks like a test (tests/ directories, test_* files, *_test.* and *.test.*/*.spec.* files), unconventional locations and deleted conftest.py fixtures are not detected.
- The ci-gate step wiring is checked by tools/gate_wiring_check.py, which the merger runs before merging (it compares the ci-gate job and triggers with the base and demands a Gate-Edit trailer, in the trailer block, on every commit that touches the workflow file; it cannot verify the id).
- Hand-Edit commits are capped at 50 changed lines (interim ceiling, operator 2026-10-25), may not touch the auth/door surface and are listed in the gate log.
- For a Writer-trailer commit the gate checks the trailer text, not that a writer really ran.

`ci_commit_gate.py check --base BASE --head HEAD --item-text PART.txt` warns
for each keeper key named in a UTF-8 part/PR description but absent from writer
commit `Plan-Item` trailers in BASE..HEAD. In GitHub CI the PR title and body
come from `GITHUB_EVENT_PATH`; local callers supply `--item-text`. Coverage is
advisory: missing keys print warnings, while unreadable text or Git failures
fail the check. The policy runs in Rust (`pg-policy --plan-item-trailers BASE HEAD`)
and requires Cargo. Use a trailer per fixed key, including subitems of a parent;
legacy `Closes:` tokens and merge-only trailers do not count. Named references
that the PR does not fix can also warn and should be checked by the reviewer.

## Job display names and redundancy audit

Job ids in `tests.yml` stay fixed (`needs`, `tools/ci_select.py` outputs and `tools/dell_ci_jobs.py` use them). The `name:` of each job is what a pull request lists and what the check-run API reports; `ci-gate` keeps its name because branch protection uses that context. `tests/test_ci_job_names.py` fails when a job has no display name, two jobs share one, or `tools/merge_batch_land.py` (`JOB_DISPLAY`, `SHARD_DISPLAY`) disagrees with the workflow. Shard numbers in the names count from 1.

Audit of what a full run executes (no job was merged or re-wired; that needs a router row). A test is marked duplicate only when it runs twice on the same store, platform and toolchain.

| Job id | Display name | Runs | Same-store/platform duplicate |
| --- | --- | --- | --- |
| `select` | Select suites for this change | `tools/ci_select.py` | none |
| `scrub` | Public scrub: no private names or secrets | scrub tool | none |
| `file-size` | File size limit (400 lines) | `tools/check_file_size.py` | none |
| `new-code-lang` | New code is Rust | `tools/check_new_code_lang.py` | none |
| `changelog-docs` | Changelog fragment and docs | `tools/check_changelog_docs.py`, commit gate | none |
| `build-spawnd` | Build pg-spawnd once for the run | `cargo build --release -p pg-spawnd` (also builds `ci-flake`); uploads the `pg-spawnd-bin` artifact every pytest shard downloads | none: the only pg-spawnd release build of a run |
| `pytest` | Python tests, SQLite keeper (shard N of M) | `tests` (sharded), minus `tests/browser`, on the SQLite keeper | none: shards are disjoint; `tests/contract` also runs on Postgres, a different store |
| `pytest-postgres` | Python tests, Postgres keeper: contract and storage (shard N of 3) | `tests/contract`, `test_keeper_pg_*`, `test_projector_ddl`, `test_worker_claim`, `test_claim_parity_dispatch`, keeper tests that do not name sqlite3 | none: same files as `pytest` run on a different store on purpose |
| `browser` | UI tests in headless Chromium | `tests/browser` (excluded from `pytest`) | none |
| `rust` | Rust workspace on Linux: fmt, clippy, tests | fmt, clippy, windows-gnu check, workspace `cargo test`, `native_writer` again under Python 3.14 | `pg-spawnd --test native_writer` runs in the workspace test (Python 3.12) and again (Python 3.14): different toolchain, kept |
| `rust-windows` | Rust spawner and worker core tests on Windows | `cargo test -p pg-spawnd`, `-p pg-worker-core` | runs crates also tested by `rust`, on Windows: different platform |
| `rust-worker` | Rust worker crate: fmt, clippy, tests | standalone `rust/worker` crate (not in the workspace) | none |
| `rust-spike` | Rust replay spike tests | `rust/spikes/temporal-spike` replay test | none |
| `ci-gate` | ci-gate | aggregates the jobs above | none |

## Reusing the green run of a reworded train commit (rule 3.42)

Rule 3.42 a runs the full suite once on the merge candidate; a reword that leaves the Git tree unchanged does not change what was tested.
On a `pull_request` run the select job passes the previous head (`github.event.before`), its commit statuses, its tree, the Actions run
record of the run its `ci-full` status names and that run's jobs of the same attempt, with the tree this run checked out (the merge with the current base), to `tools/ci_select.py select` (`--old-head --old-tree
--old-run --old-jobs --statuses`). `reword_evidence` reuses that run only when both commits have the same non-empty tree, the newest `ci-full`
of the old head is a success for that tree published by `github-actions[bot]`, the run record shows the same run id, the old head, this
workflow on a pull request, the latest attempt, concluded success, and the jobs of that attempt are every job of the protected matrix of the
runner tier it used (`deploy/ci-watch/matrix.toml`: four SQLite shards self-hosted, three hosted), all green, so a green affected-only run or a
run missing a shard never qualifies. A base that moved since the old run changes the checkout tree, so it never reuses the run. The whole
reuse is off until the repository variable `CI_REWORD_REUSE` is `1`, which waits for the verifier binding below. It prints `tested=true` and `evidence=run R
attempt A tree T reused from <old> for <new>` and keeps `full=true` with every suite off: `ci-gate` accepts that like a landing record, and
its `Verify computed GO` step still runs. A GO evidence file is bound to the head of the run, so for the reworded head the verifier refuses it
(`ci_unbound`) until a granted change of `go_verify.rs` binds the two heads; reuse never skips the verifier. The reworded head gets its own
`ci-full` status with the same run and tree; the run did not test a suite, so `Post ci-full evidence` is skipped for it. A different tree, a
missing or unverified run, or a newer or red attempt runs the suite again.

## Entry replay of changed tests (LE-CI-FLAKE-ENTRY-REPLAY)

`tools/ci_select.py entry-set --run` also replays the tests a part changed: every `tests/**/test_*.py` it added or changed (not `tests/browser`) and
every Rust `#[test]` fn whose lines it touched, a body-only edit included, five runs each in a clean detached checkout of the part's own head (verified to be that commit; the working tree never counts). The Python files run four
times serially and once with `-n auto` (the full-worker run); each Rust test runs four times alone with `--test-threads=1` and its fifth run is the whole crate with the default
threads, beside its peers. Every log is kept with `replay.json` (`python-run-N.log`, `cargo-run-N.log`) under `--replay-dir`, by default
`<telemetry>/entry-replay/<head>` (`PLANGRAPH_TELEMETRY_DIR`). A test that passed and failed among the runs is printed as
`entry-set: FLAKY: <test id> (logs <failing log> and <passing log>)`; a test that fails every run, or a run with no test outcome, is `FAIL`.
A run is `failed` when its process exits with anything but 0 or 1 (pytest) or 101 (cargo), even if it printed passes; a test pytest reports `SKIPPED` on the platform is not applicable and needs no outcome. A run that reports no outcome for any collected test id of a changed file (not just one id of it) or for a changed Rust test is `incomplete`, so five invocations that skipped the test are no receipt. Either one makes the entry run fail. The receipt covers the invoking environment only; Postgres-backed Python tests and the native Windows Rust run are a follow-up slice. `replay.json` is the receipt bound to the head: `merge_batch build` readiness (`ready_problems`) needs a
passing five-run receipt for the exact head of every part that changes a test or Rust file, and names `FLAKY: <test id> (logs ...)` from it.
`replay.json` also records the changed-file list of the diff it replayed, and readiness refuses a receipt whose list differs from the part's own (a replay against another comparison base), and recomputes the part's test inventory from its own diff (`changed_tests` against the batch base),
refusing a receipt that covers fewer or other tests. A test id keeps its full node id (a parameter id may hold ` - `); each test has one outcome
per run, and a failing teardown after a pass makes it failed, never flaky. Green CI alone never stands for the replay.

## CI speed and flake handling (tests.yml)

- **One build, shared.** `build-spawnd` builds `pg-spawnd` and `ci-flake` once per run; the SQLite and Postgres pytest shards download the
  `pg-spawnd-bin` artifact (`.github/actions/fetch-spawnd`) instead of building. The artifact carries a manifest (`pg-spawnd.sha256`,
  `pg-spawnd.source`); the action checks the digests and that the binary was built from this run's checkout, exports
  `PLANGRAPH_TEST_FLEET_SPAWND` (the fleet fixture cannot judge an artifact without Cargo dep-info) and every shard runs
  `test_fleet_samples_and_missing_host` on the downloaded binary, after the quarantine ledger is pinned (an active entry for it leaves that run). Cargo caches (`rust`, `rust-windows`, `rust-worker`,
  `rust-spike`, `build-spawnd`) are restored on every run with a `restore-keys` prefix and saved only by a push to the default branch, so a
  pull request starts from main's build and never plants one.
- **Balanced shards.** Every shard of a suite reads the same pinned map, `.github/pytest-durations-sqlite.json` or
  `.github/pytest-durations-postgres.json` (`PYTEST_SHARD_DURATIONS_FILE`; `{}` weighs every test 1 s), and each green shard writes its own
  `PYTEST_SHARD_DURATIONS_OUT` and uploads `durations-<suite>-<shard>`. On a full run `ci-gate` merges them per suite (`ci-flake
  combine-durations`) into the `pytest-durations-combined` artifact; committing that file is the pin for the next train.
- **Same-SHA retry.** A red pytest or `cargo test` step stays red. A step after it reruns the failed test ids once on the same SHA
  (`.github/actions/pytest-retry`, `.github/actions/cargo-retry` for the workspace, worker and native Windows Rust jobs; the affected path of a pull request too: CI sets `CI_CLASSIFY_RETRY`, which turns off the reruns of `tools/affected_tests.py`, so there is exactly one retry and the first log is kept). The retry's own exit status is captured and the classification always runs and `ci-flake classify` writes `flake-record.json` (runner, run, attempt, SHA, test ids, both
  logs) and `flake-items.json` (a priority-10 Keeper item definition per fail-then-pass test) into the `flake-*` artifact and the job summary.
  Fail-pass and fail-fail are both red; more than 50 failures are not retried. A test whose retry reports a pass and a teardown error is a failure; an xdist `@group` suffix is not part of the test id. The Rust blocking run excludes an active entry by package and test (that package runs alone with `--skip`).
- **Quarantine.** Every run pins one UTC timestamp (`select` output `as-of`) and the ledger `.github/pytest-quarantine.json` of the pull
  request's or merge group's base (`.github/actions/quarantine-ledger`; a shallow checkout fetches the base, and a base that cannot be read fails the job, only a base without a ledger file means none). The blocking pytest and `cargo test` steps leave out the active entries; shard 0
  (and the `rust`, standalone worker and native Windows jobs, and the Python 3.14 native-writer run, which skip an entry of their own package in the blocking run) runs only them in a reporting step that never blocks (bounded like every pytest step); `ci-gate` publishes the owner and expiry of every entry in its
  summary. An entry past its expiry blocks again. A Rust entry is `cargo:<package>:<test path>`.

## The native Windows test entry point

The `rust-windows` job and the pre-push Windows check (`merge_batch_plan.windows_commands`, which reads the job's steps) run one command,
`ci-flake cargo-test --package <package>`: a plain `cargo test --no-fail-fast -p <package>`, except that with the pinned ledger
(`PYTEST_QUARANTINE_FILE`, `PYTEST_QUARANTINE_AS_OF`) it leaves the active quarantined tests of that package out (`--exact --skip`), and
`--quarantine-only 1` runs only them for the reporting step. The step keeps no Bash array, pipe or redirect, so nothing of it reaches PowerShell;
`--log FILE` keeps the output for `cargo-retry`. `tests/test_merge_batch_windows.py` checks the commands read from the real workflow.
