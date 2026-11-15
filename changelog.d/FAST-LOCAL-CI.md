# FAST-LOCAL-CI

Add `plangraph fast-checks`, a Rust local CI profile using the existing CI job
selector and affected-test graph. It checks tree gates and touched Rust crates
across workspaces, runs the Rust Python dependency guard and checks CRLF Rust inputs, and reports per-step
wall time with a failing 300-second budget warning. See CONTRIBUTING.md.

Open: measure a typical PR on a typical build host; local checks do not replace hosted
Postgres/browser/Windows jobs.
