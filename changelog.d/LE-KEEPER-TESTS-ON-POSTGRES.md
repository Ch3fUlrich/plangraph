Changed keeper test files also run with the Postgres backend in the affected-test run (a throwaway
local cluster, or PG_DSN; a host without Postgres says so), `World.execute_many` replaces
SQLite-only `executemany` in keeper tests, and a lint test refuses SQLite-only connection calls.
