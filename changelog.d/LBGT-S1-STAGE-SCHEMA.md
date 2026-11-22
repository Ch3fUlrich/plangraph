Keeper Postgres migration 18 and SQLite schema v19 add landing stage timing rows.
The repository, batch, stage and attempt identify a row so duplicate deliveries
leave its head and timestamps intact. The keeper snapshot copy preserves these rows.
