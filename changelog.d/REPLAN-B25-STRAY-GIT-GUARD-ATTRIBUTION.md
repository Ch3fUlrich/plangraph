The stray `.git` guard attributes a leak only where it is exact: per test in directories the
worker owns, and once per session (never per test, never removed) for the shared system temp
root, so a concurrent test or another session's run can no longer be blamed for, or hide, a leak.
Under xdist only the worker's own basetemp counts as its own (the tree above it is shared with its sibling
workers), and a single worker (gw0, or the plain process) does the once-per-session shared check.
