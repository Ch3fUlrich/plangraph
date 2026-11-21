Fixed the D-6517 findings of the keeper host samples API: the Postgres host-sample tests also
read `PG_DSN` (a job with `PG_KEEPER_BACKEND=postgres` and no DSN fails them instead of skipping), a bucket's summed sequence gaps saturate at the BIGINT maximum instead of failing
the shared maintenance transaction, migration 14 revokes `PUBLIC` execute on the
`SECURITY DEFINER` partition function, and principal tokens that start with `pgh1_` resolve to
their own row and are never minted.
