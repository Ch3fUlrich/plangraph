Normalize legacy empty claim check kinds to `change` in Python, matching Rust.
Keep unknown recorded Rust claim kinds readable while rejecting execution as a
contract defect. Cover metadata parity, legacy decoding, and raw-empty red-first
execution without relaxing the unknown-kind validation contract.
Bind backend-specific metadata query placeholders, compare SQLite results even
without local Postgres, and test recorded unknown-kind execution refusal.
Remove a redundant backend assertion and extend check-kind coverage to malformed
stored cards, case/whitespace variants, and non-string recorded JSON values.
Fix parity queries to use the store's portable binding seam; exercise SQLite and
psycopg query conversion without a DSN, and format Rust contract tests for CI.
Exercise backend binding through the real Postgres adapter without requiring
psycopg in SQLite jobs; use lint-clean empty-kind checks in worker tests.
Restore independent psycopg wire-query conversion coverage when the optional
driver is installed, while retaining always-run adapter binding coverage.
