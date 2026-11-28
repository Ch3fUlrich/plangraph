# CI gate

`tests.yml` runs on every pull request to `main`, every push to `main`, a merge queue event and manual dispatch; there is no scheduled run.

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

The `select` job (`tools/ci_select.py`) maps changed paths to the suites `pytest`, `pytest-postgres`, `rust`, `rust-worker`, `rust-spike` and `browser`; `scrub`, `file-size`, `new-code-lang` and `changelog-docs` run on every change. `rust` also starts the native Windows job. Unknown paths, or a change under `.github/`, `requirements-ci.txt`, or `tools/ci_select.py`, run every suite; a docs-only change runs only those four fast jobs and `ci-gate`, except a Rust input under `docs/` (`RUST_INPUTS`, for example `docs/flow-watchdog.md`), which also selects `rust`. A change only under `.github/workflows/` selects `pytest` and `rust`. A draft `lane/*` pull request, and a title or body edit, skip `select` through the `if:` at `tests.yml:36`.

The full suite runs exactly once, on the merge candidate into `main` (`full_run_required`): a `land/train-*` head or a pull request labelled `ci-full` at merge time (not a `land-part` part), both targeting `main`, or a manual dispatch. Every other event runs the affected selection of its own diff: `batch/*` heads, parts, lanes, the merge queue, and the push to `main`, which selects from `github.event.before` to `github.sha`. The Python jobs then run `tools/affected_tests.py --base <select base>`; the Rust Linux job runs clippy and `cargo test` only for the select output `rust-packages` (changed crates plus the crates that depend on them, from `tools/preflight.py` `rust_commands`), and the Windows job only the listed crates among `pg-spawnd` and `pg-worker-core`. An empty list, on a full run or when a Rust input outside `rust/` changed, tests the whole workspace.

Skill text (`skills/**/*.md` and `skills/<name>/evals/evals.json`) selects only `pytest`, whose affected run picks the skill tests; the skills that Rust embeds keep their Rust jobs, and a skill eval file beside any non-text path runs the full suite. A push to `main` of a merge commit whose first-parent diff (renames split into delete and add) is only skill text and Markdown that no Rust, worker, unit or CI check reads runs the fast jobs only and reports `tested=true` with evidence `text-only merge`; any other path runs the affected selection (D-7122).

