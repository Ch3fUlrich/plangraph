The settings root search (`plangraph.settings`) now ignores an empty `.git` directory (a
repository has a HEAD, a linked checkout has a `.git` file) and stops at
`PLANGRAPH_ROOT_CEILING` as well as `GIT_CEILING_DIRECTORIES`, so a stray `/tmp/.git` no
longer changes where a test finds its settings. The test session sets the ceiling to the
pytest basetemp, and an autouse guard reports any test that leaves a `.git` at or above the
temp dir (it fails the test in CI or with `PLANGRAPH_STRAY_GIT_GUARD=1`, and warns on a shared
workstation, where another session's run can create the same `.git`). Not found: the test
or tool that creates the empty `/tmp/.git`: single test files and quiet runs left none, and
the sporadic appearances coincide with other runs on the same host.
