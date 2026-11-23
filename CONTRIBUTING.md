# Contributing

Run `python3 tools/preflight.py --base origin/main --head HEAD` before pushing:
it blocks on scrub, CI policy (including commit trailers), changed-crate Rust checks,
and affected tests, in that order. Each family ends with a timed PASS/FAIL/SKIP/INFRA
summary; failed checks with HTTP 5xx output in any family (including policy) are
inferred as INFRA and retried once; persistent INFRA exits 3, other failures exit 1.
The Rust step prints `CARGO_TARGET_DIR` (or its default `rust/target`) before running
Cargo. pg-spawnd CLI integration tests reuse Cargo's executable unless
`CARGO_TARGET_DIR` (or `CARGO_BUILD_TARGET_DIR`) points outside the checkout. For
that shared case they build into `<shared target>/cli-tests/<checkout path hash>`,
so another checkout cannot replace their CLI and extra output stays under the shared cap.
Preflight pins Cargo to `PLANGRAPH_CARGO_TARGET_DIR` (default
`~/.cache/plangraph/cargo-target`), disables incremental builds and holds the
shared cleanup lease. It checks the host cache cap before and after each Cargo
command; never override this with a private target. See [disk limits and
cleanup](docs/operations/spawner-disk-admission.md). For md-sync changes, use a temporary `XDG_STATE_HOME` when testing repeated syncs so the invalid-row diagnostics ledger starts empty.

On Linux, preflight calls `pg-spawnd host post-run-cargo` after all checks
finish and the build leases have been released. Older pg-spawnd installs
skip this extra trim until upgraded. The helper trims idle
build entries first and can reclaim an identified debug profile if the
shared target still exceeds its cap.

Rust packages outside the `rust/` workspace (including `rust/worker`) run through
their owning `Cargo.toml` with `--manifest-path`, including excluded packages
without a `[workspace]` table. Worker tests use `--lib --bins` to stay offline.

Small checks have a 60-second timeout; cargo clippy/test have 600 seconds each and
file-only affected preflight has 240 seconds (`TEST_TIMEOUT`) for selection and pytest together. Preflight sizes pytest-xdist from the host using half the cores, at most one worker per 2 GiB of available memory, and between 2 and 16 workers. If available memory cannot be read, preflight uses the core count alone. `PREFLIGHT_TEST_WORKERS` overrides the count. A timeout reports the step, deadline, and selected test count. A scrub FAIL skips the test step (summary line `SKIP tests`). The hook has no additional overall preflight bound.

When the local affected selection includes the fleet projection tests, the selector
builds `pg-spawnd` from the selected checkout before pytest starts.
`tools/pgtest.py` runs the bounded test subprocess and handles process-tree
cleanup when it times out. Preflight chooses the deadline and reports failures.
When preflight selects `tests/test_hooks_prepush_paths.py`, it builds and stages
one `plangraph` CLI before starting the 240-second step. Every pytest worker
uses that executable through `PLANGRAPH_TEST_CLI`. Direct pytest and selector
runs build the CLI once per pytest session when the variable is unset. Set it
to a prebuilt executable's path to reuse a build.

The Rust step bound is `max(600, planned test binaries x 15)` seconds times `1 + load/cores`, capped at 3600 (`rust_bound`), and a timeout names the test binary that was running. `CARGO_BUILD_JOBS` follows the free cores (cores minus load, floor 4) unless it is set. `cargo test` runs with `--no-fail-fast`, the Rust family runs every cargo check, and the Python tests still run after a Rust failure, so one pass lists every failing Rust test (`failing test:` lines) and every Python failure. The affected-test selector takes `--parts-union` (preflight passes it): a pushed batch or train selects the tests of every part's changed files. Standalone preflight uses the train and base first-parent histories to find the range.

The affected-test selector can also run changed or added `tests/test_keeper_*.py`
files with `PG_KEEPER_BACKEND=postgres`. The optional throwaway cluster requires
`initdb`, `pg_ctl`, and `createdb` from `PG_BIN`, `PATH`, or standard install
directories, plus `psycopg` in the test interpreter. If the binaries or
`psycopg` are missing, it prints `keeper-postgres: SKIPPED`. An explicit
`PG_DSN` runs the tests against that database and reports missing dependencies
or test failures as errors.

Keeper tests insert many rows with `World.execute_many`; `tests/test_keeper_tests_lint.py` refuses `w.conn.executemany`.

The pre-push hook has two explicit paths and runs no test on either (`preflight --lane`: scrub, commit policy, file size and document headroom, new-code language, changelog/docs-map, skill rule lint, and logged `cargo fmt --check` and `cargo clippy --all-targets -- -D warnings` on the shared target for affected crates). The affected tests run in CI. For local train fixes, use the [bounded selector](docs/TESTING.md); the Bash guard rejects whole-tree pytest on lane and train branches. A lane push is a branch head outside `main` and `phase/*`. Phase pushes keep file-only affected preflight. A direct push to `main` or a tag keeps the full preflight. Use `plangraph land-train push --repo DIR --branch land/train-NAME` for a built train. The wrapper passes the builder's ordered part-head manifest through `PLANGRAPH_TRAIN_MANIFEST` and runs fast preflight through the installed hook. The hook uses `plangraph land-train check-chain` on `PATH` to check that each two-parent merge above the merge-base with `origin/main` matches the manifest in order. Replay verification uses the same validator and binds standalone resolver diffs and attribution to the review artifact. Single-parent `Train N: resolve` and `Train N: drop` commits require `Writer` and `Harness` trailers. The first push uses the manifest check. Subsequent pushes also check the existing PR's folded parts against foreign open PRs. Scrub and commit policy still run.

A part (a batch waiting for a train) enters a train only after its own head passed: the seconds-fast checks (`preflight --lane`), and the entry set (`tools/ci_select.py entry-set --base B --head H --run`: suites from `select`, Rust crates and their reverse dependencies from preflight, Python tests from the affected selector, whose unknown-input fallback is the full suite, rule 3.9); a change that selects Rust CI, a Python-only quota change included, also needs the native Windows run (`windows` in the plan). The latest completed CI attempt on the head must not be red, and a red attempt followed by a green one clears only with a recorded flake disposition (rule 3.46; `attempt_problems`, `ready_problems`). The workflow separates the two roles: a pull request labelled `land-part` runs the affected-only mode, even on a `batch/*` head or with a stray `ci-full` label; a `land/train-*` head always runs the full suite once. `merge_batch push --ci-run` (and `tools/ci_select.py part-cost`) appends, per merged part from the Tests run matching its PR number and head and once for the candidate, queue seconds, run seconds and cold/warm runner to `part-cost.jsonl` in the telemetry directory. `pg-policy --part-entry BASE HEAD PARENT|- [OTHER...]` refuses a part that merges foreign history (`imported_history`), is built on another open head without naming it (`undeclared_dependency`) or shares commits outside the base with one (`shared_history`); `merge_batch build` runs that predicate on every part against all open pull request heads, and the readiness verdict from each part's own CI on its head (ci-gate stands for the fast checks, entry suites and Windows job; a rerun that turned green needs the `flake-dispositioned` label); a refusal stops the build unless `--force-set`, which logs it. `.gitattributes`, at any depth, is a gate path that needs a `Gate-Edit` trailer.

`tools/merge_batch.py push` runs the Windows tests of the batch head before it pushes: when CI would run the Windows job (any change that selects Rust, a Python-only one included, and always for a `batch/*` or train head, where CI runs the full suite) and `PLANGRAPH_WINDOWS_HOST` names the native Windows ssh host (ws-build), it sends the head as a git bundle, clones it into a disposable directory with its own capped cargo target, runs the `cargo test` commands of the `rust-windows` job of `tests.yml` (parsed from the workflow with PyYAML from `requirements-ci.txt`) with `--no-fail-fast`, and removes the checkout. A red run refuses the push and names the failing test ids; a green one stores `windows ok head=... host=... commands=N seconds=S`, which `go-req` prints (nothing is added when none ran for that head).

`merge_batch build` requires a successful full Tests run on the exact head of
each part that changes shared test execution, setup, fixtures, or workflows.
This includes the Rust toolchain pin and shared deadline and process-group code.
The part table shows the run ID. This requirement also applies with `--force-set`.

