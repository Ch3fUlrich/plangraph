# Fast writer, L2 and PR checks

Install `requirements-ci.txt` in a virtual environment (includes pytest-xdist),
activate it, then use bounded selection for writer and L2 checks:

```sh
python3 tools/affected_tests.py --base origin/main --local --files-only --run
```

Affected PR CI runs the selector without the local flags. The local selection stops at 39 test files,
except for a change under `src/plangraph/spawner/`, `planner/` or `routing_support/`, which selects every
test that reaches it (the pre-push hook keeps the bound with `--keep-local-bound`).

The selector's optional PostgreSQL pass needs both the server binaries and
`psycopg` in the test interpreter to start a throwaway cluster. It reports a
skip when either dependency is missing. An explicit `PG_DSN` still selects
PostgreSQL tests and reports any missing dependency as a failure.

For a train resolution, set `--base` to the start of the fix range and use
`--local --files-only --run -n 2`. Run the push checks with
`python3 tools/preflight.py --base <range-base> --head HEAD --lane`.
The Bash guard rejects full-tree pytest on `land/*` and `lane/*` worktrees.
For an operator-approved exception, supply its D-id in
`PLANGRAPH_FULL_PYTEST_D_ID` in the hook's environment.
The base must exist locally. The tool diffs the merge base against the working tree,
including staged, unstaged and untracked files. JSON output explains the plan;
without `--run` it only prints the plan. Invalid refs fail rather than selecting nothing.
`--workers 4` overrides the default `-n auto`; `PYTEST_XDIST_AUTO_NUM_WORKERS=4`
also caps auto on resource-constrained hosts. Exit status propagates pytest failures.
`--run` is quiet (`-q --tb=short --no-header`, only durations over 1 s); `--loud` restores
pytest's default report. After a failed run (exit 1, at most 50 failed ids) the failed ids rerun
with `-n0` and at the first worker count, with a tree hash around every run. `FLAKY (...)` needs
an explicit pass on both reruns and equal hashes; `FAILED-IN-PARALLEL` is a pass at `-n0` and a
failure at the worker count; `FAILED-THEN-PASSED` is a pass after the tree changed. The exit
status stays that of the first run, so a flake is named, never hidden.

Pytest CI environment (read by `tests/conftest.py`, implemented in `tests/_split_ci_environment.py`):

- `PYTEST_SHARD=INDEX/COUNT` keeps one shard; an `xdist_group` stays whole in one shard.
- `PYTEST_SHARD_DURATIONS_FILE`: JSON object of canonical test ID to nonnegative seconds;
  shards are then balanced by recorded duration (unknown tests weigh 1 s) instead of by hash.
- `PYTEST_SHARD_DURATIONS_OUT`: path where a fully green run writes the same JSON, summing
  setup, call and teardown per canonical test ID (xdist `@group` suffix removed).
- `PYTEST_QUARANTINE_FILE`: JSON ledger mapping a canonical test ID to
  `{"owner": "plangraph:<ITEM-KEY>", "starts": <ISO UTC>, "expires": <ISO UTC>}`. Both
  timestamps must carry a UTC offset and `0 < expires - starts <= 48 h`; the owner must be a
  `plangraph:` item key. Any other entry is a usage error, never a silent quarantine.
- `PYTEST_QUARANTINE_AS_OF`: the shared UTC run timestamp (ISO, with offset) that decides
  which windows are active (`starts <= as_of < expires`); a window that has not begun or has
  expired blocks as normal.
- `PYTEST_QUARANTINE_MODE`: `blocking` (default) deselects active quarantined tests;
  `quarantine` runs only them, and an empty selection is not a failure.

Pytest supports expiry-bound quarantine selection. The part-entry replay is
tracked by `LE-CI-FLAKE-ENTRY-REPLAY`. The nonblocking job, retry ledger and
Keeper reporting are tracked by `LE-CI-FLAKE-RETRY-QUARANTINE-WIRING` and
require a workflow `Gate-Edit` decision.

Selection follows transitive AST imports in src/ and tests/, plus test references
to filenames for subprocess consumers. Changed tests select themselves.
The push hook uses `--local --files-only` to keep up to 39 test files, giving
priority to policy checks, changed tests and direct consumers. A changed package
initializer selects local tests that explicitly import the package or a changed
export. Local selection stops at re-export boundaries. The selector reports
additional files deferred to CI. Workflow-only changes select CI regression
tests in local preflight and the affected PR tier. Only the merge candidate into main (a train head or a `ci-full` pull request) runs the full
suite.
Outside file-only local preflight, conftest/shared helpers, config, and deleted or unmapped files can
select the full suite. AST cannot establish arbitrary runtime, plugin, or filesystem dependencies.
Request full verification when changing behavior with those dependencies.

