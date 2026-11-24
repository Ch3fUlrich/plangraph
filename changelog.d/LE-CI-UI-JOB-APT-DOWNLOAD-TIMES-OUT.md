The UI job installs Chromium's system packages in their own step, before the timed
browser tests, with its own timeout and at most 3 tries; the last failure still fails the
job. A slow apt mirror no longer kills the Chromium download step or the tests.
A test checks the step order, the retry bound and that the test step is unchanged.
The UI job's own timeout is raised to the sum of its step timeouts, and a workflow-only
change now selects the new test.