Outside lane preflight, with sccache on PATH and the effective `RUSTC_WRAPPER` being sccache, the Rust step builds in a private per-run `CARGO_TARGET_DIR` that is deleted at the end (also on failure) when free disk is at least `PLANGRAPH_PREFLIGHT_MIN_FREE_GB` (default 20); without sccache, or with less disk, it prints one line and uses the shared target. Orphaned `preflight-cargo-*` dirs older than 12 hours are pruned before a private target is created. `RUSTC_WRAPPER=sccache` is set only when unset; `SCCACHE_CACHE_SIZE` comes from `PLANGRAPH_SCCACHE_CACHE_SIZE` (default 20G). Before any build preflight trims the shared target (over `PLANGRAPH_CARGO_TARGET_CAP_GB` it is removed; `cli-tests/<hash>` builds are kept to the newest `PLANGRAPH_CLI_TESTS_KEEP`, default 3, and 24 h), then builds pg-policy once into `policy-bin/<source hash>` of that target (equal sources share a warm build, different sources never share an artifact; the newest 3 are kept) and hands a private copy to the policy step as `PG_POLICY_BIN`, outside the 60-second check bound. See docs/TESTING.md.
`tools/merge_batch.py land` merges the batch PR with one `Plan-Item` trailer per item of every included part and for the batch `--plan` key, plus one `Review-Run: KEY RUN_ID PART_HEAD` line per recorded part review, in the merge commit body, and runs `plangraph merge-close` once after the batch landed, so the keeper closes the items its merge names; the job's report goes to stderr and a failure or a missing binary never fails the land.
`tools/merge_batch.py land` lands in the mode of the `landing.mode` setting of the batch's repository (`tested-ff`, `train-pr-merge`, `single-pr`; D-6540, details in `docs/operations/spawner-land-go.md`). `tested-ff` pushes the batch head with `--force-with-lease` on the tested base after the same gates and reads the outcome from main (no merge commit); `train-pr-merge` merges the batch pull request as above; `single-pr` lands one part only. An unknown mode refuses the land before any GitHub call. Every landing writes a receipt and a telemetry row naming the mode (`landing-report`, `landing-settle`, `landing-backfill`); a landing without its row fails. `tools/ci_select.py select --event push --statuses FILE --tree T --land-creator-id ID` runs only the fast jobs for a commit with the land App's `land-auth` and a matching `ci-full` status, and the full suite otherwise.
With Keeper configured, `push` journals the open train manifest and `review` refreshes it for a pushed train. `land` checks keeper dependencies before merging. Each dependency must be done, present in the same train, or recorded as included in an earlier landed train. `abort` marks an unmerged train abandoned. Run `tools/merge_batch.py settle` from a checkout of the same repository to reconcile trains. The lander freezes the mode and receipt inputs before attempting to land. The journal can supply those inputs if local state is lost. Tested fast-forward settlement requires an explicit seat-policy acknowledgement at item closure. Settlement leaves open PRs pending, marks closed unmerged PRs abandoned, and verifies merged commits on main before saving the checked part outcomes and advancing the cursor. The [keeper deployment notes](docs/keeper-postgres.md#schema-and-migrations) cover host verification, guarded writer stops, and the landing timeline.
`tools/merge_batch.py push` also waits without an overall deadline and forwards
git and hook output to stderr while the hook enforces its per-step bounds.
The Rust step resolves `protoc` (`PROTOC`, then `~/.local/bin/protoc`, then `PATH`) before cargo when a target pulls a protobuf build script, and refuses within a second with a named message when it is missing. The shared subprocess runner kills the process group on a caller-supplied
timeout or interruption, including nested worker sessions on Linux, then reaps
git before reporting the failure.
When a Rust compile check fails in the shared target, preflight logs the clean target
and reruns the check there once. It uses that result and keeps the clean target for
the remaining Rust checks in the run.

The commit policy reads its exemptions from the base commit and checks the entire
stack against `origin/main`.
Within the policy family every check runs and all failures are printed, followed by a `policy: N check(s) failed:` block with each failed check's first error line. Use `--keep-going` to collect all failures or `--fast`
to skip affected tests in a direct run. The pre-push hook runs preflight with
file-only affected tests for the pushed revision, using the pushing checkout's
virtual environment when present.
On landing train refs (`train/*` and `land/train-*`), `PREFLIGHT_FAST=1` forwards `--fast`.
On `land/*`, `PREFLIGHT_SKIP=1` reports that the local test stage is skipped and
still runs the fast push gates. Phase pushes ignore that legacy flag and run
affected preflight. Other refs refuse the flag. On a train push, a missing
`plangraph` binary is reported by name. An attribution failure identifies the
resolver commit and the invalid trailer.
Private scrub words come from `PLANGRAPH_SCRUB_WORDS` or `~/.config/plangraph/scrub-words.txt`;
when neither is present, public rules still run but scrub reports SKIP with a reason.
`PREFLIGHT_TOOLS_DIR` overrides tool paths in direct preflight runs.
The revision controls `FILESIZE_REQUIRE_BASE`, `NEWCODE_BASE` and `CHANGELOG_BASE`
are scoped to policy checks. Other families receive none of these controls,
including inherited values, so nested Git test fixtures own their revision bases.
Preflight clears every name reported by
`git rev-parse --local-env-vars`, all `GIT_CONFIG*` variables, `GIT_PREFIX`, `GIT_NAMESPACE`,
`GIT_CEILING_DIRECTORIES` and `GIT_DISCOVERY_ACROSS_FILESYSTEM`
before starting the selector. The selector repeats this before pytest, and the
pytest conftest clears inherited bindings and disables global/system Git config
before initial nested conftests load, then repeats the guard at session start and
before test fixtures.
Discovery failures refuse to launch tests. The hook's own Git and policy calls
retain the caller's context; temporary test repositories keep their own hooks.
Changes to Rust sources or Python tests select `tests/test_spawner_import_guard.py`
in affected CI runs. The guard identifies bare Python subprocess launches and keeps
pytest modules and test helpers out of Rust-launched Python snippets. Use
`test_python::command()` in Rust tests and `python_test_env()` in Python tests
to select the checkout's interpreter and `src/` path.

`tools/merge_batch.py plan` and `build` fetch origin and measure each PR against
`origin/<base>...<head sha>` with Git, so refresh merges and GitHub's truncated
file lists do not inflate sizes or hide HIGH paths. The `GENERATED` column
reports Cargo.lock, `generated/` trees and minified-file lines separately; these
lines are also included in `LINES` and the batch cap. Both rename paths count
for HIGH checks. For stacks, each PR still displays its diff against the batch
base; the batch cap counts each layer against its parent head once (`batch_lines`
in JSON), preserving per-PR stack contributions.

PR label writes use the REST issue-label endpoint through `gh api`, avoiding
`gh pr edit` and its deprecated Projects (classic) GraphQL fields. The merge
batch helper reports a label as added only when the returned label list contains
its name; missing labels or malformed responses leave `labeled` false and warn.

`tools/merge_batch.py build --name NAME --prs 1,2 --plan plangraph:KEY` records the
batch PLAN key. `--deviations TEXT` records nonconformance, including forced
build exceptions. Record a keeper-bound spawner review for each included
Plan-Item with `review --name NAME --pr N --run-id RUN_ID`. Both `go-req` and
`land` recheck the recorded item key, head, and verdict. Use `--file PATH` to
record a supplementary review with a full `HEAD` and final `VERDICT`.
`push --name NAME --ci-run ID` records the full batch CI run's
head, status and conclusion; repeat push after CI completes. A push without an ID
refreshes an existing run only on the same head; a new head clears it.
`go-req --name NAME` prints one batch GO-REQ with PR, full head, diff stat, part
reviews, CI and the final PLAN block. It refuses absent/stale ACCEPT evidence,
non-green or missing CI, and workflow edits whose touching commits lack a
`Gate-Edit: D-<id>` trailer, including edits introduced by merge resolution (a main-into-lane merge that resolves a workflow conflict carries the trailer in its own message: the commit gate refuses it at push and in CI, and `build` refuses a PR head with such a merge before it builds). The batch key appears in both GO-KEYS and the landing merge trailers; part Plan-Items do not
stand in for the batch's PLAN key. The PR body, GO request, Keeper preparation, and single-PR part count use the parts still active at the pinned train head. `tools/merge_batch.py land` always passes a merge commit `--subject` ending with `(#N)` for the batch PR and refuses a custom `--subject` without `(#N)`. When the keeper schema advances, copy the
standalone `MIGRATION <version>` field into the GO statement for the exact PR
head and pass that row with `land --go-row PATH`. See [keeper migrations](docs/keeper-postgres.md#schema-and-migrations).


After land reports included parts as merged, cleanup
invokes `pg-worktree-sweep --apply --delete-remote --include-merged` with exact
saved SHAs, worktree paths and branches for landed parts. The same pass checks
other idle worktrees under the scanned roots for local removal. Configure
`PG_PINS` and `PG_SWEEP_CLAIMS_READER="PYTHONPATH=/path/to/plangraph/src /path/to/plangraph/.venv/bin/python3 -m plangraph.keeper.cli sweep-claims"`; authority failures
protect host state and cleanup errors never fail land. See [pg-reap](docs/pg-reap.md)
for idle thresholds, run records, restore TSVs and remote SHA leases.

CI runs the full suite once, on the merge candidate: a `batch/*` head, a merge queue
event, or a head a router GO row labels `ci-full` (push to main, dispatch and schedule also run it).
Every other pull request, draft or ready, runs the affected selection
(`tools/ci_select.py`, `full_run_required`); ci-gate refuses a candidate whose selection was not full.
Marking a draft ready starts no run; `merge_batch land` names a pending required check and exits. `land` also asks `plangraph main-state --admit` and refuses with `main_red`, `main_pending`, `state_unknown` or `ref_not_forward_only` while main is not green (`EXCEPTION:main-red-fix` passes only while red; see docs/ci-parallel.md).

Every writer commit fixing keeper items needs one `Plan-Item` trailer per fixed
key, including subitems of a larger item. `ci_commit_gate.py check --base BASE
--head HEAD --item-text PART.txt` warns for uncovered keys; GitHub CI reads the
matching PR event automatically. See [the commit gate](docs/ci-gate.md).
A merge resolution that removes a test needs its own `Test-Removal: D-<id>`
trailer, including when a later commit restores the test.

CI job ids stay fixed; each job's display name and the per-job audit are in [the CI gate page](docs/ci-gate.md).

plangraph is public; the plans it tracks usually are not. Keep that difference clean.

The feeder's `deploy/feeder/house-rules.md` and policy are repository inputs to
Rust tests. The feeder service is also a Rust contract-test input. Changes to
these inputs select the Rust CI job through
`tools/ci_select.py` so template and default-policy checks cannot be skipped.

## Setup and tests

New devstack keeper configs require `PLANGRAPH_DECISIONS_REPO` naming a decisions
checkout; existing valid configs are reused when it is unset. Process waits in
devstack and orchestrator scripts use `plangraph wait-pid` (exact PID or PID file,
never `pgrep -f` pattern scans). Explicit setup
adds missing `repos.decisions` at `origin/main` privately and preserves all existing values.
See the [keeper install config](docs/RUNBOOK.md#1-init-and-config).

Python 3.11+ (CI runs 3.12). `tests/conftest.py` puts `src/` on `sys.path`, so there is no
install step; the keeper tests need Linux, macOS or WSL, the rest run anywhere.
The MCP snapshot guard has a path-specific `tools/new_code_allowlist.txt` entry
with `(D-5062), retired by MCP-RUST`; keep lane exceptions scoped to that lane's files.
The D-5504/D-5506 `mcp_tools.py` exception only splits schema data out of the full Python MCP transport; it also retires with MCP-RUST.
Keep both OOM diagnostic regression and quota snapshot module/test entries in `tools/new_code_allowlist.txt` when merging these concurrent in-flight changes.

```bash
python -m pip install -r requirements-ci.txt  # dev deps, including pytest-xdist and jsonschema>=4
python tools/affected_tests.py --base origin/main --run -n auto  # writers / PR / L2
python -m pytest -q -n auto tests            # main merge + nightly full suite
```

`cargo test --manifest-path rust/Cargo.toml -p pg-spawnd --test benchmark_rows`
checks the catalog schema and strict route admission. Both the schema and the
legacy provenance fixture select Rust CI checks.

The selector follows static Python module launches (`-m`) and calls through
`tools/*.sh`, including imports in shell Python heredocs. Deleted Python modules
use the base revision’s importer graph; unresolved importers select the full suite,
including deleted leaf tests. Deleted `conftest.py` files select the full suite for
implicit fixture consumers. Local preflight refuses these full-suite selections.
The affected selector falls back to the full suite for shared fixtures and suite
configuration; unmapped code selects a bounded smoke set (tests naming its directory,
at most 12, plus `tests/test_affected_tests.py`), and CI/main keep the full suite; documentation, `changelog.d/` fragments,
build artifacts, and `.md`/`.rst`/`.txt` files no test names, select no tests. Under `docs/` and `status/` only documentation files (`.md`, `.rst`, images) select no tests; any other file there (`docs/conf.py`, `status/x.toml`) goes through the normal mapping. Do not bypass a conservative fallback on PRs.
Pass `--head <revision>` for a committed-only comparison, excluding staged,
worktree and untracked changes; omit it to include checkout changes. The selector puts the tested checkout's `src/` before inherited `PYTHONPATH` entries for pytest.
Each affected pytest invocation gets a separate basetemp under the selector's
private run directory. The selector removes that directory on exit. On Linux,
disk hygiene can reclaim an abandoned run directory after its lease is idle.
Reproduce speed measurements with `bash tools/measure-test-speed.sh both HEAD`;
the fixed runner.py sample does not modify files, and the full run uses a 6 GiB
systemd user scope. Use `affected` or `full` to measure each separately.

Rust review record assertions run on every platform; keeper-backed assertions run only on Unix because the keeper imports POSIX `fcntl`.
Rust review tests also execute keeper evidence and planner routing code; changes to `src/plangraph/keeper/errors.py`, `src/plangraph/planner/routing.py`, `src/plangraph/planner/measured_down.py`, `src/plangraph/planner/provider_stop.py` and `src/plangraph/routing_support/liveness.py` select Rust CI through `RUST_INPUTS`. `rust/pg-spawnd/tests/witness_job_identity.rs` reads `src/plangraph/config/prompt_transport_v1.json`, so that contract file is a `RUST_INPUTS` entry as well. `rust/plangraph-cli/src/runner/config.rs` tests parse `docs/operations/ci-runners.example.toml`, which is a `RUST_INPUTS` entry too. `rust/pg-spawnd/tests/ci_step_timeout.rs` reads `.github/actions/bounded-step/action.yml`, so that action file is a `RUST_INPUTS` entry as well. `rust/pg-spawnd/src/role_header.rs` embeds `src/plangraph/config/role_headers.json`, so that header table is a `RUST_INPUTS` entry too. `rust/pg-spawnd/src/round_progress_tests.rs` reads the round setting in `src/plangraph/settings.py`, so that file is a `RUST_INPUTS` entry as well.

CI suite selection in `tools/ci_select.py` treats `docs/schemas/**` and its
`RUST_INPUTS` list as Rust inputs even when they are documentation. These inputs
select both `rust` and `rust-windows` via the shared `rust` output without changing
other suites. `tests/test_ci_select_rust_inputs.py` scans `rust/` for include
literals, manifest-relative reads and paths leaving `rust/`; new repository
inputs belong in `RUST_INPUTS`. For deployment tests, register each systemd unit
and environment template they read. Add an explicit test case in
`tests/test_ci_select_rust_inputs.py` for inputs the scan misses, such as unit
filenames joined to a deployment directory by a helper. `rust/pg-spawnd/` and
`rust/Cargo.*` also select `rust-worker`; its helper and tests share the cached target.
Python files executed by Rust tests (for example `src/plangraph/catalog_data.py` and `src/plangraph/routing_errors.py` run by `rust/pg-spawnd/tests/mimo_output_cap.rs`) are listed in `ci_select.RUST_INPUTS` so changes to them select the Rust suite.
The router liveness installation test selects Rust CI when its deployed config or watchdog changes.
`skills/router-core/SKILL.md` selects Rust CI because the batch CLI tests check its command examples; `tests/fixtures/replan_slice_definition.json` does because the spawner's replan test asserts it.
`ci_select.RUST_WORKER_INPUTS` lists inputs that also select the standalone
Temporal worker job, including `tools/probe_admission_policy_guard.py`.
Include paths passed through read helpers too: `pg-hook` reads `.docs-map`,
`docs/rust-hooks.md`, `tools/new_code_allowlist.txt` and its parity corpus at
`tests/hooks/parity_payloads.json`; all four are explicit selector inputs.
`setup/manifest.toml` and `docs/setup.md` select the Rust job because the CLI setup contract test (`rust/plangraph-cli/tests/setup_contract.rs`) reads them.

Run `cargo test -p <crate>` from `rust/` for each affected crate. Documentation
mapping for a crate covers its own paths, not shared workspace manifests or locks.

### Rust builds and toolchain bumps

The repository-root `rust-toolchain.toml` is the single Rust release pin shared
with all CI jobs through `.github/actions/setup-rust`, including Windows.
Rustup selects it from both the repository root and `rust/`, so root-level
`cargo build --manifest-path rust/Cargo.toml` uses the same compiler as:

```bash
cd rust
cargo build -p pg-spawnd --release
cargo fmt --check
cargo clippy --all-targets -- -D warnings
```

Toolchain-only changes select all CI suites, the Python pin guards, and local Rust
preflight checks. Dell installs this pin before mounting its direct sysroot.

**Bump step:** open one PR that changes the channel in `rust-toolchain.toml`
and fixes the new lints, without weakening `-D warnings`. Run every Rust CI job
(`rust`, `rust-windows`, `rust-worker`, `rust-spike`) and require green before
merging. Do not duplicate the version in workflows. The regression check is
`python3 -m pytest -q -n 2 tests/test_ci_rust_toolchain.py`.
The Rust integration test `toolchain_pin` checks `rustc --version` from the root
and `rust/` against that channel, without inherited `RUSTUP_TOOLCHAIN` overrides.
The Python guard scans every workflow and local composite action, rejecting case-insensitive
`RUSTUP_TOOLCHAIN` overrides (including container/service environments),
`cargo +<toolchain>`, `rustup run <toolchain>` and direct rustup installation/selection.
Only the shared setup may use actions whose names contain `rust` or `toolchain`.
Its upstream action is commit-pinned with an action-version comment; update that
SHA explicitly in a reviewed PR when needed, not the shared Rust release pin.

## Lanes

Completed Rust ports remove only their own Python waiver from
`tools/new_code_allowlist.txt`; preserve unrelated exceptions when merging main.
Merge in-flight test entries additively so each active lane retains its own coverage.
SPAWNER-RUST-PORT slice 2 also removes the stale doctor-source waiver retired by
slice 1; its changelog fragment accounts for all four named Python waivers.
Doctor source-filter goldens live in `tests/fixtures/doctor_sources.json` and run
through both Rust unit tests and `tests/test_ps_host_sources.py`; update both
consumers rather than matching Python source text or spawning Python from Rust.

One branch per lane (`lane/<short-name>`, cut from `main`), one small PR into `main`.
The RUN-WORKTREE Python regression exception (D-5062) retires with SPAWNER-RUST-PORT.
Work that covers two plan items is two lanes.
The spawner pins the item, epoch, review round, head, and run at seat admission. `tools/seat_verdict.py` persists the decision before delivery. Repost and backfill reuse that decision and recover its keeper receipt if the first reply was lost.
Record-derived verdicts and backfill allow an operator-ordered override only when
`override_reason` starts with `D-<n> operator` and that exact id appears once in
`router/decisions-index.tsv` in the data repository named by `PG_DATA_DIR`.
Unset or empty `PG_DATA_DIR` refuses with `PG_DATA_DIR_UNSET`; no path is guessed.
The TSV's first two columns are decision id and source (`OPERATOR`, case insensitive);
headers and extra columns are allowed. Missing or unreadable authority refuses the
exception. Explicit pin flags still refuse, and the served model must be witnessed
and the completion HEAD bound (D-5874). Rust regressions:
`cargo test --manifest-path rust/Cargo.toml -p pg-policy --test seat_operator_order`.
Seat records bind manual `tools/seat_verdict.py --key` posting to their recorded `item_key`; a different key or an explicit null is refused before token reads or network I/O. Historical records without a proven admission binding can be inspected and are surfaced for routing recovery before delivery. A review record with a checkout `head_sha` can use the head-bound record path even when response artifacts are present. Card/environment identity validation never infers a target from prompt text. Record-derived review text uses a completed OpenCode step or Claude result from `live.log` and also accepts plain-text logs. The verifier refuses fabricated tool-call markup in replies from tool-less seats with `toolless_tool_markup`. The final `VERDICT` line follows the ending rules in F8 (D-3420). Lifecycle events decide event mode: at least one JSON object with `type` equal to `step_start` or `step_finish` and an object-valued `part` is required; `text`/`tool_use` lines, quoted JSON and other typed objects alone leave a plain-text log unchanged. `codex` names the openai family in both family tables (`tools/seat_verdict.py` and `src/plangraph/review/family.py`, kept equal by a test).
The `evidence.json` path applies the same refusal to every supplied `item_key` or `key`, including explicit nulls and conflicting aliases; evidence without either identity field retains the legacy manual path. Reviewer tokens belong to the seat's model family, not the `--writer-family` being reviewed.
When evidence and a record coexist, the record's `item_key` remains binding even if evidence omits or replaces it; an unreadable or malformed companion record is refused. The record identity check also runs independently of evidence derivation on the record-only path. `--key` is required for all manual invocations, including legacy records; omitting it exits 2 before seat/token reads or network I/O.

Record-derived finish evidence uses a nonempty string from `record.finish_reason`
first, then `completion.finish_reason`, then the last `opencode_step_finish` in
`events[]`. Only recorded values count: an absent or unusable reason on that last
finish event stays unset, never falling back to an earlier event. Malformed event
entries and non-list event collections do not raise. PASS still requires `stop`
or `end_turn` and `TEST_GAMING: no`; the hermetic posting regressions use admitted seat records in
`tests/test_seat_record_finish.py` (D-5624).

For full-diff CLI seats, dispatch records `review_binding` with `base_sha`,
`head_sha`, and `diff_sha256`. The digest hashes the sorted raw Git diff
records, including paths, file modes, and object IDs. `seat_record.py` checks
that the completion repeats the dispatch binding and names the same head.
Discovery callers can supply an expected `review_binding` to require an exact
match. Records created before this field was added still support head-based
lookups.

Historical seat discovery uses `python3 tools/seat_verdict.py --snapshot plan.snapshot.json --records-dir RECORDS` (equivalently, `--backfill-dry-run`). It is local and read-only by default; `--format json` emits structured outcomes, and `--pr-cache CACHE` supplies evidenced-commit-to-PR mappings without network access. Keeper writes require `--backfill-apply --url URL --token-dir TOKENS`; only apply may enable network PR lookup with `--pr-lookup-gh`. Backfill retains the existing verdict parser, exact-head binding, independent reviewer family and PASS safety gates. It shares the spawner's locked `verdict.json` / `verdict.delivered` outbox, so completed or ambiguous sends are never blindly replayed, including when the forward path already handled the record. A nonzero verifier result is terminal for automatic backfill replay; reconcile an ambiguous delivery before any operator-authorized retry. See [the historical backfill diagnosis](docs/seat-verdict-backfill-diagnosis.md) for the original observations; Slice A has since supplied the forward completion bridge.

Run the documented `tools/seat_verdict.py --repost-transport` workflow directly
from a checkout. The tool loads its imports from `src/` when needed.

Batch orchestration and guarded outbox delivery live in `tools/seat_backfill.py`,
with compatibility re-exports at `seat_record.backfill` and
`seat_verdict.apply_backfill`. Record parsing/discovery remain in `seat_record`;
`seat_verdict.main()` remains the only keeper poster. The helper's path-specific
Python exception covers this responsibility-only split of existing code.
Explicit transport-only retries require `--repost-transport --url URL` without `--dry-run`; see the [backfill diagnosis](docs/seat-verdict-backfill-diagnosis.md).

Backfill rejects pinned or explicitly overridden seats, incomplete/nonzero-exit records, writer-family seats, and records without a strict verdict. Family independence uses the same witnessed evidence model as the poster. Any eligible CHANGES takes precedence over ACCEPT in every discovery order; replay never falls through from a terminal or ambiguous CHANGES to another accepting seat. Apply JSON separates eligibility from `delivery` (`delivered`, `failed`, or `skipped`) and includes `verifier_exit` after a send. Corrupt journals and busy shared outbox locks skip with reasons without blocking other items. PR subprocesses have a 30-second timeout; lock acquisition is nonblocking. Exact completion-head equality remains required; backfill does not permit a different head via card ancestry.

Discovery and apply JSON always include `delivery` and `verifier_exit`, initially `null`; item reasons are collected only after a record binds by item key, named prompt item, or matching evidenced PR. An item with no bound review reports `no record`, including its missing-seat requirements. Writer attribution unions Git trailers and matching implementation records; an unknown writer witness refuses independence. Record traversal uses `os.walk(followlinks=False)`: symlinked directories are pruned and reported separately on stderr, never attributed as another item's rejection reason. Presence of `pin`, `pinned`, or `spawn_pin` refuses a seat unless the value is explicitly boolean `false`. The library entry points enforce explicit apply and reject contradictory dry-run flags too. Per-seat lock-open, lock, journal-read/write, and unexpected poster errors report `delivery=failed` with a phase and exception type while allowing other items to finish; once written, a `sending` journal remains ambiguous after a crash or invalid verifier result and is never blindly replayed.

Missing configured repository paths and failed or empty Git trailer lookups refuse as `unknown writer`, never partial implementation-record coverage. PR/writer resolution errors remain reasoned per-item outcomes. Missing apply URL or token directory reports `delivery=failed` before touching the shared forward journal or lock. Library callers default absent/null `records_dir` to `~/.local/share/plangraph/spawner-records` and absent `url` to `PG_URL`, matching the CLI. Explicit backfill modes reject `--key`, `--seat-dir`, `--head`, and `--writer-family`; prompt contradiction checks recognize only known snapshot item keys rather than metadata such as `demo:GPT-4` or `demo:PASS`, while exact named-item prompt binding is preserved.

New production code must be Rust unless explicitly listed in `tools/new_code_allowlist.txt`; the host-readiness source-probe helper is a single-file exception for the pre-gate lane, retired by `SPAWNER-RUST-PORT`. `src/plangraph/spawner/redact_tokens.py` is a second one: the Python scrubber must mirror the Rust token catalog until that port retires it (D-5279).

For an offline judge brief, run `python3 tools/judge_prompt.py --findings findings.txt
--base origin/main --head HEAD --ci-summary ci.json --output judge.md`, with
`ci.json` mapping job names to conclusion strings. The prompt includes complete
findings and diff; `--byte-budget` limits whole file bodies and lists explicit cuts.
See [Lean review harness](docs/lean-review-harness.md) for seat tool modes.
Only `--tool-mode tools` advertises follow-up reads; `inline-only` reads the
spawner-inlined diff without tools, and `prompt-only` uses supplied evidence.
Gateway prompt-only config is file-backed via a portable launcher, never argv.
New Python files require a reasoned rule 9.14 entry in `tools/new_code_allowlist.txt`.

## File size

A tracked source, test, script or doc file stays at or under 800 lines; over 800, split it
by responsibility before adding more. `python3 tools/check_file_size.py` enforces this in CI
and in the pre-push hook. Files that were larger when the gate landed are listed in
`.filesize-baseline`: such a file may shrink, never grow, and the baseline itself only
shrinks: `--update-baseline` writes it once when none exists, afterwards only lowers counts or removes entries that fit the 800-line cap, and CI refuses an entry added or raised against the change's base (`origin/main`
on a PR, the pushed-over commit on a push to main). Data and vendored
files are not counted; a deliberate exemption goes in `.filesize-allow` with a reason
(today only `CHANGELOG.md`, an append-only ledger). A doc with a lower cap is named in
`DOC_CAPS` of `tools/check_file_size.py` (today `docs/SPAWNER.md`, 728 lines); the check refuses it
over its cap, naming the file and the cap, and main keeps `DOC_HEADROOM` (60) lines under it: move a
self-contained section into `docs/operations/` and link it.
The file-size gate refuses malformed Git batch headers with a single reason line.
Pre-push uses `--head` for each pushed ref, reading its blobs, `.filesize-allow`
and `.filesize-baseline` from Git. Public scrub rules also come from that head.
Preflight runs in a temporary detached worktree of the pushed head and removes
it after success or failure; another checked-out branch or dirty files cannot
change the policy inputs or tests. `--head` is read-only and cannot be combined
with `--update-baseline`.

New Python source outside tests needs a `path | reason (D-id)` line in
`tools/new_code_allowlist.txt`, which `python3 tools/check_new_code_lang.py` reads; for
example the keeper's host-sample store stays Python because the keeper is Python (D-5038).
The `spawner/gateway_models.py` entry covers a responsibility-only extraction of existing Python model admission and row parsing; `gateway_witness.py` keeps compatible re-exports.

## AutoOS references

`python3 tools/autoos_refs_check.py --only opencode` scans the OpenCode config root alone for AutoOS paths in `instructions` entries and skill links.

## Changelog and documentation

Every PR records its changelog only in `changelog.d/<ITEM-KEY>.md` (several parts of one item: `<ITEM-KEY>-<n>.md`). Add a non-heading entry line against the merge-base; fragments must be direct children and README.md is excluded. Do not edit `CHANGELOG.md` in an ordinary PR, including doc-only edits, deletions or edits later reverted in the range. The gate checks each commit; merges may inherit parent ledger edits, but must preserve additions and cannot invent ledger lines or discard common base lines (unless a parent already deleted them) without a release trailer. A release commit is recognized only by a nonempty `Release: <version or release description>` trailer in its trailing trailer block (case-insensitive); body prose and empty trailers do not qualify. Release maintainers run `python3 tools/changelog_fold.py`, then commit the folded changelog and fragment deletions with that trailer. `--check` writes nothing and exits 1 while fragments remain; empty and heading-only fragments remain with a warning. A release trailer permits the CHANGELOG edit but does not waive the fragment requirement for code changes. Non-doc changes may instead carry `Changelog: skip (<reason>)` on every non-doc commit, with a nonempty reason for typos or format-only edits; merge commits do not count. Neither skip trailers nor fragments excuse a non-release CHANGELOG edit. The changelog/docs checker imports `ci_commit_gate._trailer_block_lines`: the block is the maximal trailing run of paragraphs made entirely of trailer-shaped lines. Attribution paragraphs such as `Co-Authored-By` and `Claude-Session` may follow policy trailers across blank lines; prose after a skip ends its eligibility. Furthermore, repository documentation mappings in `.docs-map` pair path globs with their corresponding doc files; changes touching a mapped path must update at least one of its listed docs. Globs: `*` stays inside one path segment, `**` spans segments; the `# enforce-after:` date is the first day the mapping is an error (earlier days warn). `python3 tools/check_changelog_docs.py` (range `merge-base(base, --head)..head`) verifies this in CI and runs with `--warn-only` on each pushed sha in the pre-push hook.

`tests/fixtures/writer_docs_map.json` is the shared glob/parser case table for Rust writer finalization and `test_writer_docs_map_shared_parity` in `tests/test_check_changelog_docs.py`; run the latter with `python3 -m pytest -q -n 2 tests/test_check_changelog_docs.py`. Rust reads the head map; CI additionally retains merge-base rules (see [writer finalization](docs/SPAWNER.md#writer-result-finalization)).

Shared `rust/Cargo.toml` and `rust/Cargo.lock` stay unmapped unless an existing workspace-wide doc applies; their mappings follow the head revision so retired crate-specific rules do not block other crates. Other paths retain merge-base and head enforcement.

## Commits

Every commit message ends with one trailer paragraph — no blank lines inside it:

```
Plan-Item: plangraph:<KEY>
Co-Authored-By: <author> <noreply@example.com>
```

A merged commit whose trailers fail the CI commit gate is never rewritten. A router decision adds its full sha to
`tools/ci_commit_exempt.txt` in a commit that carries `Gate-Edit: D-<id>` in the same trailer paragraph. The gate reads
the list from the base commit, so the row has to merge to `main` before the pull request that holds the commit can pass.
A row for a test-removal failure is added only after the branch asserts the removed behaviour again; the comment line
above the row names the decision, the pull request and that test.
Each row is one comment line (`# D-<id>: #<pr> <reason>`) followed by the full 40-hex sha on its own line.

## No secrets in the history

This repo is public: never commit a password, token, hostname, IP address, e-mail address
or home path — not in a diff, not in a commit message. Keep
`python -m pytest -q tests/test_split_make_public.py` green: it is the guard that keeps the
public split public, and it fails on exactly those leaks. CI runs on GitHub-hosted runners
only.

Fast checks must run `python3 tools/preflight.py --base origin/main --head HEAD --fast`
on the exact committed head. Public rules from `manifest.toml` and `scrub_ci.toml`,
credential shapes and the configured private words run together (the ipv6 loopback address and the RFC 3849 documentation range are allowed, like the ipv4 test ranges). The legacy
`tools/preflight_private_scrub.py --base origin/main --head HEAD --words FILE`
entry point also runs all these rules. Hook, CI and fast checks share
`scrub_check.scan_content`; committed checks exclude inherited line occurrences.
The SSH tests' loopback and documentation-range IPv6 values are scoped by file,
rule, and SHA-256 in `scrub_ci.toml`.

Install the pre-push scrub gate (scans the files changed in the pushed commits with
`tools/split/scrub_check.py`, refuses on any hit, prints only `file:line: rule`):

```bash
git config core.hooksPath tools/hooks        # per clone; undo: git config --unset core.hooksPath
```
Or set `PLANGRAPH_PUBLIC_REMOTES` to comma-separated remote substrings (empty items ignored) and run `python3 tools/check_push_gate.py --install PATH`; audit with `python3 tools/check_push_gate.py --root DIR` (unset or empty: all remotes are public; installation refuses without writing).

The private word list is never stored in the repo: point the hook at it via environment,
`PLANGRAPH_SCRUB_WORDS=<path to words.txt>` (optional extra allowances: `PLANGRAPH_SCRUB_RULES=<path to rules.toml>`).
Unset, the hook uses `~/.config/plangraph/scrub-words.txt` (requires `chmod 600`).
A missing default warns once that the private-word rule is inactive; public patterns still run.
An explicit missing or unreadable list refuses pushes to any remote. The env list wins.
Remotes are public unless `git config remote.<name>.plangraph-private true` is set:
that skips private-word matching, not public patterns. Classification uses local config only.
Diagnostics never show matched values or the words-file path.

For an existing branch refreshed by merging known `origin/main` history, the hook
selects affected tests from the merge-base with `origin/main` when that base
descends from the remote branch head. Public scrub still covers the original
pushed range, passed separately to preflight as `--scrub-base`.

For a merge batch, the hook fetches and prunes origin branch refs before selecting
tests. When the range starts in known `origin/main` history and every second
parent is reachable from `refs/remotes/origin/*`, local
affected tests cover each merge's `git show --remerge-diff` (conflict resolutions
and evil-merge edits), files changed by at least two parts, and every non-merge
commit on the batch's first-parent chain. Exact commit ancestry is required;
unpushed or locally rewritten heads, unavailable fetch/history, octopus merges,
and ranges without a first-parent base fall back to normal affected selection.
Existing feature or partially published batch tips outside main history also
retain normal selection, preserving interactions with their earlier commits.
The selection log names the mode and counts parts, delta files, and selected tests.
This changes only test selection: scrub, policy and Rust steps still run, and
batch CI runs the full suite (rule 3.42 a).

## Shadow merge decision (D-5252, slice 1)

`python3 tools/merge_gate.py facts.json` prints `GO <sha>` (exit 0) or
`REFUSE <code> <detail>` (exit 1); it only reads that file. No collection,
network, merge, GO-row write, verdict post or item closure is implemented.
`merge_gate_core.decide(facts)` is pure and returns one immutable decision.
Facts: `head_sha_at_check`, `head_sha_now` (full lowercase SHA), `required_checks`
(nonempty names), `ci_checks` (complete latest-per-name records with `name`,
`head_sha`, `status`, `conclusion`), `seats` (`family`, raw `verdict` text,
boolean `pinned`, `open_findings` severity strings), `writer_families`,
`changed_paths`, `tier`, `plan_key`, `item_key`, boolean `preflight_present`,
and `main_changed_production_paths` (complete main delta since CI base).
Path lists include deleted paths and both rename sides. Missing facts refuse;
only completed success checks pass (unrequired suites may be skipped).
The trusted future collector owns
completeness, freshness and evidence provenance; this CLI grants no authority.
First refusal wins: head, CI red/pending, seat absent/pinned/family/findings/
acceptance, gate path, tier, PLAN key, preflight, stale green (rule 3.19).
Python imports the existing strict seat parser/coverage and trailer door table;
additional conservative path categories cover docs-map, deployment, allowlists,
redact/auth and gate/check/scrub tools without copying the door list.

The facts object and every CI/seat record require exactly the keys above;
missing, renamed, non-string or extra keys refuse (`ci_pending` for facts/CI,
`seat_missing` for seats). Top-level schema validation precedes the semantic
refusal order; CI/seat record schemas are validated at their respective stages.
Wrong value types, UNKNOWN values and duplicate evidence never yield GO.
Required/check names, writer/seat families, severity lists and normalized path
lists must be unique; every supplied seat must independently parse as accepting.
Malformed CI values refuse `ci_pending`; malformed writer families refuse
`seat_family_is_writer`; malformed seat pinning/findings/verdicts refuse
`seat_pinned`/`finding_open`/`seat_missing`, respectively. Duplicate seat families
refuse `seat_missing`, duplicate changed paths refuse `gate_path`, and duplicate
main-delta paths refuse `stale_green`. LOW-only findings remain nonblocking.
Item/PLAN keys must match `[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?`, with no UNKNOWN
component; invalid keys refuse `plan_key_mismatch`. String-list entries must be
nonempty, printable and have no surrounding whitespace. Paths use the same
case/separator/dot normalization for both evidence and imported door patterns.
Both door tables must be nonempty tuple/list collections of valid path patterns;
the authority's existing root globs `roles*` and `scopes*` are intentional
slash-less exceptions. Missing/malformed/duplicate table entries refuse
`gate_path`, including when a table is accidentally replaced by a plain string.
Duplicate JSON object keys at any depth and undecodable or excessively nested
JSON refuse `ci_pending` without a traceback. Decision tests block file,
environment and clock access inside the real parser/coverage dependency calls,
in addition to subprocess, network and verdict-post effects.

### Shadow merge decision slice 2: fail-closed boundaries

`required_checks` must include `ci-gate`, `select`, the live `ci_select.ALWAYS`
table and every suite selected by the existing pure `ci_select.select()` policy
for `changed_paths`. The selector sees both literal and normalized paths so
case normalization cannot silently remove a required suite. Empty, trivial or
incomplete self-declared lists refuse `ci_pending`; each required check must
complete successfully on the exact head. Every supplied check still participates
in red/pending detection, even if it is not required. Invalid selector tables or
results refuse `ci_pending`; the policy is imported, not copied into the gate.

Slice 2 limit: this is a local repository-policy floor, not live GitHub branch
protection/ruleset discovery, workflow parsing or authenticated evidence
collection. Additional workflow jobs (including browser and platform jobs), live
required contexts, workflow/base provenance and completeness/freshness must be
established by the trusted future collector and included in `required_checks`.
No count or self-declared list proves those external facts. This shadow CLI
remains non-authoritative and must not be wired directly to unattended merges.

Both the public core boundary and the CLI catch unexpected validation/dependency
exceptions and return `REFUSE ci_pending facts unavailable or invalid`, without
exception text or a traceback. Malformed parser/coverage return values refuse
`seat_missing`. The CLI validates the decision's outcome, defined reason code,
single-line detail and exact GO head before printing exactly one result line.
The defined refusal codes remain `head_moved`, `ci_red`, `ci_pending`,
`seat_missing`, `seat_pinned`, `seat_family_is_writer`, `finding_open`,
`gate_path`, `tier_high`, `plan_key_mismatch`, `preflight_missing`, `stale_green`.

Evidence paths are literal: `*`, `?` and `[` refuse `gate_path` in PR paths or
`stale_green` in main-delta paths. Only imported door patterns may use globs.
Shared fixtures are registered in `tests/conftest.py`, with bounded support code
in `tests/_merge_gate_support.py`; neither test module imports the other. The
guards block subprocess entry points, `os.system`/`os.popen`, socket construction
and connection/server helpers, `urllib.request.urlopen` and verdict posting.
Real dependency calls retain the scoped file/environment/clock guards. Tests
isolate each live door table and verify the refusal disappears without it.

## Turn-budget hook development

The Bash guard checks explicit model arguments to Codex and OpenCode against the shared retired-model policy.

The Python `tools/hooks/context_guard.py` carries the AutoOS compaction scan in
`context_compaction.py` and `ready <handoff-path> [--reason cap|router|operator|planned]`.
Ready requires a sibling `<stem>.card.md` validated by PATH's `handoff-check`
(or executable `AUTOOS_HANDOFF_CHECK`; waits at most `HANDOFF_CHECK_TIMEOUT_S`, 60 s, and names a
timeout), records v2 hashes, and best-effort kicks
the watchdog unless `AUTOOS_READY_KICK=0`. Once `<state_dir>/<session_id>.ready.json`
exists as a v2 marker for the calling `session_id`, `context_guard.py` (and
`ctx_guard_session.py`) registered on `PreToolUse` (matcher for all tools, `"*"`)
denies further tool calls—including subagent calls carrying `agent_id`—with
`hookSpecificOutput` (`permissionDecision: "deny"`, exit 0) naming the ready
timestamp and handoff path. Only a single Bash invocation with no env-assignment
prefixes, plain `python3`/`python` (or `/usr/bin/python3` / resolved `sys.executable`),
a script path resolving via `realpath` to the guard itself or
`$HOME/.local/share/plangraph/hooks/context_guard.py`, and the exact shape
`ready <path> [--reason <cap|router|operator|planned>]` or `status` is exempted so
a refused `ready` can be retried; missing, foreign-session, and corrupt markers allow
(fail open). When a v2 ready marker exists (for the session identified by a bounded
JSON prefix parse or `CLAUDE_CODE_SESSION_ID`, or any v2 marker in `<state_dir>` if
unidentified), oversized or unparsable stdin payloads fail closed with the same
PreToolUse deny output unless the prefix names a non-`PreToolUse` event; known
non-`PreToolUse` events keep failing open. In `ctx_guard_session.py`, `{"off": true}` (and attended leases) only
skip `PostToolUse` threshold guidance and do not disable `PreToolUse` denial. Tests
isolate state and mock the kick. The HANDOFF-NOW and HARD-CAP lines carry the absolute
command `python3 <guard path> ready <handoff>` (`ready_command`); `ctx_guard_session.py`
has no ready step and exits 2 naming that command when called with `ready` or `status`.
`tools/hooks/install_hooks.sh` copies the Python hooks by default. The installer
leaves harness settings to the operator. Register the
guarded missing-file wrapper on both `PreToolUse` (with an all-tools matcher) and
`PostToolUse` (using either `context_guard.py` or `ctx_guard_session.py`, not both):

```json
"PreToolUse": [
  {
    "matcher": "*",
    "hooks": [
      {
        "type": "command",
        "command": "sh -c 'test -f \"$1\" || exit 1; exec python3 \"$1\"' _ $HOME/.local/share/plangraph/hooks/context_guard.py"
      }
    ]
  }
]
```
`AUTOOS_CTX_*` and `autoos/ctx-guard` stay compatible; plangraph keeps calm,
path-free guidance and its safer parsing/subagent handling rather than push advice.
The scan cursor lives in sibling state directory `autoos/ctx-guard-scan`; rewrites
before the cursor are not detected (transcripts are append-only), and records over
1 MiB are skipped.

The standalone Rust `turn-budget-hook` lives in `rust/worker-core`, beside the
existing watchdog binaries. The default `tools/hooks/install_hooks.sh` mode
copies Python hooks; its `--git` mode installs the Rust Git hook.
Use `--session-rust` to install the session guard binary and include it in
`SHA256SUMS`. Register the Stop and PreToolUse commands separately using
`tools/hooks/install_hooks.sh --help`. The events and inputs are documented in
[Rust session guards](docs/rust-hooks.md#session-guards).
Build, registration examples, injectable state paths and the polling-recognition
boundary are documented in [Turn-budget hook](docs/turn-budget-hook.md).
Run its hermetic contracts with `cargo test --manifest-path rust/Cargo.toml -p
pg-worker-core --test turn_budget_hook --test turn_budget_poll --test
turn_budget_alert --test turn_budget_watchdog`. Tests never install hooks, read
live session state, or change permissions/settings files.

Python files already in flight are individually listed in
`tools/new_code_allowlist.txt` with reason `D-5847 in-flight`.

Local preflight tests compare the branch against origin/main, follow shared asset consumers, and refuse full-suite fallback. CI retains the full-suite selection path.

Local affected selection always includes the subprocess UTF-8 AST check and the
file-size, new-code language, and changelog/docs policy tests, even for docs-only
changes. Explicit `--within` restrictions still apply. CI-FAST-PATH fail fast
applies to stages, not sibling pytest shards: both pytest matrices finish all
shards, and any failing shard still fails `ci-gate`.

The standalone fresh-base admission carry is listed as D-5848 in
`tools/new_code_allowlist.txt` until the spawner Rust port.

Local selection includes named consumers and bounds the file list at 39. CI covers the wider set. Policy checks use origin/main when available, including inherited stack changes. Preflight clears the complete GIT_CONFIG namespace before selector launch.

Both pytest CI jobs validate collection once without `PYTEST_SHARD` on shard 0, using the PR
affected selection or the full push/nightly suite. Empty individual shards remain
valid; a nonempty file selection that collects no tests fails the job. Local
preflight timeouts terminate the selector and its pytest descendants. Missing
`tools/preflight.py` refuses pushes to main.
Topic and tag pushes without preflight still run the scrub, ref and file-size gates.

The K1-R1 HEAD audit runtime is explicitly stdlib Python so the installer can copy
it outside the changing checkout; its narrow allowlist entry covers that requirement.

The generated example batch declares tier `none` for new decision-only items; the example builder preserves historical imported plan definitions and their stored tiers.

Gemini retirement regressions preserve historical record reads while checking request
admission and catalog filtering; AGY capability/witness activation requires evidence.

The 800-line tracked file budget is authorized by D-5840 (95a9ac34); legacy baseline ratchets still apply.

[Per-item checkpoint and status notes](docs/checkpoints/). Add new notes there, one file per item, rather than appending here.

Standalone Rust workspaces, including rust/worker, receive manifest-specific preflight checks.
Preflight selects changed crates and their transitive path dependents across workspaces;
the Temporal spike keeps its CI replay-only check and is skipped for unrelated workspace changes.

Worker preflight tests default to the CI test-only unjailed environment when no jail
configuration is supplied; explicit jail settings are preserved and other check commands are unaffected.

Rust test builds, including nested admission checks and Python placement fixtures, inherit `CARGO_TARGET_DIR`; keep the host shared target when it is configured.

Local worker preflight runs library and binary unit tests offline. CI runs the full worker suite, including Temporal-dependent integrations, through `ci-temporal`.

Install Git message validation with `tools/hooks/install_hooks.sh --git`
(using the shared host `CARGO_TARGET_DIR` and [compiler cache](docs/host-tools.md)). Non-merge commits require a strict
`Writer:` or `Hand-Edit:` trailer. See `docs/rust-hooks.md`.

Writer handoffs named `STATUS-*.txt` are ignored at every depth. Keep them
outside the checkout; the changelog/docs PR gate also rejects added status
notes, including forced additions and renames into that naming pattern.
The status-note check also inspects additions in every non-merge commit, so
adding and then deleting a note within the PR still fails.

The new-code language gate is implemented in Rust (`rust/pg-policy`), with the
existing Python command retained as a launcher. Added allowlist entries need
base-revision OPERATOR authority; D-5840 and D-5830 do not count. See
[the migration plan](docs/PY-TO-RUST-MIGRATION-PLAN.md) for the index and first ports.

The pre-push hook checks commit trailers on `origin/main..head` before scrub and
preflight. It queries open PR heads and checks
foreign merges when gh/network access is available; offline pushes still run the
local trailer gate and explicitly report the skipped open-PR check.
For a landing train re-push, keep the folded parts' head SHAs in the train PR's
part table. `merge_batch push` uses the batch's saved part heads for the first
push, before the batch PR exists. See [the commit gate](docs/ci-gate.md).

Review prompt builders use `src/plangraph/review/review_brief.md` through
`plangraph.review.prompt.render_brief`. Supply item context explicitly (the judge
CLI accepts `--context` JSON); missing fields remain visible as unavailable.
Fixed brief rules and output contracts precede `=== ITEM CONTEXT`; item-specific
content follows it so different items reuse the same prompt cache prefix.

Append-only ledger paths are declared once using `merge=union` in the root `.gitattributes` (currently the historical `CHANGELOG.md` ledger, retained for historical merges and releases; current PRs still need fragments). `tools/merge_batch.py build` reads these defaults from the fetched base and persists them for continue/recovery; `--union GLOB` adds extra paths. Union can preserve both old and reworded entries, so review its warnings. Do not apply union to reference docs: edit substantive documentation in place, and put checkpoint/status records in `docs/checkpoints/<ITEM>.md` without updating a central list. See the [append audit](docs/checkpoints/LE-APPEND-ONLY-DOCS-CONFLICT.md).

The `pg-mainff` Rust regression checks the keeper launch inventory against
`pyproject.toml`. Packaging metadata changes therefore select Rust CI through
`tools/ci_select.py` as well as the ordinary packaging checks.

The pre-push gate prints its hook checkout HEAD. A hook checkout missing commits
older than one day from `origin/main` refuses pushes and
prints a `pg-repo-sync --repo PATH` repair command. Sync discovers effective
`core.hooksPath` targets and symlinked hooks outside its normal roots; keep the
shared hook checkout on a clean main branch. Old cached upstream tips are
fetched before checking freshness; a failed refresh refuses the push. Changes to
gates on topic branches take effect in the shared hook checkout after merge and sync.

The new-code policy gate also scans test sources for portability hazards. Rust
`read_to_string` results must normalize CRLF before splitting on LF literals;
`.lines()` is already CRLF aware. The guard follows local bindings and aliases,
not cross-function data flow or macro expansions. Test sources must use temporary
or synthetic paths instead of literal `/home/<user>` paths, including fixtures.
Run it alone with `cargo run --manifest-path rust/Cargo.toml -p pg-policy --
--test-sources .`; `python3 tools/check_new_code_lang.py` runs it in fast checks
and CI. No allowlist is provided.

The fleet Stop hook reads the commit SHA from plan-order's `DONE JOB-fix SHA`
report and verifies it against the checkout HEAD before calling the prebuilt
`pg-policy` from `PG_POLICY_BIN` or PATH. Launchers can also select a fix by
setting `PG_LESSON_FIX_COMMIT`. A missing or unverified Class-Search record
is filed as a LESSON-TO-GATE failure.

## Fast local CI (D-6054)

Run `cargo run --manifest-path rust/Cargo.toml -p plangraph-cli -- fast-checks`
from a checkout. Use `--base <revision>` to override `origin/main`, or
`--plan --files <paths...>` to inspect fixture selection without executing checks.
The command delegates job selection to `tools/ci_select.py`, includes committed,
tracked local and untracked changes, and prints CI job booleans before its steps.
Use `--owned-worktree` for committed-head checks. It runs in a disposable
checkout and saves a review stamp by commit SHA in the repository's shared Git state.
It runs the four always-required tree guards, commit and whitespace gates,
affected Python tests with the existing full-suite refusal, and fmt/clippy/tests
for touched Rust crates and local reverse dependencies in their own workspaces.
The Rust test environment guard runs even when Python files were untouched,
checking that Rust-launched Python snippets have no pytest dependencies. Changed Rust
inputs with CRLF fail early with a normalization diagnostic. Worker tests use
the existing CI Temporal wrapper and require the Temporal CLI and protobuf tools.
Use a warm `CARGO_TARGET_DIR`; the profile does not install dependencies.
The fleet snapshot selection builds `pg-spawnd` from the checkout under the
Cargo pool lease before pytest. Fast checks and the native writer gate validate
Class-Search body lines in the commit range using `pg-policy`.
A non-Rust lane push can reuse a current cached policy executable even when
the shared target is at the build cap.
Each step and the total report wall seconds; exceeding 300 seconds emits a
warning and returns failure after checks, without interrupting or weakening them.
Affected tests and base replays run with context, session and Python path
overrides removed. To record a known base failure, use one `Fails-at-Base:`
commit trailer per pytest node, with the pinned base SHA followed by the node
ID. Fast checks print the replay command and its output from a disposable
checkout. A passing or unreproduced base failure rejects the claim, and the
affected tests must still pass on the proposed head.
This is the cheap local projection of CI; hosted browser, Postgres and Windows
runners remain CI responsibilities. A typical build host timing measurement is still
required before claiming that host's five-minute done criterion.

D-6054: OpenCode catalog integration tests use the shipped provider configuration
and existing Python rendering helpers. Their inputs are listed in `RUST_INPUTS`
so changes to catalog identities or rendering select the Rust CI suite.

Notification smoke: `tools/smoke-run-exit-notify.sh` targets
`omniroute/cx-gpt-6.2-sol-medium` (D-6310).

Claude transcript census: see [usage and classification limits](docs/claude-census.md).

The GO-REQ key list (`tools/merge_batch_util.py` `go_key_block`) is generated from the `Plan-Item` trailers of the train's commits not on main; keys trailered only on `docs/checkpoints/**` print as `GO-DEFERRED [..]`. Check a GO row against it with `pg go-row check` (see `docs/KEEPER.md`).

## Bash guard rules (`tools/hooks/bash_guard.py`)

The PreToolUse Bash guard (`tools/hooks/bash_guard.py` / `bash_guard_rules.py`)
denies:
- Unquoted heredocs carrying unescaped backticks or `$(...)`.
- `claude --bg`/`--background` or `claude -p`/`--print` commands with live
  double-quoted or unquoted command substitutions.
- Non-resume `claude --bg` / `--background` launches unconditionally (rule D-158;
  including `--bg=0` / `--background=0`, fail closed), so session names come from
  the fleet launcher counter (`Sessions` launches `claude` via Python subprocess,
  never through the Bash tool). `claude -p`/`--print`, `claude --bg --resume
  <uuid|hex>` / `-r <uuid|hex>` without `-n`/`--name`, `claude --resume`,
  `claude agents`, and `claude stop <id>` stay allowed.
- A while or until condition where a literal `pgrep -f` pattern can match the
  shell running the loop. Wait on the PID or unit, or put the original command
  in the Bash tool's background mode.
- Bare `pytest` / `cargo test` runs when host opt-in `pgtest-required` is present
  (see `docs/TESTING.md`).
- Sleep loops with a literal `pgrep -f` condition that can match their own shell command line.

## Skill lint (`tools/skill-rules.py`)

`python3 tools/skill-rules.py lint` fails when a skill markdown file passes 500
lines, a `SKILL.md` lacks frontmatter, or any skill text names a path under the
retired routing folder. Skills read site values from the router
config keys they name, never from absolute paths.

`tools/apply_batch.py` also accepts a role-l1 token that holds a router-written apply grant
(`pg grant-apply NAME PROJECT`); the keeper refuses any key outside the granted projects and posts
one `granted_apply` report line per applied batch to `routing`.

The Bash and context guards have a fail-closed fleet registration and an
isolated missing-file, crash, and timeout fixture. [Guard hooks](docs/guard-hooks.md)
