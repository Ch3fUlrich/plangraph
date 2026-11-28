A hanging test now fails by name: every setup, call and teardown phase has a 300 s budget
(`PYTEST_TEST_TIMEOUT`, `@pytest.mark.timeout(seconds)`, 0 = off) and over budget fails with
`TEST-TIMEOUT <nodeid> (<phase>) exceeded <N>s`; a faulthandler backstop dumps and exits 60 s
later when the interpreter never regains control. Cold cargo builds inside tests run under
`timeout_extended(1500)`. tools/affected_tests.py --run passes the pytest batch when every
failure is FLAKY on both reruns (`FLAKY-ONLY`), none timed out and --maxfail did not stop the
run; timed-out tests are not rerun. The committed ledger tests/quarantine.json applies in the
new `xfail` quarantine mode: active entries run and report but cannot fail the run.
