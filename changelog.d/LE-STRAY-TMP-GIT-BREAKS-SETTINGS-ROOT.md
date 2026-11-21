The settings root search (`plangraph.settings`) now ignores an empty `.git` directory (a
repository has a HEAD, a linked checkout has a `.git` file) and stops at
`PLANGRAPH_ROOT_CEILING` as well as `GIT_CEILING_DIRECTORIES`, so a stray `/tmp/.git` no
longer changes where a test finds its settings. The test session sets the ceiling to the
pytest basetemp, and an autouse guard fails the test that leaves a `.git` in a directory its worker owns (the
pytest basetemp, its parent, the process temp dir: one test runs at a time there, so the
attribution is exact) and removes an empty stray. A `.git` in a directory shared with other workers
and sessions (the system temp root above a private TMPDIR) is never attributed to a test or removed;
the session checks it once at its end and warns, or fails in CI or with `PLANGRAPH_STRAY_GIT_GUARD=1`.
Not found: the test
or tool that creates the empty `/tmp/.git`: single test files and quiet runs left none, and
the sporadic appearances coincide with other runs on the same host.
