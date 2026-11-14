# Keeper backup
Build `keeper-backup` from `pg-worker-core`; inactive units expect it in `~/.local/bin`.
Store its private environment file at `.config/plangraph/backup.env` relative to the user's home directory.
Set PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE, PGSSLMODE and PGSCHEMA there. Docker receives PG variables by name.
`dump DIRECTORY` keeps 14 private archives and a digest ledger; seq is a conservative pre-dump lower bound. `restore-test ARCHIVE` checks archived event seq >=19096, digest and >=40 tables (43 in the reference dump; partitions may grow), then restores in a disposable network-isolated cluster. Units need manual installation; concurrent manual runs are unsupported. External imports must enforce the same floor.