CI runs the full suite once, on the **merge candidate into main**, not routinely in writer loops (docs/ci-gate.md):

```sh
python3 -m pytest -n auto tests
```

Conservative fallback is the exception for PR changes that cannot safely be mapped.
Browser tests retain their separate explicit invocation and dependencies.
Targets: typical affected run under 3 minutes; full parallel run under 8 minutes.
These are performance goals, not assertions to weaken when a host is overloaded.

## Preflight Rust target and sccache

`tools/preflight.py` runs its Rust family with a private `CARGO_TARGET_DIR`
(`preflight-cargo-*` under `PLANGRAPH_PREFLIGHT_RUN_DIR`, default the parent of
the shared target) so concurrent pushes do not wait on one cargo lock, but only
when `sccache` is on PATH (or `PLANGRAPH_SCCACHE` names it) and the effective `RUSTC_WRAPPER` is sccache (an inherited other wrapper keeps the shared target): a fresh target
without sccache is always cold and exceeds the Rust timeout. Without sccache
preflight prints `rust: shared CARGO_TARGET_DIR (no sccache; ...)` and uses the
shared target (`PLANGRAPH_CARGO_TARGET_DIR`). The private directory is removed
when preflight exits, also after a failed, timed-out or aborted run; a hard kill
(SIGKILL, host crash) can leave one behind, so each run deletes
`preflight-cargo-*` directories older than 12 hours before creating its own.
When free disk there is below `PLANGRAPH_PREFLIGHT_MIN_FREE_GB` (default 20), or
the value is invalid, preflight prints `rust: shared CARGO_TARGET_DIR (...)` and
uses the shared target instead; this never fails the push. With sccache,
`RUSTC_WRAPPER` is set only when not already set, and `SCCACHE_CACHE_SIZE` comes
from `PLANGRAPH_SCCACHE_CACHE_SIZE`, else an inherited `SCCACHE_CACHE_SIZE`,
else 20G. The shared-target lease taken around each cargo command is a shared
(non-exclusive) flock, so concurrent runs do not block each other.

## Isolation

pytest's tmp_path includes the worker ID. Each worker also gets a private temporary
root for daemon helpers, even when it inherits the controller's TMPDIR. Keeper
Doors binds port zero (the OS reserves the socket); never replace this with fixed
ports or a find-free-then-bind race. PostgreSQL keeper schemas are hashes of absolute
DB paths, so worker-specific paths isolate data in a shared PG database without
requiring per-worker database creation privileges. Supply PG_DSN for live PG checks.
`tests/test_parallel_isolation.py` pins directory/schema separation and simultaneous
live keeper listeners; run it three times with xdist after fixture changes.

## Writing load-independent tests

A test that passes alone and fails inside a full run on a loaded or shared host
costs every writer a full fast-check run. Measured 2026-11-24 to 2026-11-28 (CI
run attempts and the dell and workstation fast-check logs), these rules each
answer one real failure.

1. No real host reads. A spawner test never reaches the real memory reader, the
   ambient model catalog, the installed `opencode` binary or the host's `systemctl`;
   pin them with a fixture. tests/conftest.py raises "spawner test reached the real
   host memory reader; pin it with the admission_memory fixture"; one train head
   failed 68 tests on it (run 45564794074).
2. Private directories for everything a test writes or locks. Never write under a
   directory that parallel tests or a second process share. Example:
   pg-spawnd/tests/fleet_launch/interrupted.rs:96 (os.replace of
   `$ROOT/guard/<id>.json.tmp` raised FileNotFoundError because two runs shared
   the dir). A module-level cache is shared state too: tests/test_stall_alarms.py
   `test_failing_repo_cached_for_ttl` clears `stall._cache` by hand and fails when
   the order changes.
3. No fixed sleeps and no wall-clock upper bounds. Wait on a condition with a
   generous deadline, or inject the clock. Examples:
   tests/test_spawner_base_fresh.py `test_probe_budget_counts_routing_time_spent_before_the_run_record`
   (`assert 1.82 < 1.8`, five reruns); `test_admission_timeout_fails_original_run_records`
   (`assert 1 > 1` against a 0.3 s watchdog); pg-spawnd/tests/placement_spawn.rs:132
   (`elapsed < 1.0 s`) and :145 (`sleep(1100 ms)`);
   plangraph-cli/src/ci_watch/batch_delivery_tests.rs:228 (`elapsed < 10 s`);
   pg-spawnd/src/evidence_tests.rs:189 (10 s ready wait for two child processes).
4. Write-then-exec fixtures use `executable_test_support::write_executable`, never
   `fs::write` plus `Command`: a parallel test that forks while the file is open
   for writing makes the exec fail with "Text file busy" (ETXTBSY). Examples:
   plangraph-cli/src/fast_check_target.rs:252 and pg-repo-sync/tests/sync.rs:295.
   `executable_fixtures_use_the_shared_writer` fails the head that forgets it.
