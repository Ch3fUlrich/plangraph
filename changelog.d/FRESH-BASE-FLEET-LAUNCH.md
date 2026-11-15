Expose the existing fetched-origin checkout admission gate through the Rust
`plangraph base-fresh <repo>` command. It returns one reason line and a nonzero
status for stale checkouts, failed Git operations, or an unavailable gate.
Rust regression tests use current and stale local fixture repositories.

This is the CLI slice: routing's external `bin/fleet-launch` caller is not in
this repository and still needs wiring before any session worktree is created.
