## DELL-CI-RUN slice 1

Add `python3 tools/dell_ci_run.py --sha <40-hex>` for SSH-based CI runs on
`dell-build`. Each run fetches origin, verifies a clean detached worktree,
records job status/timing in `out/dell-ci` (or `--out`), and removes its worktree.
Use `--jobs select,scrub,file-size,changelog-docs,pytest` to include selection;
the default omits select. Comparison commands use `origin/main`, matching PR CI.
The remote login directory must be the repository with CI prerequisites already
installed, including GNU timeout. Postgres and Rust jobs are recorded as skipped
with reason `slice 2`. Captured remote output is never printed or stored.
