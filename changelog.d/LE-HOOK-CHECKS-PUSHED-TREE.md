Pre-push checks every pushed head's committed file sizes, exemptions, baseline
and public scrub rules instead of the pushing checkout's files. Preflight runs
in a temporary detached worktree of the pushed head and cleans it up after
success or failure, so another checked-out branch and dirty files cannot change
which tests or policy inputs are checked. Rust hook regressions cover both
checkout-only oversized files passing and committed oversized files refusing.
