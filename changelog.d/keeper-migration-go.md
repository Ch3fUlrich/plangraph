Batch GO requests flag new keeper Postgres migrations with `MIGRATION <version>`.
Landing a migration requires the same field in the GO statement for the batch PR and exact pushed head.
`plangraph keeper-restart` applies the migration with a private migration-role
DSN, then checks the schema with the runtime role before restarting the keeper.
The restart guard reloads systemd and uses the effective keeper interpreter
and unit environment for both preflight commands.
