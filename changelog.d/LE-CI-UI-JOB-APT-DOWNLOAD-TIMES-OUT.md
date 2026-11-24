The UI job installs Chromium's system packages before the timed browser tests, in steps of
their own: the package list comes from Playwright's dry run, the downloaded .deb files are
cached by that list, and apt runs directly as root with at most 3 tries. A try that times out
is killed with its whole process group and dpkg is repaired before the next try; the last
failure still fails the job. A slow apt mirror no longer fails the Chromium download or the
tests, and a warm run downloads nothing.
The job timeout is the sum of its step timeouts, and a workflow-only change selects the test
that checks the order, the retry bound, the cache key and that timeout.
