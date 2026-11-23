## Rust test deadlines in local checks

Preflight modes that run Rust tests and the Rust steps in `plangraph
fast-checks` probe `cargo nextest --version` when the repository supplies
`rust/.config/nextest.toml`. They pass the config explicitly to standalone
crates. A timed-out test fails and logs a `HUNG` line with its nextest ID.
If the probe fails, both commands log a fallback to `cargo test`, which runs
without per-test deadlines. The enclosing preflight deadline covers the
entire Rust test command, including compilation.

The Linux integration test covers the `HUNG` message and cleanup of the
test's child process when cargo-nextest is on PATH. The focused command
from `rust/` is

```sh
cargo test -p plangraph-cli --test fast_checks preflight_reports_a_hanging_rust_test_and_reaps_its_child
```

`LE-CI-HUNG-TEST-RUNNER-ENFORCEMENT` in the batch's `slices.json` tracks
the protected hosted CI rollout, Rust stack capture, timeout reports to
Keeper and the lane push path. The lane pre-push hook runs formatting and
lints. The hosted workflow edit needs a routing `Gate-Edit` decision.
