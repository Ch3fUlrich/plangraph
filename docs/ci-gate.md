# CI gate

`tests.yml` runs on every pull request to `main`, every push to `main`, and manual dispatch.

An edit confined to the title or description runs the base-pinned commit policy as
`ci-pr-description` in a concurrency group unique to that workflow run. The policy still
reads the edited title and description for Plan-Item coverage. The required `ci-gate` result
and any running tests are preserved. An edit that changes the base follows normal CI,
even when it also updates the title or description. Exact-head CI readers use the
run's metadata marker and its attempt's job names to exclude metadata checks from
test proof and rerun accounting. An unconfirmed run cannot supply green CI evidence.

An ordinary draft `lane/*` run starts only the literal `ci-gate` job. Its
`Draft lane without test proof` step runs without checkout, commit-policy
execution, selected suites, or a `ci-full` status. The run is titled
`Draft lane check <run-id>`. The CI wait, failure watcher, and legacy lander
exclude it only with the exact Actions attempt's successful witness and skipped
suites. A missing or contradictory witness withholds CI evidence. A draft lane
title or description edit still uses `ci-pr-description`, and a base edit takes
the ordinary suites and gate path. A manually imported timing report is outside
this admission check.

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
| `pytest` | Python tests, SQLite keeper (shard N of M) | `tests` (sharded), minus `tests/browser`, on the SQLite keeper | none: shards are disjoint; `tests/contract` also runs on Postgres, a different store |
| `pytest-postgres` | Python tests, Postgres keeper: contract and storage (shard N of 3) | `tests/contract`, `test_keeper_pg_*`, `test_projector_ddl`, `test_worker_claim`, `test_claim_parity_dispatch`, keeper tests that do not name sqlite3 | none: same files as `pytest` run on a different store on purpose |
| `browser` | UI tests in headless Chromium | `tests/browser` (excluded from `pytest`) | none |
| `rust` | Rust workspace on Linux: fmt, clippy, tests | fmt, clippy, windows-gnu check, workspace `cargo test`, `native_writer` again under Python 3.14 | `pg-spawnd --test native_writer` runs in the workspace test (Python 3.12) and again (Python 3.14): different toolchain, kept |
| `rust-windows` | Rust spawner and worker core tests on Windows | `cargo test -p pg-spawnd`, `-p pg-worker-core` | runs crates also tested by `rust`, on Windows: different platform |
| `rust-worker` | Rust worker crate: fmt, clippy, tests | standalone `rust/worker` crate (not in the workspace) | none |
| `rust-spike` | Rust replay spike tests | `rust/spikes/temporal-spike` replay test | none |
| `ci-gate` | ci-gate | aggregates the jobs above | none |


## Changed-test replay for the Python batch builder

`python3 tools/merge_batch.py build` requires five consistent observations of each applicable changed test in addition
to the part's hosted Tests run and successful `ci-gate`. Use the declared parent head as a stacked part's comparison
base. Use the fetched protected-branch head for an independent part. Generate separate receipts on the hosts and in
the environments selected by the part's changes.

```sh
python3 tools/ci_select.py entry-set --base BASE_SHA --head PART_SHA --replay --environment default
```

The replay uses a detached checkout of `PART_SHA`. It runs changed Python test files and Rust test functions whose
lines changed, following edited renames to their destination. Linux runs `default`, `postgres` (with `PG_DSN`), and
`browser`. The `windows` replay runs on native Windows and uses the Rust packages selected by the Windows CI job.
The builder compares the receipt's base, head, environment, and selected inventory to its own Git calculation.
A test skipped consistently in its environment counts as inapplicable. A missing observation, consistent failure,
or a mix of results refuses readiness. Mixed pass, fail, or skip outcomes are reported as `FLAKY`.

The producer and builder use the same `PLANGRAPH_TELEMETRY_DIR` (default `~/.local/share/plangraph/telemetry`).
For a replay on another host, place its receipt and preserved evidence in the builder's telemetry store.
Below `entry-replay/PART_SHA`, `ENVIRONMENT.json` is the latest receipt. Each replay keeps its own receipt
and separate stdout and stderr files. Their paths in the receipt are relative to the part directory.
An empty applicable inventory
needs no replay. The ordinary `entry-set --run` and hosted CI checks remain required where already selected.

This is the documented Python entry point. Train 68 used manual readiness. Active-train adoption and a live
receipt with the ordinary required CI result are still pending.
