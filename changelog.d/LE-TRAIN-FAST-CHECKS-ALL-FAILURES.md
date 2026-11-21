The push gate now runs `cargo test --no-fail-fast`, keeps running the remaining cargo checks and the
Python tests after a Rust failure and lists every failing Rust test in one pass; `affected_tests.py
--parts-union` (used by preflight) selects the tests of every part's changed files, so a break
between combined parts surfaces in one run.
