## Rust test deadlines in local checks

Preflight modes that run Rust tests and the Rust steps in `plangraph
fast-checks` require `cargo-nextest` when the repository supplies
`rust/.config/nextest.toml`. They pass that config explicitly to standalone
crates. A timed-out test fails and logs a `HUNG` line with its nextest ID.
The enclosing preflight process deadline also covers compilation.

`LE-CI-HUNG-TEST-RUNNER-ENFORCEMENT` in the batch's `slices.json` tracks
the protected hosted CI rollout, Rust stack capture, timeout reports to
Keeper and the lane push path. The lane pre-push hook runs formatting and
lints. The hosted workflow edit needs a routing `Gate-Edit` decision.
