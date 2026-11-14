Add an offline Rust plan-quality library and `pg-plan-lint --snapshot FILE`
report CLI. Reports collect all fixes, expose per-item scores and codes, and
check card shape, criteria, command syntax, write scope, dependency reasons,
follow-up keys and duplicate criteria. Synthetic fixtures exercise the real CLI;
commands supplied in cards are never executed.
