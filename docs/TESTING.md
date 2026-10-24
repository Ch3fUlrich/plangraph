# Fast writer, L2 and PR checks

Install `requirements-ci.txt` in a virtual environment (includes pytest-xdist),
activate it, then use the same command for writers, L2 and PR CI:

```sh
python3 tools/affected_tests.py --base origin/main --run
```

Build-L1: use that line in PR CI; this change intentionally does not edit workflows.
The base must exist locally. The tool diffs the merge base against the working tree,
including staged, unstaged and untracked files. JSON output explains the plan;
without `--run` it only prints the plan. Invalid refs fail rather than selecting nothing.
`--workers 4` overrides the default `-n auto`; `PYTEST_XDIST_AUTO_NUM_WORKERS=4`
also caps auto on resource-constrained hosts. Exit status propagates pytest failures.

Selection follows transitive AST imports in src/ and tests/ (including relative
imports and package initializers), plus test references to filenames for subprocess
consumers. Changed tests select themselves. Conftest/shared test helpers, config,
workflows, deleted/unmapped files, parse failures and source files without mapped
tests select the full suite. This is deliberate over-selection, not an allow-list.
AST cannot establish arbitrary runtime/plugin/file-system dependencies: when changing
such behavior, request full verification rather than treating selection as coverage.

Run the full suite on **merge to main and nightly**, not routinely in writer loops:

```sh
python3 -m pytest -n auto tests
```

Conservative fallback is the exception for PR changes that cannot safely be mapped.
Browser tests retain their separate explicit invocation and dependencies.
Targets: typical affected run under 3 minutes; full parallel run under 8 minutes.
These are performance goals, not assertions to weaken when a host is overloaded.

## Isolation

pytest's tmp_path includes the worker ID. Each worker also gets a private temporary
root for daemon helpers, even when it inherits the controller's TMPDIR. Keeper
Doors binds port zero (the OS reserves the socket); never replace this with fixed
ports or a find-free-then-bind race. PostgreSQL keeper schemas are hashes of absolute
DB paths, so worker-specific paths isolate data in a shared PG database without
requiring per-worker database creation privileges. Supply PG_DSN for live PG checks.
`tests/test_parallel_isolation.py` pins directory/schema separation and simultaneous
live keeper listeners; run it three times with xdist after fixture changes.

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