5. Compare paths in canonical form on Windows. `canonicalize` returns `\\?\C:\...`
   there; compare canonicalized to canonicalized or strip the prefix. One
   head failed only on the Windows job (run 46090538048).
6. Tests that share a real resource (a socket path, the Cargo lease, a shared
   target, a fixed port) go into a nextest test group with one slot, or take the
   resource from a private tempdir. Example: pg-spawnd/src/transport_unix.rs:221
   `concurrent_bind_cannot_replace_live_socket` pins an inode number that the
   filesystem reuses after remove and bind.
7. A test that spawns a child process asserts the child's stderr in the failure
   message and has a deadline that scales with load. Example:
   pg-spawnd/tests/mimo_output_cap.rs:46 prints the child's stderr only after the
   child failed under load.
8. Never depend on a download inside a test job. The browser job installs
   Chromium with `playwright install --with-deps` under a 285 s bounded step and
   exits 124 on a cache miss (UI tests in headless Chromium, 16 failed attempts
   in four days); restore the browser from the cache or the runner image.

Check a new or changed test by running it alone three times and then once next to
a CPU-bound process (`taskset`/`stress`), before the head leaves the writer.

## W22 measured evidence (local sandbox)

For the representative two-file change `src/plangraph/live_follow.py` plus
`tests/test_live_follow.py`, the selector chose that test and
`tests/test_live_follow_token.py`: 97 passed. Graph construction took 1.223 s;
serial pytest wall time was 0.455 s and two-worker pytest was 0.498 s (startup
dominates this small selection). Selection plus parallel execution was 1.721 s,
well below the 180 s target, but parallelism was not faster on this tiny case.
Three consecutive two-worker isolation/keeper-door runs each passed all 18 tests:
3.120 s, 3.118 s and 3.131 s wall time. Live PostgreSQL was not available; schema
derivation was checked without a server. A broader `mimo_policy.py` selection
exceeded a 120 s serial measurement budget; affected runs are not universally
under three minutes. No full-suite timing was taken in the writer loop, so the
eight-minute full-parallel target remains unverified (the supplied ~25-minute
serial baseline is not a measurement from this sandbox).

## Opt-in bash_guard test placement

The host-local marker `$XDG_CONFIG_HOME/plangraph/pgtest-required` (default
`~/.config/plangraph/pgtest-required`) enables bare-test refusals. Without it,
guard behaviour is unchanged. Unreadable markers count as absent; this change
does not create the marker or install live hooks.
Run `python3 tools/pgtest.py <command>` (or the executable `tools/pgtest.py`):
Linux uses `nice` and `systemd-run` in `agent-tests.slice`, with
`PGTEST_MEMORY_MAX` defaulting to `2G`. Without systemd-run it warns and uses
nice only. Windows assigns the child to a kill-on-close Job Object. Exit codes
pass through. Direct systemd-run commands selecting that slice remain allowed.

Python interpreter option values, Cargo toolchain selectors, and wrapper option
values are skipped before checking the test command; quoted option values are
not commands. An unresolvable home directory counts as an absent marker.

Git hook callers must not bind tests to their repository. The pre-push gate
passes a clean environment to the selector, which cleans it again for pytest;
the session guard also removes inherited bindings before collection and fixtures.
Each boundary removes Git-advertised local variables, `GIT_CONFIG*` and
`GIT_PREFIX`. Environment-discovery errors stop test launches. Regression tests
use throwaway caller repositories and verify refs, HEAD and config byte for byte.

Rust publish fixtures in `rust/worker-core/src/finalize_publish_tests.rs` create
commits through the production `finalize_publish::git` helper with
`-c user.name=Fixture` and `-c user.email=fixture@example.invalid`. The helper
uses `workspace_resolver::git_command` and strips inherited `GIT_*` identity
variables before running Git, so fixture identity relies on that filtering.
`fixture_has_explicit_isolated_ident` checks the newer fixture commit's author;
it does not assert the committer identity or the older fixture commit's identity.

Caller-repository snapshot fixtures configure `maintenance.auto=false`,
`maintenance.autoDetach=false`, `gc.auto=0` and `gc.autoDetach=false` locally
before their first commit, including the child checkout after Git environment
scrubbing. This prevents commit-triggered detached maintenance from racing a
snapshot. The shared snapshot in `tests/test_preflight_git_env.py` ignores only
Git-internal lock files; it retains metadata/object bytes, modes, symlinks and
working-tree contents. Missing non-lock entries remain mutation evidence.
Mutation probes cover HEAD, refs, index, config, reachable objects, tracked and
untracked worktree files (including worktree files named with a `.lock` suffix).
