The settings root search (`plangraph.settings`) now ignores an empty `.git` directory (a
repository has a HEAD, a linked checkout has a `.git` file) and stops at
`PLANGRAPH_ROOT_CEILING` as well as `GIT_CEILING_DIRECTORIES`, so a stray `/tmp/.git` no
longer changes where a test finds its settings. The test session sets the ceiling to the
pytest basetemp, and an autouse guard fails any test that leaves a `.git` at or above the
temp dir. Not found: the test or tool that creates the empty `/tmp/.git`; a quiet run of
7,800 Python tests with the guard left none, and a Rust test run alongside the earlier
hunts is the remaining suspect.
