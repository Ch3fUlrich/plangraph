The stray `.git` guard attributes a leak only where it is exact: per test in directories the
worker owns, and once per session (never per test, never removed) for the shared system temp
root, so a concurrent test or another session's run can no longer be blamed for, or hide, a leak.
Under xdist, each worker owns its basetemp and process temp dir. The controller checks
both shared trees once after all workers finish, including when `--basetemp` and
`TMPDIR` are separate, and fails the session in strict mode if it finds a new leak.
