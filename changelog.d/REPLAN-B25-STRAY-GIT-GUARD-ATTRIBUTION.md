The stray `.git` guard attributes a leak only where it is exact: per test in directories the
worker owns, and once per session (never per test, never removed) for the shared system temp
root, so a concurrent test or another session's run can no longer be blamed for, or hide, a leak.