On self-hosted Linux runners (`CI_SELF_HOSTED=1` with `CI_LINUX_RUNNER_LABELS`, same condition as `runs-on`), the `rust`, `rust-worker` and `rust-spike` jobs set `CARGO_BUILD_JOBS` to the runner CPU count, build into a `CARGO_TARGET_DIR` and skip the `actions/cache` restore and save. Only a `push` to the default branch uses the persistent dir `$HOME/cargo-target/<repository>/<job>/<hash of the job's Cargo.lock and Cargo.toml files>` (keyed by the slot's OS user, since runners are single-use and the runner name changes every job); every other run, including same-repo pull requests, builds into an empty per-run dir under `RUNNER_TEMP`, so a pull request cannot plant artifacts that later main builds execute. The job's `CARGO_BUILD_JOBS` and `CARGO_TARGET_DIR` come only from `$GITHUB_ENV` steps (self-hosted or hosted), never from job-level `env`. The fork exclusion in the gate is expression-only and a fork pull request runs the workflow from its own merge commit, so it relies on the repository setting "Require approval for all outside collaborators' workflow runs" being on. `RUST_TEST_THREADS` is set to 8 there to avoid launch-test flakes on many-CPU runners. Directories of older hashes are deleted first, then the job's directory is deleted when it exceeds `vars.CI_CARGO_TARGET_CAP_GB` (default 40 GB; a non-numeric value counts as 40). `PG_JAIL_BIN` and the admission-guard target follow `CARGO_TARGET_DIR`. Hosted runners keep `CARGO_BUILD_JOBS=4` and the cache unchanged.

The `ci-gate` job fails if any selected suite failed, was skipped, or was cancelled.

A non-doc change satisfies the changelog check with a new or edited direct-child `changelog.d/<ITEM-KEY>.md` fragment that adds an entry line against the merge-base (excluding README.md). Ordinary PRs must not edit `CHANGELOG.md`, even for docs-only changes or with a skip trailer. `tools/changelog_fold.py` folds fragments in a release commit recognized by a nonempty `Release: <version or description>` trailer in its final trailer paragraph. CI and the Rust writer gate check each commit, including merge-resolution edits.

Branch protection on `main` requires only the `ci-gate` check — no review requirement, admins not enforced. Rollback: delete the protection.

A `Commit trailers` step (`tools/ci_commit_gate.py`) checks each PR commit for a non-Claude `Writer:` trailer — the Writer may name a Claude model only as `claude-<model> <run|session|uuid id> [(note)]` (shape-only check; `claude-<model>` and the run-hex id are lowercase only, a uuid may be any case; the optional note is one parenthesised span that may not contain `)` (so no nested parens), a newline, or a second `Writer:` marker in any case) — and a `Test-Removal:` trailer when tests are removed. Exempt shas are listed in `tools/ci_commit_exempt.txt`.

## Where CI time goes

Measured from the 485 runs of `tests.yml` created 2026-11-21 00:07Z to 2026-11-28 03:15Z (GitHub API runs 44841710542 to 46101977982, job rows per run; wall = run start to last update). Every job ran on a GitHub-hosted runner (`ubuntu-24.04` 7196 jobs, `windows-latest` 459): the repository variables `CI_SELF_HOSTED`, `CI_SELF_HOSTED_WIN` and `LAND_CREATOR_ID` are unset on 2026-11-28 (only the two `*_LABELS` variables exist), so the self-hosted paragraph below and the `tested-ff` push shortcut in `ci_select.py` do not apply today.

| Event | Runs | Median wall | p90 wall | Job-minutes | Share of full job set |
|---|---|---|---|---|---|
| pull_request | 402 | 15.4 min | 23.6 min | 32168 | 324 of 402 ran 14 or more jobs |
| push (main) | 67 | 15.2 min | 20.1 min | 6984 | 67 of 67 full |
| schedule | 7 | 15.8 min | 17.8 min | 684 | 7 of 7 full |
| workflow_dispatch | 9 | 14.1 min | 26.7 min | 928 | 9 of 9 full |

Total 40764 job-minutes. Queue wait is small (median 0.05 min from run creation to first job start, p90 0.87 min); wall time is run time. Pull request outcome: 204 success, 118 failure, 79 cancelled (20 percent, 1725 job-minutes in cancelled jobs), 25 runs are reruns (attempt above 1).

Top jobs by total minutes (median / p90 minutes per run):

| Job | Job-minutes | Median | p90 |
|---|---|---|---|
| Rust workspace on Linux | 6682 | 13.9 | 20.4 |
| Rust spawner and worker core tests on Windows | 5157 | 11.8 | 17.1 |
| Rust worker crate | 4522 | 11.1 | 15.7 |
| Python SQLite keeper, 3 shards | 12489 | 9.5 | 13.8 |
| Python Postgres keeper, 3 shards | 9018 | 5.9 to 7.6 | 10.5 |

The Python jobs (SQLite plus Postgres, six shards) are 53 percent of all minutes, the three Rust jobs 40 percent. The critical path of a full run is the Rust Linux job (14 min), so shards do not shorten the wall time.

Duplicate and wasted work:

- Landing trains: 148 `land/*` pull request runs (27 percent of minutes): 60 success (6051 min), 39 failure (3641 min), 49 cancelled (1419 min). Only 41 percent of train runs end green.
- Every merge repeats the train's full run on the push to `main`: 67 pushes, 6984 job-minutes (17 percent of all), median 99.5 job-minutes each, about 2 minutes of them in the fast jobs. Pushes per day: 3 to 16.
- Non-draft `lane/*` pull requests: 157 of 179 runs ran 14 or more jobs (88 percent), 15284 minutes. A changed `src/`, `tools/` or `tests/` path selects both Python suites, and any `rust/` path selects both Python suites too, so an affected selection is near the full set.
- Windows runs on 496 job rows (5157 minutes, 13 percent): it follows `rust`, so every `rust/` and Rust-input path pays 12 minutes.
- Repeat runs: 356 distinct pull request head shas, 37 of them with more than one run (46 extra runs, 11 percent); 255 branches averaged 1.58 runs, 26 branches ran three or more.
- Failed runs: 118 of 402 pull request runs (29 percent); `ci-gate` is the failing job in 130 job rows, behind it the Postgres shards (55), Rust Linux (48), Windows (41) and the SQLite shards (97).

Selection check (`python3 tools/ci_select.py select --files ...`): `docs/` and `*.md` select nothing, except the Rust inputs in `RUST_INPUTS` (`docs/flow-watchdog.md` selects `rust`); `skills/*/SKILL.md` selects `pytest`, plus `rust` for a skill that Rust embeds (`skills/batch-writer/SKILL.md`); `skills/*/evals/evals.json` alone selects `pytest`, and every suite with `full=true` beside a non-text path (D-7122); `tools/ci_select.py` selects every suite; `rust/plangraph-cli/**` selects `pytest`, `pytest-postgres` and `rust` (and Windows); `src/plangraph/**` or `tests/**` selects `pytest` and `pytest-postgres`.

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
