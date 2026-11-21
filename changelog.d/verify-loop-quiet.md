tools/affected_tests.py --run prints little and names flakes. It passes `-q --tb=short
--no-header` and `--durations-min=1.0` (`--loud` restores the default report) and records
each nodeid's outcome through a small controller-only `-p` plugin, so xdist works and
.pytest_cache is never read. After exit 1 with at most 50 failed ids, the failed ids rerun
with `-n0` and at the first worker count, with a tree hash (tracked diff plus untracked
files) around every run: `FLAKY` needs an explicit pass on both reruns and equal hashes,
`FAILED-IN-PARALLEL` and `FAILED-THEN-PASSED` have their own labels, and the exit code is
the first run's. Sample (two changed test files, -n 2, 83 tests passing): output 3364 bytes
before, 1857 after. Cases are in tests/test_affected_tests_quiet.py.
