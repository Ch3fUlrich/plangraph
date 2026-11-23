## Rust test deadlines in local checks

Preflight modes that run Rust tests and the Rust steps in `plangraph
fast-checks` probe `cargo nextest --version` when the repository supplies
`rust/.config/nextest.toml`. They pass the config explicitly to standalone
crates. A timed-out test fails and logs a `HUNG` line with its nextest ID.
If the probe fails, both commands log a fallback to `cargo test`, which runs
without per-test deadlines. The enclosing preflight deadline covers the
entire Rust test command, including compilation.

On Linux, a simulated nextest run exercises preflight's `HUNG` reporting
and process cleanup at the enclosing deadline without requiring a nextest
installation. A second test checks cleanup by nextest itself when the
executable is available. Run both from `rust/` with

```sh
cargo test -p plangraph-cli --test fast_checks preflight_reports_a_
```

`LE-CI-HUNG-TEST-RUNNER-ENFORCEMENT` in the batch's `slices.json` tracks
the protected hosted CI rollout, Rust stack capture, timeout reports to
Keeper and the lane push path. The lane pre-push hook runs formatting and
lints. The hosted workflow edit needs a routing `Gate-Edit` decision.
